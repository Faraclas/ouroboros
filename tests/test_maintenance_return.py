"""An explicit maintenance Pause returns only its selected saved work on boot."""
from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _install_queue


def _queued(root, workers, tid="maintenance"):
    from ouroboros.task_results import write_task_result
    task = {"id": tid, "type": "task", "root_task_id": tid, "chat_id": 0,
            "_attempt": 1, "admitted_dispatch": "none"}
    workers.PENDING.append(task)
    write_task_result(root, tid, "scheduled", root_task_id=tid, chat_id=0)
    return task


def _pause(tid="maintenance", nonce="maintenance-pause", temporary=True):
    from supervisor.events_mind_controls import _apply
    return _apply({"action": "pause_task", "task_id": tid, "actor_task_id": "coordinator",
                   "action_nonce": nonce, "reason": "clear the container maintenance window",
                   "resume_after_restart": temporary})


def _boot(root, monkeypatch):
    from supervisor import maintenance_return as mr
    monkeypatch.setattr(mr, "_PROCESS_GENERATION", "a-new-process")
    return mr.resume_maintenance_pauses(root)


def test_only_explicit_temporary_pause_returns_once_in_a_later_generation(tmp_path, monkeypatch):
    from ouroboros.owner_pause import read_fence
    from supervisor import maintenance_return as mr
    q, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 5.0)
    temporary = _queued(tmp_path, workers)
    indefinite = _queued(tmp_path, workers, "indefinite")
    assert _pause()["ok"]
    assert _pause("indefinite", temporary=False)["ok"]
    assert mr.resume_maintenance_pauses(tmp_path) == [], "same process is not a restart"
    result = _boot(tmp_path, monkeypatch)
    assert len(result) == 1 and result[0]["ok"], result
    assert not temporary.get("_budget_pause_hold")
    assert read_fence(tmp_path, indefinite["id"])["state"] == "paused"
    assert indefinite["id"] in q.BUDGET_ROOT_FENCES
    assert _boot(tmp_path, monkeypatch) == []
    assert read_fence(tmp_path, "maintenance")["maintenance_return"]["outcome"]["ok"]


def test_new_plain_pause_cancels_return_and_old_request_replay_cannot_rearm(tmp_path, monkeypatch):
    from ouroboros.owner_pause import read_fence
    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    _queued(tmp_path, workers)
    assert _pause()["ok"]
    assert _pause(nonce="keep-paused", temporary=False)["ok"]
    assert _pause()["ok"], "request replay remains idempotent"
    assert read_fence(tmp_path, "maintenance")["maintenance_return"]["state"] == "cancelled"
    assert _boot(tmp_path, monkeypatch) == []


def test_cannot_convert_an_existing_indefinite_pause(tmp_path, monkeypatch):
    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    _queued(tmp_path, workers)
    assert _pause(temporary=False)["ok"]
    assert _pause(nonce="later-maintenance")["error"] == "already_paused_indefinitely"


@pytest.mark.parametrize("reason", ["panic_hold", "saved_work_hold", "authority", "budget"])
def test_independent_holds_survive_maintenance_selection(tmp_path, monkeypatch, reason):
    from supervisor.events_budget import hold_budget_row
    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    task = _queued(tmp_path, workers)
    assert _pause()["ok"]
    hold_budget_row(task, reason=reason)
    before = dict(task["_budget_pause_hold"])
    result = _boot(tmp_path, monkeypatch)
    assert not result[0]["ok"]
    assert task["_budget_pause_hold"] == before


def _saved_pause(tmp_path, monkeypatch):
    from ouroboros import budget_pause, owner_pause
    from ouroboros.task_results import write_task_result
    from supervisor.events_budget import install_exact_budget_pause
    from tests.test_owner_pause import _owner_park
    q, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 5.0)
    write_task_result(tmp_path, "maintenance", "running", chat_id=0)
    workers.RUNNING["maintenance"] = {"task": {"id": "maintenance", "type": "task", "chat_id": 0,
                                               "root_task_id": "maintenance"}, "worker_id": 0, "attempt": 1}
    assert _pause()["ok"]
    row = _owner_park(tmp_path, monkeypatch, "maintenance", [], root="maintenance")
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING=workers.RUNNING, PENDING=workers.PENDING,
                          WORKERS=workers.WORKERS, sort_pending=lambda: None,
                          persist_queue_snapshot=q.persist_queue_snapshot, bridge=None)
    install_exact_budget_pause(ctx, "maintenance", budget_pause.exact_pause_marker(row)["checkpoint"])
    source = budget_pause.budget_pause_row(tmp_path, "maintenance")["source_ref"]
    assert owner_pause.read_fence(tmp_path, "maintenance")["state"] == "paused"
    return q, state, workers, source


def test_saved_owner_pause_uses_same_checkpoint_and_real_resume_owner(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    _q, _state, workers, source = _saved_pause(tmp_path, monkeypatch)
    result = _boot(tmp_path, monkeypatch)
    assert result[0]["ok"], result
    granted = budget_pause.budget_pause_row(tmp_path, "maintenance")
    assert granted["source_ref"] == source and granted["state"] == "resume_granted"
    assert workers.PENDING[0]["id"] == "maintenance"
    assert workers.PENDING[0]["_budget_pause_resume"]["grant_id"] == granted["grant"]["grant_id"]


@pytest.mark.parametrize("kind", ["saved", "queued"])
def test_saved_work_returns_across_real_process_generations(tmp_path, kind):
    """Two exited Python hosts share persisted queue/checkpoint; no Resume click."""
    import json
    import os
    import pathlib
    import subprocess
    import sys

    script = r'''
import json, pathlib, sys
from types import SimpleNamespace
from pytest import MonkeyPatch
from tests.test_maintenance_return import _install_queue, _saved_pause, _queued, _pause
from ouroboros import budget_pause, owner_pause
from supervisor.maintenance_return import resume_maintenance_pauses
from tests._budget_pause_exact_helpers import _loop_ctx
from tests.worker_pool_support import affirm_ready
root = pathlib.Path(sys.argv[1])
with MonkeyPatch.context() as mp:
    if sys.argv[2] == "stop":
        if sys.argv[3] == "saved":
            q, state, workers, source = _saved_pause(root, mp)
        else:
            q, state, workers = _install_queue(root, mp)
            _queued(root, workers)
            assert _pause()["ok"]
            source = None
        assert q.persist_queue_snapshot(reason="maintenance_host_stop")
        (root / "expected-source.json").write_text(json.dumps(source), encoding="utf-8")
    else:
        q, state, workers = _install_queue(root, mp)
        mp.setattr(state, "budget_remaining", lambda *_a, **_k: 5.0)
        q.restore_pending_from_snapshot()
        result = resume_maintenance_pauses(root)
        assert result and result[0]["ok"], result
        if sys.argv[3] == "saved":
            row = budget_pause.budget_pause_row(root, "maintenance")
            assert row["source_ref"] == json.loads((root / "expected-source.json").read_text())
        sent = []
        workers.WORKERS[0] = affirm_ready(SimpleNamespace(wid=0, busy_task_id=None, reaping=False,
            in_q=SimpleNamespace(put=lambda task: sent.append(dict(task)))))
        workers.assign_tasks()
        assert [t["id"] for t in sent] == ["maintenance"], sent
        if sys.argv[3] == "queued":
            assert owner_pause.read_fence(root, "maintenance")["state"] == "released"
            print("SAME_SAVED_WORK_RETURNED")
            sys.exit(0)
        ctx, limit = _loop_ctx(root, "maintenance")
        ctx.budget_pause_resume = sent[0]["_budget_pause_resume"]
        blob = budget_pause.load_budget_pause(ctx)
        mp.setattr("ouroboros.owner_wait.rebind_restored_route", lambda *_a, **_k: (None, "max"))
        mp.setattr("ouroboros.owner_wait.restore_continuation_state", lambda *_a, **_k: None)
        budget_pause.resume_paused_loop(limit.tools, blob, list(limit.messages), {}, {}, set(),
                                        budget_remaining_usd=5.0)
        assert owner_pause.read_fence(root, "maintenance")["state"] == "released"
        assert resume_maintenance_pauses(root) == []
        print("SAME_SAVED_WORK_RETURNED")
'''
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parents[1]))
    for phase in ("stop", "boot"):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path), phase, kind],
                                env=env, text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, (phase, result.stdout, result.stderr)
    assert "SAME_SAVED_WORK_RETURNED" in result.stdout


@pytest.mark.parametrize("blocker", ["_project_admission_restore_hold", "_owner_hold"])
def test_project_and_owner_holds_are_not_erased(tmp_path, monkeypatch, blocker):
    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    task = _queued(tmp_path, workers)
    assert _pause()["ok"]
    task[blocker] = {"reason": "retain-original-identity"}
    result = _boot(tmp_path, monkeypatch)
    assert result[0]["error"] == blocker
    assert task[blocker] == {"reason": "retain-original-identity"}


def test_plain_pause_between_custody_observation_and_grant_wins(tmp_path, monkeypatch):
    from ouroboros import budget_pause, owner_pause
    _q, _state, workers, _source = _saved_pause(tmp_path, monkeypatch)
    real = budget_pause.observe_task_runs
    def observe(*args, **kwargs):
        answer = real(*args, **kwargs)
        if kwargs.get("reason") == "budget_resume_uncovered_cost":
            assert _pause(nonce="new-indefinite", temporary=False)["ok"]
        return answer
    monkeypatch.setattr(budget_pause, "observe_task_runs", observe)
    result = _boot(tmp_path, monkeypatch)
    assert not result[0]["ok"], result
    assert "_budget_pause_resume" not in workers.PENDING[0]
    assert owner_pause.read_fence(tmp_path, "maintenance")["maintenance_return"]["state"] == "cancelled"


def test_unknown_external_effects_are_not_accepted(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    _q, _state, workers, _source = _saved_pause(tmp_path, monkeypatch)
    monkeypatch.setattr(budget_pause, "observe_task_runs", lambda *a, **k: {
        "custody_read": "ok", "runs": [{"run_id": "still-unknown", "state": "unknown"}]})
    result = _boot(tmp_path, monkeypatch)
    assert result[0]["error"] == "owner_pause_effects_unsettled", result
    assert result[0]["blockers"][0]["run_id"] == "still-unknown"
    assert "_budget_pause_resume" not in workers.PENDING[0]


def test_money_and_deadline_keep_saved_work_held(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    _q, state, workers, _source = _saved_pause(tmp_path, monkeypatch)
    workers.PENDING[0]["deadline_at"] = "2000-01-01T00:00:00+00:00"
    result = _boot(tmp_path, monkeypatch)
    assert result[0]["error"] == "deadline_passed", result
    workers.PENDING[0].pop("deadline_at")
    from supervisor import maintenance_return as mr
    monkeypatch.setattr(mr, "_PROCESS_GENERATION", "a-later-boot")
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 0.0)
    result = mr.resume_maintenance_pauses(tmp_path)
    assert not result[0]["ok"], result
    assert budget_pause.budget_pause_row(tmp_path, "maintenance")["state"] == "paused"


def test_observation_preserves_pause_time_and_unreadable_sibling_does_not_stop_recovery(tmp_path, monkeypatch):
    from ouroboros.owner_pause import read_fence
    from supervisor import maintenance_return as mr
    _q, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 5.0)
    _queued(tmp_path, workers, "unreadable")
    task = _queued(tmp_path, workers)
    task["title"] = "Saved calculation"
    assert _pause()["ok"]
    before = read_fence(tmp_path, "maintenance")
    real = mr.read_fence
    def read(root, tid):
        if tid == "unreadable":
            raise ValueError("owner_pause_authority_unreadable")
        return real(root, tid)
    monkeypatch.setattr(mr, "read_fence", read)
    rows = _boot(tmp_path, monkeypatch)
    assert rows[0]["error"] == "maintenance_authority_unavailable"
    assert rows[1]["ok"] and rows[1]["title"] == "Saved calculation"
    after = real(tmp_path, "maintenance")
    assert after["paused_at"] == before["paused_at"]
