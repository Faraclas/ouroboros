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
@pytest.mark.parametrize(("peer_release", "recovery", "history_only"), [(False, False, False), (True, False, False), (False, True, False), (False, True, True)])
@pytest.mark.parametrize("converged", [False, True])
def test_review_handback_preserves_second_parent(candidate, monkeypatch, enforcement, explicit_paths, peer_release, converged, recovery, history_only):  # noqa: F811
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
    if history_only:
        (repo / "peer.txt").write_bytes(b"accepted\n")
        git(repo, "add", "peer.txt")
    if converged:
        (repo / "converged.txt").write_bytes(b"same independent contribution\n")
        git(repo, "add", "converged.txt")
    git(repo, "add", "change.py")
    git(repo, "commit", "-m", "author work")
    authored = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD~1")
    git(repo, "checkout", "-b", "candidate/recovered-peer" if recovery else "accepted-peer", base)
    if peer_release:
        (repo / "VERSION").write_bytes(b"2.0.0\n")
    if converged:
        (repo / "converged.txt").write_bytes(b"same independent contribution\n")
    accepted = peer(repo, "peer.txt", "accepted\n")
    if peer_release or recovery:
        if peer_release:
            git(repo, "tag", "-a", "v2.0.0", "-m", "peer release", accepted)
        serving = repo.parent / "serving"
        git(repo, "worktree", "add", "--detach", str(serving), base if recovery else accepted)
        ctx.serving_repo_dir = serving
        ctx.task_metadata = {"body_candidate": {"id": "fixture", "path": str(repo), "branch": branch}}
    git(repo, "checkout", branch)
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id,
                              [{"surface_type": "system_repo", "host_root": str(repo)}])
    if recovery:
        assert "NOT committed" in _candidate_merge(ctx, source="candidate/recovered-peer")
        from ouroboros.tools.git_review_cycle import _accepted_merge_refs
        assert _accepted_merge_refs(ctx) == []
    else:
        git(repo, "merge", "--no-ff", "--no-commit", accepted)
    if explicit_paths and not history_only:
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
    paths = [] if history_only else (["change.py", "peer.txt"] if explicit_paths else None)
    if paths and peer_release:
        paths.append("VERSION")
    if explicit_paths and not history_only:
        refused = owner._repo_commit_push(ctx, "Finish merge", paths=["change.py"], skip_advisory_review=True)
        assert "GIT_PARTIAL_MERGE_BLOCKED" in refused
        assert not reviews
        assert git(repo, "rev-parse", "MERGE_HEAD") == accepted
        # A worktree reversal must not hide an incoming change still in index.
        (repo / "peer.txt").unlink()
        refused = owner._repo_commit_push(ctx, "Finish merge", paths=["change.py"], skip_advisory_review=True)
        assert "GIT_PARTIAL_MERGE_BLOCKED" in refused
        (repo / "peer.txt").write_bytes(b"accepted\n")
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
    if explicit_paths and not history_only:
        refused = owner._repo_commit_push(ctx, "Finish merge", paths=["change.py"], skip_advisory_review=True)
        assert "GIT_PARTIAL_MERGE_BLOCKED" in refused
        assert git(repo, "rev-parse", "MERGE_HEAD") == accepted
    second = owner._repo_commit_push(ctx, "Finish merge", paths=paths, review_reference=reference,
        author_disposition={"disposition": "accepted", "rationale": "Fixture feedback inspected."})
    merged = git(repo, "rev-parse", "HEAD")
    assert merged != authored, second
    assert git(repo, "rev-list", "--parents", "-n", "1", merged).split() == [merged, authored, accepted], second
    assert git(repo, "show", "HEAD:peer.txt") == "accepted"
    if converged:
        assert git(repo, "show", "HEAD:converged.txt") == "same independent contribution"
    if explicit_paths and not history_only:
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


@pytest.mark.parametrize("action", ["add", "status", "staged-status", "staged-names", "staged-index"])
@pytest.mark.parametrize("resuming", [False, True])
def test_staging_git_failure_keeps_publication_and_original_error(candidate, monkeypatch, action, resuming):  # noqa: F811
    import time
    from ouroboros.tools import git as owner
    from ouroboros.tools import git_review_cycle as cycle

    ctx = candidate
    ctx._review_resume_pending = resuming
    observed = []
    monkeypatch.setattr(cycle, "_publish_git_error", lambda _ctx, text: observed.append("publish") or text)
    monkeypatch.setattr(owner, "_record_commit_attempt", lambda *a, **kw: observed.append("record"))

    def command(argv, **kw):
        if argv[1] == "rev-parse":
            return str(ctx.repo_dir / ".git" / "MERGE_HEAD")
        if "--no-renames" in argv and "-z" in argv:
            if action == "staged-index":
                raise RuntimeError("primary")
            return ""
        if argv[1] == "add" and action == "add":
            raise RuntimeError("primary")
        if argv[1] == "status":
            if action == "status":
                raise RuntimeError("primary")
            return "M change.py\n"
        if "--name-status" in argv:
            if action == "staged-status":
                raise RuntimeError("primary")
            return "" if action == "staged-names" else "M\tchange.py\n"
        if "--name-only" in argv:
            raise RuntimeError("secondary" if action == "staged-status" else "primary")
        return ""

    monkeypatch.setattr(owner, "run_cmd", command)
    paths, snapshot, error = owner._stage_candidate_for_review(
        ctx, "fixture", time.time(), paths=["change.py"], came_from_detached_checkout=False,
    )
    if resuming and action in {"add", "staged-index"}:
        assert error is None and paths == ["change.py"] and not observed
    else:
        assert paths == [] and snapshot is None and error["status"] == "failed"
        assert error["message"] == f"⚠️ GIT_ERROR ({action}): primary"
        assert bool(error.get("preserve_index")) is (action == "staged-index")
        assert observed == (["publish"] if resuming else ["publish", "record"])


def _retire_checkout_and_row(candidate_row):
    import shutil

    shutil.rmtree(candidate_row["path"])
    body_candidate._forget_missing(candidate_row)


def test_missing_checkout_branch_is_recovered_by_a_real_two_parent_merge(history):
    from tests.test_body_candidate import _terminal

    serving, data, ctx, candidate = history
    old_ctx = make_ctx(serving, data, "old-author")
    old_row = body_candidate.prepare(old_ctx)
    old_tip = candidate_commit(pathlib.Path(old_row["path"]), "old work", files={"old_work.txt": "kept\n"})
    old_branch = old_row["branch"]

    # Import reads one committed snapshot; taking that live checkout still refuses.
    with pytest.raises(body_candidate.CandidateRefused, match="belongs to task"):
        body_candidate.prepare(make_ctx(serving, data, "transfer-attempt"), resume="old-author")
    assert "NOT committed" in _candidate_merge(ctx, source="old-author")
    assert body_candidate.find("old-author")["task_id"] == "old-author"
    assert "aborted" in _candidate_merge(ctx, "abort")
    _terminal(data, "old-author")
    _retire_checkout_and_row(body_candidate.find("old-author"))
    assert body_candidate.find("old-author") is None
    own_tip = git(candidate, "rev-parse", "HEAD")

    for bad in ("no-such", "main", ctx.task_id):
        assert "⚠️" in _candidate_merge(ctx, source=bad)
    result = _candidate_merge(ctx, source=old_branch)
    assert "NOT committed" in result and old_branch in result
    assert git(candidate, "rev-parse", "HEAD") == own_tip
    assert git(candidate, "rev-parse", "MERGE_HEAD") == old_tip
    assert (candidate / "old_work.txt").read_text() == "kept\n"
    assert git(serving, "rev-parse", old_branch) == old_tip  # donor branch intact
    assert "aborted" in _candidate_merge(ctx, "abort")


@pytest.mark.parametrize("kind", ["delete", "rename", "mode", "restore"])
def test_merge_path_selection_keeps_noncontent_changes_and_foreign_dirt(candidate, monkeypatch, kind):  # noqa: F811
    import time
    from ouroboros.mutation_attribution import capture_mutation_baseline
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    branch = git(repo, "branch", "--show-current")
    git(repo, "commit", "-m", "author")
    git(repo, "checkout", "-b", "incoming")
    if kind in {"delete", "restore"}:
        git(repo, "rm", "change.py")
    elif kind == "rename":
        git(repo, "mv", "change.py", "renamed.py")
    else:
        git(repo, "config", "core.filemode", "false")
        git(repo, "update-index", "--chmod=+x", "change.py")
    git(repo, "commit", "-m", "incoming shape")
    incoming = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", branch)
    authored = candidate_commit(repo, files={"mine.txt": "mine\n"})
    (repo / "foreign.txt").write_bytes(b"preexisting work\n")
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id,
                              [{"surface_type": "system_repo", "host_root": str(repo)}])
    git(repo, "merge", "--no-ff", "--no-commit", incoming)
    git(repo, "add", "foreign.txt")
    paths, evidence, error, _ = owner._task_attributed_commit_paths(ctx, None)
    expected = {"change.py", "renamed.py"} if kind == "rename" else {"change.py"}
    assert not error and set(paths) == expected
    assert evidence["excluded_preexisting_dirty"] == ["foreign.txt"]
    index = git(repo, "write-tree")
    assert "GIT_ATTRIBUTION_BLOCKED" in owner._task_attributed_commit_paths(ctx, paths + ["foreign.txt"])[2]
    assert git(repo, "write-tree") == index
    _, _, refused = owner._stage_candidate_for_review(
        ctx, "shape", time.time(), paths=["mine.txt"], came_from_detached_checkout=False)
    assert "GIT_PARTIAL_MERGE_BLOCKED" in refused["message"]
    if kind == "restore":
        (repo / "change.py").write_bytes(b"restored = True\n")
    classified, _, blocked = owner._stage_candidate_for_review(
        ctx, "shape", time.time(), paths=paths, came_from_detached_checkout=False)
    assert classified == [] and blocked is not None and blocked["status"] == "blocked"
    assert "GIT_ATTRIBUTION_BLOCKED" in blocked["message"] and "foreign.txt" in blocked["message"]
    assert blocked.get("preserve_index") is True
    assert git(repo, "write-tree") == index
    assert git(repo, "rev-parse", "HEAD") == authored
    assert git(repo, "rev-parse", "MERGE_HEAD") == incoming
    assert git(repo, "show", ":foreign.txt") == "preexisting work"
    # Once foreign.txt is unstaged (still dirty in the worktree), attributed merge staging succeeds without it.
    git(repo, "reset", "HEAD", "--", "foreign.txt")
    classified, _, error = owner._stage_candidate_for_review(
        ctx, "shape", time.time(), paths=paths, came_from_detached_checkout=False)
    assert error is None and set(classified) == expected
    assert "foreign.txt" not in set(git(repo, "diff", "--cached", "--name-only").splitlines())
    assert git(repo, "rev-parse", "HEAD") == authored
    assert git(repo, "rev-parse", "MERGE_HEAD") == incoming
    assert (repo / "foreign.txt").read_bytes() == b"preexisting work\n"


@pytest.mark.parametrize("filename", ["peer.txt", "peer[1].txt"])
def test_ignored_incoming_addition_stays_required_after_handback(candidate, filename):  # noqa: F811
    import time
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    branch = git(repo, "branch", "--show-current")
    (repo / ".gitignore").write_bytes(b"peer*\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "base with ignored name")
    git(repo, "checkout", "-b", "ignored-peer")
    (repo / filename).write_bytes(b"tracked incoming data\n")
    git(repo, "add", "-f", f":(literal){filename}")
    git(repo, "commit", "-m", "tracked ignored addition")
    incoming = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", branch)
    candidate_commit(repo, files={"mine.txt": "mine\n"})
    git(repo, "merge", "--no-ff", "--no-commit", incoming)
    git(repo, "reset", "HEAD", "--", ":/")  # ordinary review handback
    assert filename not in git(repo, "status", "--porcelain")
    _, _, error = owner._stage_candidate_for_review(
        ctx, "incomplete ignored merge", time.time(), paths=["mine.txt"], came_from_detached_checkout=False)
    assert "GIT_PARTIAL_MERGE_BLOCKED" in error["message"]
    assert git(repo, "rev-parse", "MERGE_HEAD") == incoming
    assert (repo / filename).read_bytes() == b"tracked incoming data\n"


@pytest.mark.parametrize("status_fails", [False, True])
def test_empty_staged_status_reads_fallback_names_once(candidate, monkeypatch, status_fails):  # noqa: F811
    import time
    from ouroboros.tools import git as owner

    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if "--name-status" in argv and status_fails:
            raise RuntimeError("primary status failure")
        return "?? draft.txt\n" if argv[1] == "status" else ""
    monkeypatch.setattr(owner, "run_cmd", command)
    candidate._review_resume_pending = True
    assert owner._stage_candidate_for_review(
        candidate, "empty stage", time.time(), paths=["draft.txt"], came_from_detached_checkout=False,
    ) == ([], None, None)
    assert sum("--name-only" in argv for argv in calls) == 1


@pytest.mark.parametrize("generation", [1, 2])
def test_same_live_owner_recovers_exact_previous_branch_without_guessing(history, generation):
    serving, data, ctx, candidate = history
    tip = candidate_commit(candidate, files={"recovered.txt": "generation one\n"})
    for number in range(generation):
        previous = body_candidate.find(ctx.task_id)
        old_branch = previous["branch"]
        _retire_checkout_and_row(previous)
        # A fresh worker has no stale binding; prepare creates the next free branch.
        ctx = make_ctx(serving, data, ctx.task_id)
        new = body_candidate.prepare(ctx)
        candidate = pathlib.Path(new["path"])
        if number + 1 < generation:
            tip = candidate_commit(candidate, files={"recovered.txt": "generation two\n"})
    from tests.test_body_candidate import _registry
    registry = _registry(serving, data, ctx)
    result = registry.execute_result("candidate_merge", {"source": old_branch})
    assert result.status == "ok" and "NOT committed" in result.text, result.text
    assert git(candidate, "rev-parse", "MERGE_HEAD") == tip
    assert git(serving, "rev-parse", old_branch) == tip
    assert "generation" in (candidate / "recovered.txt").read_text()
    assert "aborted" in registry.execute("candidate_merge", {"action": "abort"})
    assert not (candidate / "recovered.txt").exists()


def test_transferred_live_donor_import_is_not_checkout_transfer(history, monkeypatch):
    from tests.test_body_candidate import _terminal, _registry
    serving, data, ctx, candidate = history
    donor = make_ctx(serving, data, "original")
    row = body_candidate.prepare(donor)
    tip = candidate_commit(pathlib.Path(row["path"]), files={"donor.txt": "committed\n"})
    _terminal(data, "original")
    successor = make_ctx(serving, data, "successor")
    body_candidate.prepare(successor, resume="original")
    before = body_candidate.find("successor")
    registry = _registry(serving, data, ctx)
    assert "NOT committed" in registry.execute("candidate_merge", {"source": "successor"})
    assert body_candidate.find("successor") == before
    assert git(candidate, "rev-parse", "MERGE_HEAD") == tip
    assert "aborted" in registry.execute("candidate_merge", {"action": "abort"})
    other = make_ctx(serving, data, "other")
    with pytest.raises(body_candidate.CandidateRefused) as refused:
        body_candidate.prepare(other, resume="successor")
    assert refused.value.code == "CANDIDATE_OWNER_LIVE"
    _terminal(data, "successor")
    monkeypatch.setattr(body_candidate, "live_lineage_processes", lambda *a: [123])
    with pytest.raises(body_candidate.CandidateRefused) as refused:
        body_candidate.prepare(other, resume="successor")
    assert refused.value.code == "CANDIDATE_OWNER_PROCESSES_LIVE"
    # Even unsettled donor processes grant no transfer, but cannot change a resolved import.
    assert "NOT committed" in registry.execute("candidate_merge", {"source": before["branch"]})
    later = candidate_commit(pathlib.Path(before["path"]), files={"donor.txt": "later\n"})
    assert later != tip and git(candidate, "rev-parse", "MERGE_HEAD") == tip
    assert (candidate / "donor.txt").read_text() == "committed\n"
    assert body_candidate.find("successor")["task_id"] == "successor"


def test_explicit_preservation_pin_recovers_files_not_staged_split(history):
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    old = make_ctx(serving, data, "preserved")
    row = body_candidate.prepare(old)
    path = pathlib.Path(row["path"])
    tip = candidate_commit(path, files={"notes.txt": "committed\n"})
    (path / "notes.txt").write_text("staged\n")
    git(path, "add", "notes.txt")
    (path / "notes.txt").write_text("filesystem\n")
    (path / "local-notes").mkdir()
    (path / "local-notes/private.txt").write_text("retained ignored file\n")
    pin = body_candidate._preserve(row, body_candidate.unique_work(row))
    assert pin == body_candidate.PIN_PREFIX + row["candidate_id"]
    snapshot = git(serving, "rev-parse", pin)
    assert git(serving, "show", pin + "^:notes.txt") == "staged"
    assert git(serving, "rev-parse", pin + "^^") == tip
    _retire_checkout_and_row(row)
    registry = _registry(serving, data, ctx)
    text = registry.execute("candidate_merge", {"source": row["branch"]})
    assert "committed history only" in text
    assert (candidate / "notes.txt").read_text() == "committed\n"
    assert not (candidate / "local-notes/private.txt").exists()
    assert "aborted" in registry.execute("candidate_merge", {"action": "abort"})
    text = registry.execute("candidate_merge", {"source": pin})
    assert "NOT committed" in text and pin in text
    assert git(candidate, "rev-parse", "MERGE_HEAD") == snapshot
    assert (candidate / "notes.txt").read_text() == "filesystem\n"
    assert git(candidate, "show", ":notes.txt") == "filesystem"
    assert (candidate / "local-notes/private.txt").read_text() == "retained ignored file\n"
    assert git(serving, "rev-parse", pin) == snapshot
    assert git(serving, "rev-parse", row["branch"]) == tip


@pytest.mark.parametrize("source", ["main", "unknown-task", "candidate/missing", "candidate/x~1", "candidate/x^{commit}",
                                    "refs/heads/ouroboros", "refs/ouroboros/candidates/nope^", "candidate/../ouroboros"])
def test_recovery_sources_are_exact_candidate_refs_not_revision_expressions(history, source):
    serving, data, ctx, candidate = history
    before = git(candidate, "write-tree")
    assert "⚠️" in _candidate_merge(ctx, source=source)
    assert git(candidate, "write-tree") == before
    assert git(candidate, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""


def test_recovered_protected_change_keeps_authored_review_boundary(history, monkeypatch):
    import time
    from ouroboros.tools import git as owner
    from ouroboros.tools.git_review_cycle import _accepted_merge_refs, _fingerprint_staged_diff
    serving, data, ctx, candidate = history
    donor = make_ctx(serving, data, "protected-donor")
    row = body_candidate.prepare(donor)
    tip = candidate_commit(pathlib.Path(row["path"]), files={"BIBLE.md": "unaccepted change\n"})
    assert "NOT committed" in _candidate_merge(ctx, source=row["branch"])
    assert _accepted_merge_refs(ctx) == []
    fingerprint = _fingerprint_staged_diff(candidate)
    assert fingerprint["binding"]["parents"] == [git(candidate, "rev-parse", "HEAD"), tip]
    monkeypatch.setattr(owner, "_current_runtime_mode", lambda: "advanced")
    outcome = owner._run_reviewed_stage_cycle(ctx, "Import", time.time(), paths=["BIBLE.md"])
    assert outcome["block_reason"] == "core_protection_blocked", outcome
    assert git(candidate, "rev-parse", "MERGE_HEAD") == tip


def test_recovery_merge_adoption_requires_review_of_the_importing_commit(history):
    from ouroboros import body_adoption
    serving, data, ctx, candidate = history
    donor = make_ctx(serving, data, "raw-history")
    row = body_candidate.prepare(donor)
    tip = candidate_commit(pathlib.Path(row["path"]), files={"recovered.txt": "unreviewed donor\n"})
    assert "NOT committed" in _candidate_merge(ctx, source=row["branch"])
    git(candidate, "commit", "-m", "imported merge fixture")
    merged = git(candidate, "rev-parse", "HEAD")
    assert git(candidate, "rev-list", "--parents", "-n", "1", merged).split() == [merged, row["base_sha"], tip]
    with pytest.raises(body_adoption.AdoptionRefused) as refused:
        body_adoption.authorize(ctx, merged, reason="fixture")
    assert refused.value.code == "ADOPTION_UNREVIEWED"
    # The reviewed importing merge covers second-parent content; no false donor PASS.
    body_candidate.record_reviewed_commit(ctx, merged)
    handoff = body_adoption.authorize(ctx, merged, reason="fixture")
    assert handoff["cand"] == merged and handoff["unreviewed_commits"] == []
    assert git(serving, "rev-parse", "HEAD") == row["base_sha"]
    body_adoption.abandon(data, "fixture ended before switch")


@pytest.mark.parametrize("keep_row", [False, True])
def test_bound_prepare_recovers_missing_checkout_and_reports_previous_branch(history, keep_row):
    import shutil
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    tip = candidate_commit(candidate, files={"lost.txt": "preserved history\n"})
    previous = body_candidate.descriptor(ctx)["branch"]
    shutil.rmtree(candidate)  # Same bound context, with and without its registry row.
    if not keep_row:
        body_candidate._forget_missing(body_candidate.find(ctx.task_id))
    registry = _registry(serving, data, ctx)
    result = registry.execute("prepare_self_change", {})
    assert pathlib.Path(ctx.repo_dir).is_dir(), result
    assert previous in result and body_candidate.descriptor(ctx)["branch"] != previous
    assert "NOT committed" in registry.execute("candidate_merge", {"source": previous})
    assert git(ctx.repo_dir, "rev-parse", "MERGE_HEAD") == tip
    assert (pathlib.Path(ctx.repo_dir) / "lost.txt").read_text() == "preserved history\n"


@pytest.mark.parametrize("foreign_staged", [False, True])
def test_history_only_merge_has_no_attributed_file_but_keeps_its_parent(history, foreign_staged):
    from ouroboros.mutation_attribution import capture_mutation_baseline
    from ouroboros.tools.git import _task_attributed_commit_paths
    serving, data, ctx, candidate = history
    row = body_candidate.prepare(make_ctx(serving, data, "equal-tree"))
    tip = candidate_commit(pathlib.Path(row["path"]), files={"same.txt": "same\n"})
    candidate_commit(candidate, "independent identical content", files={"same.txt": "same\n"})
    if foreign_staged:
        (candidate / "foreign.txt").write_text("not mine\n")
    capture_mutation_baseline(data, ctx.task_id, [{"surface_type": "system_repo", "host_root": str(candidate)}])
    assert "NOT committed" in _candidate_merge(ctx, source=row["branch"])
    if foreign_staged:
        git(candidate, "add", "foreign.txt")
    index = git(candidate, "write-tree")
    paths, evidence, error, _ = _task_attributed_commit_paths(ctx, None)
    assert paths == [] and evidence["candidates"] == []
    if foreign_staged:
        assert "GIT_NO_ATTRIBUTED_CHANGES" in error
        assert evidence["excluded_preexisting_dirty"] == ["foreign.txt"]
    else:
        assert error == "", error
        assert git(candidate, "status", "--porcelain") == ""
    assert git(candidate, "write-tree") == index
    assert git(candidate, "rev-parse", "MERGE_HEAD") == tip


@pytest.mark.parametrize("mode", ["advanced", "pro"])
@pytest.mark.parametrize("kind", ["branch", "pin"])
@pytest.mark.parametrize("path", ["BIBLE.md", "ouroboros/tools/registry_core.py", "ouroboros/contracts/example.py", "recovered.txt"])
def test_public_recovery_protection_runs_before_staging(history, monkeypatch, mode, kind, path):
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    donor = make_ctx(serving, data, "public-protected-donor")
    row = body_candidate.prepare(donor)
    donor_path = pathlib.Path(row["path"])
    tip = candidate_commit(donor_path, files={path: "source = 'unaccepted'\n"})
    source = row["branch"]
    if kind == "pin":
        source = body_candidate._preserve(row, body_candidate.unique_work(row))
        tip = git(serving, "rev-parse", source)
    head, index = git(candidate, "rev-parse", "HEAD"), git(candidate, "write-tree")
    before = (candidate / path).read_bytes() if (candidate / path).exists() else None
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)  # BEFORE public dispatch, not only commit.
    result = _registry(serving, data, ctx).execute_result("candidate_merge", {"source": source})
    if mode == "advanced" and path != "recovered.txt":
        assert result.status == "blocked" and result.code == "CORE_PROTECTION_BLOCKED", result
        assert git(candidate, "write-tree") == index
        assert git(candidate, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
        assert ((candidate / path).read_bytes() if (candidate / path).exists() else None) == before
    else:
        assert result.status == "ok" and "NOT committed" in result.text, result
        assert git(candidate, "rev-parse", "MERGE_HEAD") == tip
        assert (candidate / path).read_text() == "source = 'unaccepted'\n"
    assert git(candidate, "rev-parse", "HEAD") == head
    assert git(serving, "rev-parse", source) == tip


def test_advanced_default_serving_sync_keeps_accepted_protected_source(history, monkeypatch):
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    accepted = peer(serving, "BIBLE.md", "accepted protected source\n")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    result = _registry(serving, data, ctx).execute_result("candidate_merge", {})
    assert result.status == "ok" and "NOT committed" in result.text, result
    assert git(candidate, "rev-parse", "MERGE_HEAD") == accepted
    assert (candidate / "BIBLE.md").read_text() == "accepted protected source\n"


def test_advanced_import_keeps_converged_protected_and_recipient_only_changes(history, monkeypatch):
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    row = body_candidate.prepare(make_ctx(serving, data, "converged-protected"))
    candidate_commit(pathlib.Path(row["path"]), files={"BIBLE.md": "identical\n", "incoming.txt": "new\n"})
    candidate_commit(candidate, files={"BIBLE.md": "identical\n", "ouroboros/tools/registry_core.py": "recipient_only = True\n"})
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    result = _registry(serving, data, ctx).execute_result("candidate_merge", {"source": row["branch"]})
    assert result.status == "ok" and "NOT committed" in result.text, result
    assert (candidate / "BIBLE.md").read_text() == "identical\n"
    assert (candidate / "ouroboros/tools/registry_core.py").read_text() == "recipient_only = True\n"
    assert (candidate / "incoming.txt").read_text() == "new\n"


def test_import_path_observation_failure_precedes_git_effect(history, monkeypatch):
    import subprocess
    from ouroboros.tools import git_pr
    from tests.test_body_candidate import _registry
    serving, data, ctx, candidate = history
    row = body_candidate.prepare(make_ctx(serving, data, "unreadable-delta"))
    candidate_commit(pathlib.Path(row["path"]), files={"incoming.txt": "new\n"})
    original = git_pr._g
    def observe(args, cwd, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", b"unreadable") if args[0] == "diff" else original(args, cwd, **kwargs)
    monkeypatch.setattr(git_pr, "_g", observe)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    before = git(candidate, "write-tree")
    result = _registry(serving, data, ctx).execute_result("candidate_merge", {"source": row["branch"]})
    assert "source paths unavailable" in result.text
    assert git(candidate, "write-tree") == before
    assert git(candidate, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
    assert not (candidate / "incoming.txt").exists()


@pytest.mark.parametrize("shape", ["add", "modify", "staged_unstaged_split", "mode", "delete", "rename"])
def test_preexisting_staged_foreign_entry_refused_without_reset_across_shapes(candidate, monkeypatch, shape):  # noqa: F811
    from ouroboros.mutation_attribution import attributed_git_candidates, capture_mutation_baseline
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    ctx.branch_dev = git(repo, "branch", "--show-current")
    (repo / "VERSION").write_bytes(b"1.0.0\n")
    (repo / "foreign.txt").write_bytes(b"base foreign\n")
    git(repo, "add", "VERSION", "change.py", "foreign.txt")
    git(repo, "commit", "-m", "base with foreign")
    head_before = git(repo, "rev-parse", "HEAD")
    if shape == "add":
        (repo / "foreign_new.txt").write_bytes(b"preexisting new\n")
        git(repo, "add", "foreign_new.txt")
        expected_foreign = ["foreign_new.txt"]
    elif shape == "modify":
        (repo / "foreign.txt").write_bytes(b"staged foreign\n")
        git(repo, "add", "foreign.txt")
        expected_foreign = ["foreign.txt"]
    elif shape == "staged_unstaged_split":
        (repo / "foreign.txt").write_bytes(b"staged hunk\n")
        git(repo, "add", "foreign.txt")
        (repo / "foreign.txt").write_bytes(b"worktree edit\n")
        expected_foreign = ["foreign.txt"]
    elif shape == "mode":
        git(repo, "config", "core.filemode", "false")
        git(repo, "update-index", "--chmod=+x", "foreign.txt")
        expected_foreign = ["foreign.txt"]
    elif shape == "delete":
        git(repo, "rm", "foreign.txt")
        expected_foreign = ["foreign.txt"]
    else:
        git(repo, "mv", "foreign.txt", "foreign_renamed.txt")
        expected_foreign = ["foreign.txt", "foreign_renamed.txt"]
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id, [{"surface_type": "system_repo", "host_root": str(repo)}])
    (repo / "change.py").write_bytes(b"value = 42\n")
    index_before = git(repo, "write-tree")
    monkeypatch.setattr("ouroboros.commit_admission.auto_sync_release_metadata_if_needed",
                        lambda *a, **kw: pytest.fail("must not auto-sync before refusing foreign staged entries"))
    monkeypatch.setattr(owner, "_run_parallel_review", lambda *a, **kw: pytest.fail("foreign staged must not reach review"))
    refused = owner._repo_commit_push(ctx, "Task commit", skip_advisory_review=True, skip_tests=True)
    assert "GIT_ATTRIBUTION_BLOCKED" in refused
    for path in expected_foreign:
        assert path in refused
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "write-tree") == index_before
    # Non-committing review cycle must also preserve preexisting staged entries in its finally block.
    review_only = owner._run_non_committing_review_cycle(ctx, "Review only", paths=["change.py"], skip_advisory_review=True)
    assert review_only["status"] == "blocked" and "GIT_ATTRIBUTION_BLOCKED" in review_only["message"]
    assert git(repo, "write-tree") == index_before
    if shape == "staged_unstaged_split":
        assert git(repo, "show", ":foreign.txt") == "staged hunk"
        assert (repo / "foreign.txt").read_bytes() == b"worktree edit\n"
    elif shape == "mode":
        assert git(repo, "ls-files", "--stage", "--", "foreign.txt").startswith("100755 ")
    elif shape == "delete":
        assert git(repo, "diff", "--cached", "--diff-filter=D", "--name-only") == "foreign.txt"
    elif shape == "rename":
        assert set(git(repo, "diff", "--cached", "--no-renames", "--name-only").splitlines()) == {"foreign.txt", "foreign_renamed.txt"}
    evidence = attributed_git_candidates(ctx.drive_root, ctx.task_id, repo)
    assert evidence["blockers"] == []
    assert evidence["candidates"] == ["change.py"]
    assert evidence["excluded_preexisting_dirty"] == sorted(expected_foreign)


@pytest.mark.parametrize("shape", ["untracked", "modify", "delete", "mode"])
def test_preexisting_unstaged_foreign_work_stays_outside_committed_tree(candidate, monkeypatch, shape):  # noqa: F811
    import os
    from ouroboros.mutation_attribution import attributed_git_candidates, capture_mutation_baseline
    from ouroboros.review_ledger import CouplingOutcome
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    ctx.branch_dev = git(repo, "branch", "--show-current")
    (repo / "VERSION").write_bytes(b"1.0.0\n")
    (repo / "foreign.txt").write_bytes(b"base foreign\n")
    git(repo, "add", "VERSION", "change.py", "foreign.txt")
    git(repo, "commit", "-m", "base with foreign")
    if shape == "untracked":
        (repo / "foreign_untracked.txt").write_bytes(b"owner draft\n")
        expected_foreign = ["foreign_untracked.txt"]
    elif shape == "modify":
        (repo / "foreign.txt").write_bytes(b"owner edit\n")
        expected_foreign = ["foreign.txt"]
    elif shape == "delete":
        (repo / "foreign.txt").unlink()
        expected_foreign = ["foreign.txt"]
    else:
        os.chmod(repo / "foreign.txt", 0o755)
        expected_foreign = ["foreign.txt"]
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id, [{"surface_type": "system_repo", "host_root": str(repo)}])
    (repo / "change.py").write_bytes(b"value = 7\n")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setattr(owner, "_run_review_preflight_tests", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_post_commit_result", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_auto_push", lambda *a, **kw: "")

    def reviewer(_ctx, _msg, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        assert git(repo, "diff", "--cached", "--name-only").splitlines() == ["change.py"]
        ctx._last_triad_raw_results = [{"slot_id": "critic", "status": "responded", "parsed": [], "raw_text": "[]"}]
        return None, CouplingOutcome(verdict="PASS", status="responded"), "", []

    monkeypatch.setattr(owner, "_run_parallel_review", reviewer)
    result = owner._repo_commit_push(ctx, "Task only", skip_advisory_review=True)
    assert result.startswith("OK: committed"), result
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == ["change.py"]
    evidence = attributed_git_candidates(ctx.drive_root, ctx.task_id, repo)
    assert evidence["blockers"] == [] and evidence["excluded_preexisting_dirty"] == expected_foreign
    if shape == "untracked":
        assert (repo / "foreign_untracked.txt").read_bytes() == b"owner draft\n"
    elif shape == "modify":
        assert (repo / "foreign.txt").read_bytes() == b"owner edit\n"
    elif shape == "delete":
        assert not (repo / "foreign.txt").exists()


def test_resumed_review_consumers_keep_foreign_staged_work_out_of_commits(candidate, monkeypatch):  # noqa: F811
    import json
    from ouroboros.mutation_attribution import capture_mutation_baseline
    from ouroboros.review_ledger import CouplingOutcome
    from ouroboros.task_results import write_task_result
    from ouroboros.tools import git as owner

    ctx, repo = candidate, candidate.repo_dir
    ctx.branch_dev = git(repo, "branch", "--show-current")
    (repo / "VERSION").write_bytes(b"1.0.0\n")
    git(repo, "add", "VERSION", "change.py")
    git(repo, "commit", "-m", "base")
    (repo / "foreign.txt").write_bytes(b"owner wip\n")
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id, [{"surface_type": "system_repo", "host_root": str(repo)}])
    (repo / "change.py").write_bytes(b"value = 9\n")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(owner, "_run_review_preflight_tests", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_post_commit_result", lambda *a, **kw: None)
    monkeypatch.setattr(owner, "_auto_push", lambda *a, **kw: "")

    mode = {"pending": True}

    def reviewer(inner, _msg, **kw):
        from ouroboros.review_custody import prepare_frozen_review_reconciliation
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        if inner._review_reconcile_only:
            prepare_frozen_review_reconciliation(inner, inner._pending_review_attempt)
        else:
            invoke_review_paid_stamp(inner._review_paid_stamp)
        if mode["pending"]:
            inner._last_triad_raw_results = [{
                "slot_id": "triad-one", "operation_id": "pending-op", "model_id": "test/model",
                "status": "error", "operation_state": "in_flight", "late_result_pending": True,
                "pending_invocation_id": "inv-1", "error": "still pending",
            }]
            return "review pending", None, "review_late_result_pending", []
        inner._last_triad_raw_results = [{
            "slot_id": "triad-one", "operation_id": "pending-op", "model_id": "test/model",
            "status": "responded", "parsed": [], "raw_text": "[]",
        }]
        return None, CouplingOutcome(verdict="PASS", status="responded"), "", []

    monkeypatch.setattr(owner, "_run_parallel_review", reviewer)
    # 1. Start a pending review while foreign.txt is unstaged; index retains only change.py.
    first = owner._repo_commit_push(ctx, "Resumed commit", skip_advisory_review=True)
    assert "reviewers are still running" in first, first
    pending_ref = json.loads(first.split("\n", 1)[1])["review_reference"]
    assert git(repo, "diff", "--cached", "--name-only").splitlines() == ["change.py"]
    # 2. If foreign.txt is staged while pending, both automatic retry and explicit reference collection refuse without resetting index.
    git(repo, "add", "foreign.txt")
    polluted_index = git(repo, "write-tree")
    refused_retry = owner._repo_commit_push(ctx, "Resumed commit", skip_advisory_review=True)
    assert "REVIEWED_ATTEMPT_IN_PROGRESS" in refused_retry, refused_retry
    assert git(repo, "write-tree") == polluted_index
    refused_collect = owner._repo_commit_push(ctx, "Resumed commit", review_reference=pending_ref)
    assert "REVIEW_BINDING_BLOCKED" in refused_collect, refused_collect
    assert git(repo, "write-tree") == polluted_index
    assert git(repo, "show", ":foreign.txt") == "owner wip"
    # 3. Unstaging foreign.txt restores the exact pending index; collection settles to advisory handback.
    git(repo, "reset", "HEAD", "--", "foreign.txt")
    mode["pending"] = False
    settled = owner._repo_commit_push(ctx, "Resumed commit", review_reference=pending_ref)
    assert settled.startswith("Review outcome returned before commit."), settled
    settled_ref = json.loads(settled.split("\n", 1)[1])["review_reference"]
    # 4. If foreign.txt is staged before author continuation, continuation refuses without resetting or committing.
    git(repo, "add", "foreign.txt")
    head_before = git(repo, "rev-parse", "HEAD")
    staged_before = git(repo, "write-tree")
    refused_cont = owner._repo_commit_push(
        ctx, "Resumed commit", review_reference=settled_ref,
        author_disposition={"disposition": "accepted", "rationale": "Inspected."},
    )
    assert "GIT_ATTRIBUTION_BLOCKED" in refused_cont and "foreign.txt" in refused_cont
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "write-tree") == staged_before
    assert git(repo, "show", ":foreign.txt") == "owner wip"
    # 5. Once unstaged, author continuation commits only change.py and preserves unstaged foreign.txt.
    git(repo, "reset", "HEAD", "--", "foreign.txt")
    landed = owner._repo_commit_push(
        ctx, "Resumed commit", review_reference=settled_ref,
        author_disposition={"disposition": "accepted", "rationale": "Inspected."},
    )
    assert landed.startswith("OK: committed"), landed
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == ["change.py"]
    assert (repo / "foreign.txt").read_bytes() == b"owner wip\n"
