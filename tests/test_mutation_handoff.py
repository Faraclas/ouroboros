"""Attribution belongs to a work identity, not to its direct or pooled carrier."""
from __future__ import annotations

import copy
import pathlib
from types import SimpleNamespace

import pytest

from ouroboros import body_candidate
from ouroboros.agent import OuroborosAgent
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools import git as git_tools
from tests.body_candidate_support import git, isolate, make_ctx, make_serving


def start(serving, data, task, *, canonical=None):
    agent = SimpleNamespace(env=SimpleNamespace(
        repo_dir=serving, drive_root=data, budget_drive_root=str(canonical or data)))
    OuroborosAgent._capture_mutation_baseline(agent, task, task.get("metadata", {}))


@pytest.mark.parametrize("forked", [False, True])
@pytest.mark.parametrize("carrier", ["direct", "presence"])
def test_inline_edits_survive_same_id_pooled_start(tmp_path, monkeypatch, forked, carrier):
    serving = make_serving(tmp_path)
    canonical = isolate(monkeypatch, tmp_path)
    execution = tmp_path / "fork" if forked else canonical
    execution.mkdir(exist_ok=True)
    task = {"id": "author", "root_task_id": "author", "_is_direct_chat": True,
            "budget_drive_root": str(canonical), "_execution_owner": {"kind": carrier}}
    write_task_result(canonical, "author", "running")
    # Foreign dirt at initial admission must not be attributed, even after a carrier change.
    (serving / "owner.txt").write_text("owner\n", encoding="utf-8")
    start(serving, execution, task, canonical=canonical)
    ctx = make_ctx(serving, execution, "author", task_metadata={
        "root_task_id": "author", "budget_drive_root": str(canonical), "work_kind": "owner"})
    body_candidate.prepare(ctx)
    candidate = pathlib.Path(ctx.repo_dir)
    (candidate / "ouroboros/mod_a.py").write_text("GEN = 'OWN'\n", encoding="utf-8")
    (candidate / "new.txt").write_text("new\n", encoding="utf-8")
    before = copy.deepcopy(load_task_result(canonical, "author").get("mutation_evidence"))
    # Serialization/new ToolContext mirrors cold restoration: no process-local attribution state.
    pooled = {**task, "_execution_owner": {"kind": "pooled"}}
    restored = make_ctx(serving, execution, "author", task_metadata=dict(ctx.task_metadata))
    assert body_candidate.restore(restored)["path"] == str(candidate)
    start(serving, execution, pooled, canonical=canonical)
    body_candidate.reattribute(restored)
    selected, evidence, error, binding = git_tools._task_attributed_commit_paths(restored, None)
    assert error == "", (selected, evidence, error)
    assert selected == ["new.txt", "ouroboros/mod_a.py"]
    assert binding == (canonical, "author")
    assert before == load_task_result(canonical, "author")["mutation_evidence"]
    assert (serving / "owner.txt").read_text(encoding="utf-8") == "owner\n"
    # Retry / repeated preparation must never turn own WIP into preexisting dirt.
    body_candidate.prepare(restored)
    start(serving, execution, pooled, canonical=canonical)
    assert git_tools._task_attributed_commit_paths(restored, None)[0] == selected
    assert before == load_task_result(canonical, "author")["mutation_evidence"]
    assert git(candidate, "diff", "--cached", "--name-only") == ""


def test_unbound_inline_preexisting_dirt_is_not_owned(tmp_path, monkeypatch):
    serving = make_serving(tmp_path)
    data = isolate(monkeypatch, tmp_path)
    task = {"id": "author", "_execution_owner": {"kind": "direct"}}
    write_task_result(data, "author", "running")
    (serving / "ouroboros/mod_b.py").write_text("GEN = 'FOREIGN'\n", encoding="utf-8")
    start(serving, data, task)
    (serving / "ouroboros/mod_a.py").write_text("GEN = 'OWN'\n", encoding="utf-8")
    start(serving, data, {**task, "_execution_owner": {"kind": "pooled"}})
    ctx = make_ctx(serving, data, "author")
    selected, evidence, error, _ = git_tools._task_attributed_commit_paths(ctx, None)
    assert error == "" and selected == ["ouroboros/mod_a.py"], evidence
    (serving / "ouroboros/mod_b.py").write_text("GEN = 'MIXED'\n", encoding="utf-8")
    assert "preexisting_dirty_changed" in git_tools._task_attributed_commit_paths(ctx, None)[2]


def test_subagent_start_does_not_author_a_root_baseline(tmp_path, monkeypatch):
    serving = make_serving(tmp_path)
    data = isolate(monkeypatch, tmp_path)
    task = {"id": "child", "_is_direct_chat": True, "metadata": {"delegation_role": "subagent"}}
    write_task_result(data, "child", "running")
    start(serving, data, task)
    assert not load_task_result(data, "child").get("mutation_evidence")
