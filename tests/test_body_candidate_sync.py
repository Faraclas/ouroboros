"""Accepted mainline synchronization in the same private candidate, with real Git."""
from __future__ import annotations

import pathlib

import pytest

from ouroboros import body_candidate
from ouroboros.tools.git_pr import _candidate_merge
from tests.body_candidate_support import candidate_commit, git, isolate, make_ctx, make_serving


@pytest.fixture
def history(tmp_path, monkeypatch):
    serving = make_serving(tmp_path)
    data = isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    ctx = make_ctx(serving, data, "sync-author")
    bound = body_candidate.prepare(ctx)
    return serving, data, ctx, pathlib.Path(bound["path"])


def peer(serving, path="ouroboros/mod_b.py", text="GEN = 'PEER'\n"):
    return candidate_commit(serving, "accepted colleague", files={path: text})


def test_nonoverlapping_wip_survives_staging_and_abort(history):
    serving, data, ctx, candidate = history
    old = git(candidate, "rev-parse", "HEAD")
    (candidate / "ouroboros/mod_a.py").write_bytes(b"# unfinished\n")
    (candidate / "new.txt").write_bytes(b"untracked\n")
    accepted = peer(serving)
    # Even when repository config enables autostash, this operation owns no stash.
    git(candidate, "config", "merge.autostash", "true")
    result = _candidate_merge(ctx)
    assert "NOT committed" in result
    assert git(candidate, "rev-parse", "HEAD") == old
    assert git(candidate, "rev-parse", "MERGE_HEAD") == accepted
    assert (candidate / "ouroboros/mod_a.py").read_bytes() == b"# unfinished\n"
    assert (candidate / "new.txt").read_bytes() == b"untracked\n"
    assert git(candidate, "stash", "list") == ""
    assert "aborted" in _candidate_merge(ctx, "abort")
    assert (candidate / "ouroboros/mod_a.py").read_bytes() == b"# unfinished\n"
    assert (candidate / "ouroboros/mod_b.py").read_bytes() == b"GEN = 'GEN_OLD'\n"
    assert git(serving, "rev-parse", "HEAD") == accepted
    assert git(serving, "status", "--porcelain") == ""


def test_conflict_stays_visible_and_abort_recovers(history):
    serving, data, ctx, candidate = history
    authored = candidate_commit(candidate)
    accepted = peer(serving, "ouroboros/mod_a.py")
    result = _candidate_merge(ctx)
    assert "CANDIDATE_MERGE_FAILED" in result
    assert git(candidate, "diff", "--name-only", "--diff-filter=U") == "ouroboros/mod_a.py"
    assert git(candidate, "rev-parse", "HEAD") == authored
    assert git(candidate, "rev-parse", "MERGE_HEAD") == accepted
    assert "CANDIDATE_MERGE_IN_PROGRESS" in _candidate_merge(ctx)
    assert "aborted" in _candidate_merge(ctx, "abort")
    assert git(candidate, "status", "--porcelain") == ""
    assert (candidate / "ouroboros/mod_a.py").read_bytes() == b"GEN = 'GEN_CAND'\n"


@pytest.mark.parametrize("staged", [False, True])
def test_overwrite_refusal_preserves_index_and_bytes(history, staged):
    serving, data, ctx, candidate = history
    peer(serving)
    (candidate / "ouroboros/mod_b.py").write_bytes(b"mine\n")
    if staged:
        git(candidate, "add", "ouroboros/mod_b.py")
    before = git(candidate, "write-tree")
    assert "CANDIDATE_MERGE_FAILED" in _candidate_merge(ctx)
    assert git(candidate, "write-tree") == before
    assert (candidate / "ouroboros/mod_b.py").read_bytes() == b"mine\n"
    assert "CANDIDATE_MERGE_NOT_STARTED" in _candidate_merge(ctx, "abort")


def test_real_commit_preparation_and_parent_binding_preserve_history(history):
    serving, data, ctx, candidate = history
    from ouroboros.mutation_attribution import capture_mutation_baseline, attributed_git_candidates
    from ouroboros.tools.git import _prepare_review_commit_worktree, _task_attributed_commit_paths
    from ouroboros.tools.git_review_cycle import _fingerprint_staged_diff, _verify_reviewed_commit_binding

    authored = candidate_commit(candidate)
    body_candidate.record_reviewed_commit(ctx, authored)
    capture_mutation_baseline(data, ctx.task_id,
                              [{"surface_type": "system_repo", "host_root": str(candidate)}],
                              owner_id=ctx.task_id)
    accepted = peer(serving)
    (candidate / "draft.txt").write_bytes(b"unfinished\n")
    assert "NOT committed" in _candidate_merge(ctx)
    assert attributed_git_candidates(data, ctx.task_id, candidate)["blockers"] == []
    assert _prepare_review_commit_worktree(ctx, None)[1] == ""
    paths, attribution, error, binding = _task_attributed_commit_paths(ctx, None)
    assert not error and set(paths) == {"draft.txt", "ouroboros/mod_b.py"}
    # Public commit preparation keeps both parents. The miniature fixture uses
    # Git for the final commit, not a fake paid review of this toy repository.
    git(candidate, "add", "draft.txt")
    fingerprint = _fingerprint_staged_diff(candidate)
    assert fingerprint["binding"]["parents"] == [authored, accepted]
    git(candidate, "commit", "-m", "reviewed merge fixture")
    merged = git(candidate, "rev-parse", "HEAD")
    assert _verify_reviewed_commit_binding(candidate, merged, fingerprint, verify_expected_tag=False) == (True, "")
    body_candidate.record_reviewed_commit(ctx, merged)
    row = body_candidate.find(ctx.task_id)
    assert body_candidate.unreviewed_commits({**row, "base_sha": accepted}, merged) == []
    assert body_candidate.adoption_base(ctx) == accepted
    assert git(candidate, "rev-list", "--first-parent", "-n", "2", "HEAD").splitlines() == [merged, authored]
    assert git(serving, "rev-parse", "HEAD") == accepted


def test_registry_stage_and_readonly_light_refusals(history, monkeypatch):
    serving, data, ctx, candidate = history
    from ouroboros.tools.registry import ToolRegistry
    accepted = peer(serving)
    registry = ToolRegistry(repo_dir=serving, drive_root=data)
    registry.set_context(ctx)
    result = registry.execute_result("candidate_merge", {})
    assert result.status == "ok", result
    assert accepted in result.text
    assert git(candidate, "rev-parse", "MERGE_HEAD") == accepted
    assert "aborted" in registry.execute("candidate_merge", {"action": "abort"})
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    assert "LIGHT_MODE_BLOCKED" in registry.execute("candidate_merge", {})


def test_noop_and_bad_arguments(history):
    serving, data, ctx, candidate = history
    assert "already contains" in _candidate_merge(ctx)
    assert "CANDIDATE_MERGE_ARGUMENT" in _candidate_merge(ctx, "reset")
    unbound = make_ctx(serving, data, "no-candidate")
    assert "CANDIDATE_MERGE_UNBOUND" in _candidate_merge(unbound)
    assert git(candidate, "status", "--porcelain") == ""


from tests.test_git_review_preflight_gate import candidate  # noqa: F401


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
@pytest.mark.parametrize("explicit_paths", [False, True])
@pytest.mark.parametrize("peer_release", [False, True])
def test_review_handback_preserves_second_parent(candidate, monkeypatch, enforcement, explicit_paths, peer_release):  # noqa: F811
    """Run the real reviewed-commit cycle through feedback, not just raw git commit."""
    import json
    from ouroboros.mutation_attribution import capture_mutation_baseline
    from ouroboros.review_ledger import CouplingOutcome
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import git as owner

    ctx = candidate
    repo = ctx.repo_dir
    branch = git(repo, "branch", "--show-current")
    ctx.branch_dev = branch
    (repo / "VERSION").write_bytes(b"1.0.0\n")
    git(repo, "add", "VERSION")
    git(repo, "commit", "-m", "base version")
    (repo / "change.py").write_bytes(b"value = 4\n")
    git(repo, "add", "change.py")
    git(repo, "commit", "-m", "author work")
    authored = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD~1")
    git(repo, "checkout", "-b", "accepted-peer", base)
    if peer_release:
        (repo / "VERSION").write_bytes(b"2.0.0\n")
    accepted = peer(repo, "peer.txt", "accepted\n")
    if peer_release:
        git(repo, "tag", "-a", "v2.0.0", "-m", "peer release", accepted)
        serving = repo.parent / "serving"
        git(repo, "worktree", "add", "--detach", str(serving), accepted)
        ctx.serving_repo_dir = serving
        ctx.task_metadata = {"body_candidate": {"id": "fixture", "path": str(repo)}}
    git(repo, "checkout", branch)
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id,
                              [{"surface_type": "system_repo", "host_root": str(repo)}])
    git(repo, "merge", "--no-ff", "--no-commit", accepted)
    if explicit_paths:
        (repo / "change.py").write_bytes(b"value = 3\n")
        (repo / "draft.txt").write_bytes(b"unfinished\n")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setattr(owner, "_run_review_preflight_tests", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_post_commit_result", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_auto_push", lambda *a, **kw: "")
    reviews = []

    def reviewer(_ctx, message, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        reviews.append(kw["review_binding_fingerprint"])
        finding = {"item": "code_quality", "severity": "critical", "verdict": "FAIL", "reason": "fixture feedback"}
        ctx._last_review_critical_findings = [finding]
        ctx._last_triad_raw_results = [{"slot_id": "critic", "status": "responded", "parsed": [finding], "raw_text": "fixture feedback"}]
        return "fixture feedback", CouplingOutcome(verdict="PASS", status="responded"), "critical_findings", []

    monkeypatch.setattr(owner, "_run_parallel_review", reviewer)
    paths = ["change.py", "peer.txt"] if explicit_paths else None
    if paths and peer_release:
        paths.append("VERSION")
    if explicit_paths:
        refused = owner._repo_commit_push(ctx, "Finish merge", paths=["change.py"], skip_advisory_review=True)
        assert "GIT_PARTIAL_MERGE_BLOCKED" in refused
        assert not reviews
        assert git(repo, "rev-parse", "MERGE_HEAD") == accepted
    first = owner._repo_commit_push(ctx, "Finish merge", paths=paths, skip_advisory_review=True)
    assert git(repo, "rev-parse", "HEAD") == authored, first
    assert git(repo, "rev-parse", "MERGE_HEAD") == accepted, first
    assert not git(repo, "diff", "--cached", "--name-only")
    assert (repo / "peer.txt").read_bytes() == b"accepted\n"
    if enforcement == "blocking":
        assert first == "fixture feedback"  # the injected review owner's exact refusal
        return
    assert first.startswith("Review outcome returned before commit"), first
    reference = json.loads(first.split("\n", 1)[1])["review_reference"]
    second = owner._repo_commit_push(ctx, "Finish merge", paths=paths, review_reference=reference,
        author_disposition={"disposition": "accepted", "rationale": "Fixture feedback inspected."})
    merged = git(repo, "rev-parse", "HEAD")
    assert merged != authored, second
    assert git(repo, "rev-list", "--parents", "-n", "1", merged).split() == [merged, authored, accepted], second
    assert git(repo, "show", "HEAD:peer.txt") == "accepted"
    if explicit_paths:
        assert git(repo, "show", "HEAD:change.py") == "value = 3"
        assert (repo / "draft.txt").read_bytes() == b"unfinished\n"
        assert "?? draft.txt" in git(repo, "status", "--porcelain")
    assert len(reviews) == 1
    if peer_release:
        assert git(repo, "rev-parse", "v2.0.0^{commit}") == accepted
        assert git(repo, "show", "HEAD:VERSION") == "2.0.0"


def test_authored_existing_release_tag_still_refused(candidate):  # noqa: F811
    from ouroboros.tools.git_review_cycle import _fingerprint_staged_diff, _review_binding_precondition_error

    git(candidate.repo_dir, "tag", "v9.0.0")
    (candidate.repo_dir / "VERSION").write_bytes(b"9.0.0\n")
    git(candidate.repo_dir, "add", "VERSION")
    assert "REVIEW_BINDING_BLOCKED" in _review_binding_precondition_error(_fingerprint_staged_diff(candidate.repo_dir))


@pytest.mark.parametrize("authored_edit", [False, True])
@pytest.mark.parametrize("accepted_history", [False, True])
@pytest.mark.parametrize("stale_serving", [False, True])
def test_advanced_merge_preserves_authored_protected_boundary(candidate, monkeypatch, authored_edit, accepted_history, stale_serving):  # noqa: F811
    import time
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    branch = git(repo, "branch", "--show-current")
    git(repo, "commit", "-m", "author change")
    git(repo, "checkout", "-b", "accepted-protected", "HEAD~1")
    accepted = peer(repo, "BIBLE.md", "accepted protected source\n")
    serving = repo.parent / "serving"
    git(repo, "worktree", "add", "--detach", str(serving), accepted if accepted_history else "HEAD~1")
    if stale_serving:
        peer(serving, "BIBLE.md", "later accepted fix\n")
    ctx.serving_repo_dir = serving
    ctx.task_metadata = {"body_candidate": {"id": "fixture", "path": str(repo)}}
    git(repo, "checkout", branch)
    git(repo, "merge", "--no-ff", "--no-commit", accepted)
    if authored_edit:
        (repo / "BIBLE.md").write_bytes(b"author adaptation\n")
    monkeypatch.setattr(owner, "_current_runtime_mode", lambda: "advanced")
    marker = {"status": "blocked", "message": "after protection"}
    monkeypatch.setattr(owner, "_free_cycle_gate", lambda *a, **kw: marker)
    outcome = owner._run_reviewed_stage_cycle(ctx, "Merge", time.time(), paths=["BIBLE.md"])
    assert outcome["block_reason"] == "core_protection_blocked" if authored_edit or not accepted_history or stale_serving else outcome == marker
    assert git(repo, "rev-parse", "MERGE_HEAD") == accepted
