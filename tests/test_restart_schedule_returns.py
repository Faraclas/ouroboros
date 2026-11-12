"""Acknowledged running follow-ups continue saved work, not the occurrence's first launch."""
from types import SimpleNamespace

import pytest

from ouroboros import working_checkpoint as wc
from ouroboros.task_results import load_task_result
from supervisor.events_budget import budget_hold_fact
from tests._budget_pause_exact_helpers import _install_queue, _loop_ctx
from tests.test_restart_retention import _pool_events
from tests.test_restart_saved_work import _ack, _stop_and_boot
from tests.test_schedule_occurrence import _row
from tests.test_worker_crash_retry import _make_worker
from tests.worker_pool_support import affirm_ready

pytestmark = pytest.mark.serial


def _running_followup(tmp_path, monkeypatch):
    from ouroboros.projects_registry import create_project
    from supervisor import workers

    root = tmp_path / "data"
    root.mkdir()
    queue, state, workers = _install_queue(root, monkeypatch)
    _pool_events(workers, monkeypatch)
    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.setattr(workers, "REPO_DIR", tmp_path / "repo")
    monkeypatch.setattr("ouroboros.config.get_bg_wakeup_min_sec", lambda: 0)
    project = create_project(root, "return-room", name="Return room", working_dir=str(folder))
    q = SimpleNamespace(queue=queue, root=root)
    _row(q, project_id="return-room", chat_id=project["chat_id"],
         intent={"kind": "room_default", "project_id": "return-room"})
    queue.check_scheduled_tasks()
    [task] = workers.PENDING
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 10.0)
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1
    assert task["id"] in workers.RUNNING
    task = workers.RUNNING[task["id"]]["task"]
    ctx, limit = _loop_ctx(root, task["id"])
    ctx.task_metadata = task["metadata"]
    wc.save_round(limit, "pre_effect")
    assert load_task_result(root, task["id"])["admitted_dispatch"] == "possible"
    return root, queue, workers, task, worker


@pytest.mark.parametrize("door", ["planned", "owner_restart"])
def test_acknowledged_running_project_followup_returns_to_saved_loop(tmp_path, monkeypatch, door):
    from supervisor.restart_retention import prepare_restart_returns
    from tests._work_stop_helpers import live_restart

    root, queue, workers, task, worker = _running_followup(tmp_path, monkeypatch)
    tid = task["id"]
    if door == "owner_restart":
        live_restart(root, monkeypatch)
    assert prepare_restart_returns(root, workers.RUNNING, workers.PENDING,
                                   transaction_id="return-followup") == {tid}
    _ack(root, "return-followup")
    _stop_and_boot(queue, workers)
    [restored] = workers.PENDING
    assert restored["id"] == tid and restored["_attempt"] == 2
    assert budget_hold_fact(restored) is None
    source = restored["_working_recovery"]
    assert source["source_task_id"] == tid and source["from_attempt"] == 1
    ctx, _ = _loop_ctx(root, tid, attempt=2)
    saved = wc.load_recovery(ctx, source)
    assert saved["working"]["boundary"] == "pre_effect"
    assert saved["messages"][-1] == {"role": "tool", "tool_call_id": "call_a", "content": "done a"}
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1
    sent = worker.in_q.put.call_args.args[0]
    assert sent["id"] == tid and sent["_working_recovery"] == source
    # The already-dispatched occurrence must never create a second root/launch.
    queue.check_scheduled_tasks()
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1 and not workers.PENDING
    assert set(workers.RUNNING) == {tid}


@pytest.mark.parametrize("control", ["unacknowledged", "consumed", "panic", "stop", "bad_source", "prior_hold", "changed_project"])
def test_running_followup_keeps_independent_controls(tmp_path, monkeypatch, control):
    from ouroboros import delegate_recovery as dr
    from ouroboros.cancel_intents import request_cancel
    from ouroboros.projects_registry import begin_project_deletion
    from supervisor.events_budget import HOLD_OWNER_RESTART, hold_budget_row
    from supervisor.restart_retention import prepare_restart_returns

    root, queue, workers, task, _ = _running_followup(tmp_path, monkeypatch)
    tid = task["id"]
    assert prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="controlled") == {tid}
    if control != "unacknowledged":
        _ack(root, "controlled")
    if control == "consumed":
        tx = dr._read_restart_transaction(root, "controlled")
        dr._write_restart_transaction(root, {**tx, "returns_restored_at": "2026-01-01T00:00:00+00:00"})
    elif control == "panic":
        (root / "state/panic_stop.flag").write_text("panic", encoding="utf-8")
    elif control == "stop":
        request_cancel(root, tid, reason="stop", source="owner", requested_by="owner")
    elif control == "prior_hold":
        hold_budget_row(task, reason=HOLD_OWNER_RESTART, result_root=root)
    elif control == "changed_project":
        begin_project_deletion(root, "return-room")
    elif control == "bad_source":
        wc.checkpoint_path(root, tid, 1).write_bytes(b"broken")
    _stop_and_boot(queue, workers)
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 0
    assert tid not in workers.RUNNING
    if control in {"unacknowledged", "consumed", "panic", "prior_hold"}:
        [restored] = workers.PENDING
        assert budget_hold_fact(restored)
    if control == "prior_hold":
        import json
        rows = [json.loads(line) for line in (root / "logs/supervisor.jsonl").read_text().splitlines()]
        assert any(row.get("type") == "restart_return_refused" and tid in row["refused"] for row in rows)


def test_a_pending_saved_followup_returns_without_replaying_its_occurrence(tmp_path, monkeypatch):
    from supervisor.restart_retention import prepare_restart_returns

    root, queue, workers, task, _ = _running_followup(tmp_path, monkeypatch)
    tid = task["id"]
    prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="first")
    _ack(root, "first")
    _stop_and_boot(queue, workers)
    [saved] = workers.PENDING
    source = saved["_working_recovery"]
    assert prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="second") == set()
    _ack(root, "second")
    _stop_and_boot(queue, workers)
    [again] = workers.PENDING
    assert again["id"] == tid and again["_attempt"] == 2
    assert again["_working_recovery"] == source and budget_hold_fact(again) is None


def test_running_followup_continues_across_stopped_interpreters(tmp_path):
    """Real saved loop/receipt/restore across process exit; worker transport/model are scripted."""
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    program = r'''
import json, os, sys
from pathlib import Path
import pytest
from tests.test_restart_schedule_returns import _running_followup
from tests.test_restart_saved_work import _ack
from tests._budget_pause_exact_helpers import _install_queue
from tests.test_restart_retention import _pool_events
from tests.test_worker_crash_retry import _make_worker
from tests.worker_pool_support import affirm_ready
from supervisor.events_budget import budget_hold_fact
from supervisor.restart_retention import prepare_restart_returns
base, stage = Path(sys.argv[1]), sys.argv[2]
patch = pytest.MonkeyPatch()
if stage == "save":
    root, queue, workers, task, worker = _running_followup(base, patch)
    tid = task["id"]
    assert prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="two-process") == {tid}
    _ack(root, "two-process")
    workers.kill_workers(force=True, terminal_status="cancelled", result_reason="Server shutdown.",
                         stop_source="server_shutdown", retain_saved_work=True)
    (base / "first.json").write_text(json.dumps({"id": tid, "pid": os.getpid(),
        "occurrence": task["metadata"]["schedule_occurrence"]}), encoding="utf-8")
else:
    from ouroboros import loop
    from tests.test_working_checkpoint import _registry
    from tests.test_loop_transport_wait import _loop_kwargs
    root = base / "data"
    queue, state, workers = _install_queue(root, patch)
    _pool_events(workers, patch)
    patch.setattr(workers, "REPO_DIR", base / "repo")
    patch.setattr(state, "budget_remaining", lambda *_a, **_k: 10.0)
    first = json.loads((base / "first.json").read_text(encoding="utf-8"))
    assert os.getpid() != first["pid"]
    queue.restore_pending_from_snapshot()
    [task] = workers.PENDING
    assert task["id"] == first["id"] and task["_attempt"] == 2
    assert budget_hold_fact(task) is None
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1
    sent = worker.in_q.put.call_args.args[0]
    registry = _registry(root, patch, sent["id"], 2)
    registry._ctx.working_recovery = sent["_working_recovery"]
    seen = []
    def no_effect(*a, **k):
        raise AssertionError("saved tool/provider must not be re-executed")
    patch.setattr("ouroboros.llm.LLMClient.chat", no_effect)
    turns = []
    def dispatch(call, disposition, **kwargs):
        turns.append(1)
        assert len(turns) <= 2, repr(call.messages[-4:])
        if len(turns) == 1:
            seen.extend(call.messages)
            return {"role": "assistant", "content": "Continued once", "tool_calls": []}, 0.0
        observation = next(row for row in reversed(call.messages) if row.get("acceptance_observation"))
        subject = json.loads(observation["content"].splitlines()[1])
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "finish", "type": "function",
            "function": {"name": "finish_task", "arguments": json.dumps({"action": "finish", "answer": "Continued once",
                "acceptance_subject": {"owner_source_sha256": subject["owner_source_sha256"],
                                       "effective_criteria": "Continue saved work without repeating effects"}})}}]}, 0.0
    patch.setattr(loop, "_dispatch_round_model", dispatch)
    kwargs = _loop_kwargs(root, registry, [])
    kwargs["task_id"] = sent["id"]
    kwargs["emit_progress"] = lambda *a, **k: None
    answer, usage, trace = loop.run_llm_loop(**kwargs)
    assert answer == "Continued once"
    assert any(row.get("content") == "done a" for row in seen)
    unknown = [row for row in seen if row.get("tool_call_id") == "call_b"]
    assert len(unknown) == 1 and "UNKNOWN" in unknown[0]["content"] and "NOT re-executed" in unknown[0]["content"]
    queue.check_scheduled_tasks()
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1 and not workers.PENDING
    assert sent["metadata"]["schedule_occurrence"] == first["occurrence"]
    print("CONTINUED_ONCE_WITHOUT_REPLAY")
patch.undo()
'''
    for stage in ("save", "restore"):
        result = subprocess.run([sys.executable, "-c", program, str(tmp_path), stage],
                                cwd=Path(__file__).resolve().parents[1], env={**os.environ},
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        if stage == "restore":
            assert "CONTINUED_ONCE_WITHOUT_REPLAY" in result.stdout


def test_saved_return_read_contention_keeps_recheckable_custody(tmp_path, monkeypatch):
    from ouroboros.store import StoreBusy
    from supervisor import task_admission
    from supervisor.restart_retention import prepare_restart_returns

    root, queue, workers, task, _ = _running_followup(tmp_path, monkeypatch)
    tid = task["id"]
    prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="busy-return")
    _ack(root, "busy-return")
    original = task_admission._working_resume_granted
    busy = [True]
    def grant(*a, **k):
        if busy[0]:
            raise StoreBusy("disposable continuation read contention")
        return original(*a, **k)
    monkeypatch.setattr(task_admission, "_working_resume_granted", grant)
    _stop_and_boot(queue, workers)
    [saved] = workers.PENDING
    assert budget_hold_fact(saved) is None and saved.get("_project_admission_restore_hold")
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 0
    busy[0] = False
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 1 and not workers.PENDING


@pytest.mark.parametrize("invalid", ["target_dispatched", "bad_source"])
def test_pending_return_never_admits_an_invalid_working_successor(tmp_path, monkeypatch, invalid):
    from ouroboros.artifacts import task_artifact_dir_path
    from ouroboros.task_results import write_task_result
    from supervisor.restart_retention import prepare_restart_returns

    root, queue, workers, task, _ = _running_followup(tmp_path, monkeypatch)
    tid = task["id"]
    prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="saved")
    _ack(root, "saved")
    _stop_and_boot(queue, workers)
    [saved] = workers.PENDING
    prepare_restart_returns(root, workers.RUNNING, workers.PENDING, transaction_id="queued-saved")
    _ack(root, "queued-saved")
    if invalid == "target_dispatched":
        write_task_result(root, tid, "running", admitted_dispatch="possible", admitted_dispatch_attempt=2, task_attempt=2)
    else:
        ref = saved["_working_recovery"]["source_ref"]
        (task_artifact_dir_path(root, tid) / ref["path"]).write_bytes(b"corrupt")
    _stop_and_boot(queue, workers)
    worker = affirm_ready(_make_worker(alive=True, busy_task_id=None, exitcode=None))
    workers.WORKERS[0] = worker
    workers.assign_tasks()
    assert worker.in_q.put.call_count == 0
    assert workers.PENDING[0].get("_project_admission_restore_hold")
    assert budget_hold_fact(workers.PENDING[0]) is None, "source custody is not an independent owner Resume demand"
