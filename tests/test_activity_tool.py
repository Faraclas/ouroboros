"""Public registry/replica seams, not direct-handler-only assessment tests."""
import json
import pytest
from ouroboros.tools.registry import ToolContext, ToolRegistry
from tests._store_shared import seed_result


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ouroboros.config as config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="integrator", is_direct_chat=True)
    tools = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    tools.set_context(ctx)
    return tmp_path, ctx, tools


def test_frozen_registry_contains_assessment_module(world, monkeypatch):
    root, _ctx, _tools = world
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert "assess_activity" in ToolRegistry(repo_dir=root, drive_root=root)._entries


def test_missing_task_is_not_a_successful_assessment(world):
    _root, _ctx, tools = world
    result = tools.execute_result("assess_activity", {"task_id": "absent"})
    assert result.status == "unavailable", result
    assert result.code == "ACTIVITY_NOT_FOUND"


def test_actual_replica_occurrence_has_the_same_public_inspection_and_write_source(world):
    from ouroboros.activity_history import room_activity_page
    root, _ctx, tools = world
    replica = root / "replica"
    seed_result(root, "observed", {"status": "running", "chat_id": 1, "child_drive_root": str(replica)})
    seed_result(replica, "observed", {"status": "failed", "reason_code": "runtime_failure", "chat_id": 1,
        "result": "Actual failure details and retained answer body",
        "service_observations": {"services": [{"service_id": "observed:s", "name": "Replica service",
          "started_at": "2026-01-01T00:00:00Z", "state": "exited", "returncode": 7}]}})
    public = tools.execute_result("assess_activity", {"task_id": "observed"})
    assert public.status == "ok", public.text
    listed = json.loads(public.text)["entries"]
    history = room_activity_page(root, 1)["tasks"][0]["entries"]
    assert [(e["key"], e["source_hash"]) for e in listed] == [(e["key"], e["source_hash"]) for e in history]
    for entry in history:
        result = tools.execute_result("assess_activity", {"task_id": "observed", "activity_key": entry["key"],
            "source_hash": entry["source_hash"], "reason": "Read this exact retained occurrence"})
        assert result.status == "ok", result.text
    assert all("assessment" in e for e in room_activity_page(root, 1)["tasks"][0]["entries"])
    stale = tools.execute_result("assess_activity", {"task_id": "observed", "activity_key": history[0]["key"],
        "source_hash": "0" * 64, "reason": "Stale source"})
    assert stale.status == "blocked" and stale.code == "ACTIVITY_ASSESSMENT_REFUSED"


@pytest.mark.parametrize("author", [None, "", "  "])
def test_missing_actor_id_cannot_be_recorded_as_a_synthetic_author(world, author):
    from ouroboros.task_results import load_task_result
    root, ctx, tools = world
    seed_result(root, "failed", {"status": "failed", "chat_id": 1})
    ctx.task_id = author
    result = tools.execute_result("assess_activity", {"task_id": "failed", "activity_key": "task:failed",
        "source_hash": "0" * 64, "reason": "No attributable writer"})
    assert result.status == "blocked" and result.code == "ACTIVITY_ASSESSMENT_REFUSED"
    assert not load_task_result(root, "failed").get("activity_assessments")


@pytest.mark.parametrize("mode,parent", [("local_readonly_subagent", True), ("acting_subagent", True), ("local_readonly_subagent", False)])
def test_child_and_readonly_root_cannot_mutate_assessments(world, mode, parent):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.task_results import load_task_result
    root, ctx, tools = world
    seed_result(root, "failed", {"status": "failed", "chat_id": 1})
    ctx.is_direct_chat = not parent
    if parent:
        ctx.task_metadata = {"parent_task_id": "parent", "delegation_role": "subagent"}
    ctx.task_constraint = TaskConstraint(mode=mode, surface="self_worktree" if mode == "acting_subagent" else "",
                                        write_root=str(root) if mode == "acting_subagent" else "")
    result = tools.execute_result("assess_activity", {"task_id": "failed", "activity_key": "task:failed",
        "source_hash": "0" * 64, "reason": "Not authorized"})
    assert result.status != "ok", result.text
    assert not load_task_result(root, "failed").get("activity_assessments")


@pytest.mark.parametrize("frozen", [False, True])
def test_genuinely_fresh_interpreter_discovers_and_reads_persisted_judgment(world, frozen):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root, _ctx, tools = world
    seed_result(root, "cold", {"status": "failed", "chat_id": 1})
    entry = json.loads(tools.execute_result("assess_activity", {"task_id": "cold"}).text)["entries"][0]
    result = tools.execute_result("assess_activity", {"task_id": "cold", "activity_key": entry["key"],
        "source_hash": entry["source_hash"], "reason": "Persisted exact-source evaluation"})
    assert result.status == "ok", result.text
    repo = Path(__file__).resolve().parents[1]
    code = '''import sys,json,pathlib
repo,root=map(pathlib.Path,sys.argv[1:3]);sys.path.insert(0,str(repo))
if sys.argv[3]=='1':sys.frozen=True
from ouroboros.tools.registry import ToolContext,ToolRegistry
import ouroboros.tools.activity as owner
assert pathlib.Path(owner.__file__).resolve()==repo/'ouroboros/tools/activity.py'
t=ToolRegistry(repo,root);t.set_context(ToolContext(repo_dir=repo,drive_root=root,task_id='new-reader',is_direct_chat=True))
r=t.execute_result('assess_activity',{'task_id':'cold'});assert r.status=='ok',r.text
entry=json.loads(r.text)['entries'][0]
assert entry['assessment']['reason']=='Persisted exact-source evaluation'
assert entry['assessment']['by']['task_id']=='integrator'
print(json.dumps({'source_hash':entry['source_hash'],'status':r.status,'module':str(owner.__file__)}))
'''
    env = {**os.environ, "OUROBOROS_DATA_DIR": str(root), "OUROBOROS_SETTINGS_PATH": str(root / "settings.json")}
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(repo), str(root), "1" if frozen else "0"],
                               env=env, cwd=repo, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["source_hash"] == entry["source_hash"]
