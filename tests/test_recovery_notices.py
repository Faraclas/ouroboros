"""Recovery disclosures reach each Project without changing execution selection."""
from types import SimpleNamespace

import pytest

from ouroboros import pause_notices
from ouroboros.projects_registry import create_project
from ouroboros.task_results import write_task_result
from supervisor import message_bus
from tests import _dialogue
from tests._budget_pause_exact_helpers import _install_queue
from tests.test_maintenance_return import _boot, _pause, _queued

pytestmark = pytest.mark.serial


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    q, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_kw: 5.0)
    monkeypatch.setattr(state, "load_state", lambda: {"owner_chat_id": 1})
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"owner_id": 1})
    sent = []
    bridge = SimpleNamespace(send_message=lambda chat, text, **kw: sent.append((chat, text, kw)))
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    return tmp_path, workers, sent


def project_task(root, workers, name):
    project = create_project(root, name, name=name.title())
    task = _queued(root, workers, name)
    task.update(project_id=project["id"], chat_id=project["chat_id"])
    write_task_result(root, name, "scheduled", project_id=project["id"], chat_id=project["chat_id"])
    return project, task


def test_maintenance_outcomes_are_recorded_in_each_project_not_main(recovery, monkeypatch):
    root, workers, sent = recovery
    first, a = project_task(root, workers, "alpha")
    second, b = project_task(root, workers, "beta")
    assert _pause("alpha", nonce="pause-alpha")["ok"]
    assert _pause("beta", nonce="pause-beta")["ok"]
    b["_project_admission_restore_hold"] = {"reason": "retain-original-identity"}
    outcomes = _boot(root, monkeypatch)
    assert outcomes[0]["ok"] and outcomes[1]["error"] == "_project_admission_restore_hold"
    pause_notices.reconcile_pause_notices(root)
    rows = [r for r in _dialogue.chat_rows(root) if "recovery" in r["text"].lower()]
    assert {r["chat_id"] for r in rows} == {first["chat_id"], second["chat_id"]}, rows
    assert not [r for r in rows if r["chat_id"] == 1]
    assert any("returned this work to the queue" in r["text"] for r in rows), rows
    assert any("project identity" in r["text"] for r in rows)
    assert not a.get("_budget_pause_hold") and b["_project_admission_restore_hold"]
    before = len(_dialogue.chat_rows(root))
    assert _boot(root, monkeypatch) == []
    pause_notices.reconcile_pause_notices(root)
    assert len(_dialogue.chat_rows(root)) == before


def test_app_stop_hold_is_retained_and_disclosed_locally_once(recovery):
    from supervisor.restart_retention import hold_stopped_queue
    root, workers, sent = recovery
    project, task = project_task(root, workers, "alpha")
    hold_stopped_queue(task, root, "app_stop")
    hold = task["_budget_pause_hold"]
    pause_notices.reconcile_pause_notices(root)
    rows = _dialogue.chat_rows(root)
    assert len(rows) == 1, rows
    assert rows[0]["chat_id"] == project["chat_id"]
    assert "app stopped" in rows[0]["text"] and "Resume" in rows[0]["text"]
    assert task["_budget_pause_hold"] == hold and "_budget_pause_resume" not in task
    pause_notices.reconcile_pause_notices(root)
    assert len(_dialogue.chat_rows(root)) == 1


def test_failed_recovery_append_stays_owed_and_retries_without_resuming(recovery, monkeypatch):
    from ouroboros import obligations
    from supervisor.restart_retention import hold_stopped_queue
    root, workers, sent = recovery
    project, task = project_task(root, workers, "alpha")
    hold_stopped_queue(task, root, "app_stop")
    hold = dict(task["_budget_pause_hold"])
    with monkeypatch.context() as mp:
        mp.setattr(message_bus, "log_chat", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        pause_notices.reconcile_pause_notices(root)
    assert obligations.members(root, "pause_notices") and not _dialogue.chat_rows(root)
    pause_notices.reconcile_pause_notices(root)
    pause_notices.reconcile_pause_notices(root)
    assert len(_dialogue.chat_rows(root)) == 1 and not obligations.members(root, "pause_notices")
    assert task["_budget_pause_hold"] == hold and "_budget_pause_resume" not in task


def test_notice_annotation_failure_does_not_relabel_or_repeat_successful_return(recovery, monkeypatch):
    from ouroboros import task_store
    root, workers, sent = recovery
    project, task = project_task(root, workers, "alpha")
    assert _pause("alpha", nonce="pause-alpha")["ok"]
    original = task_store.patch_document
    def patch(root, tid, update, **kw):
        if update.__name__ == "annotate":
            raise OSError("notice storage unavailable")
        return original(root, tid, update, **kw)
    monkeypatch.setattr(task_store, "patch_document", patch)
    outcomes = _boot(root, monkeypatch)
    assert outcomes[0]["ok"] and outcomes[0]["notice_error"] == "notice storage unavailable"
    assert not task.get("_budget_pause_hold")
    assert _boot(root, monkeypatch) == [], "accepted Resume cannot be repeated to retry a notice"


def test_new_pause_survives_historical_recovery_notice(recovery, monkeypatch):
    from ouroboros.owner_pause import read_fence
    root, workers, sent = recovery
    project, task = project_task(root, workers, "alpha")
    assert _pause("alpha", nonce="pause-alpha")["ok"]
    from supervisor import maintenance_return as mr
    original = mr.launch_lock
    def lock(root, tid):
        # A new owner Pause wins BEFORE the annotation takes its launch lock.
        assert _pause("alpha", nonce="new-indefinite", temporary=False)["ok"]
        return original(root, tid)
    monkeypatch.setattr(mr, "launch_lock", lock)
    assert _boot(root, monkeypatch)[0]["ok"]
    fence = read_fence(root, "alpha")
    assert fence["state"] == "paused" and not (fence.get("maintenance_return") or {}).get("state") == "pending"
    pause_notices.reconcile_pause_notices(root)
    assert any("returned this work to the queue" in r["text"] for r in _dialogue.chat_rows(root))
    assert read_fence(root, "alpha") == fence


def test_unreadable_recovery_authority_has_a_local_disclosure(recovery, monkeypatch):
    from supervisor import maintenance_return as mr
    root, workers, sent = recovery
    project, task = project_task(root, workers, "alpha")
    assert _pause("alpha", nonce="pause-alpha")["ok"]
    original = mr.read_fence
    before = original(root, "alpha")
    monkeypatch.setattr(mr, "read_fence", lambda *a: (_ for _ in ()).throw(ValueError("damaged authority")))
    outcome = _boot(root, monkeypatch)
    assert outcome[0]["error"] == "maintenance_authority_unavailable"
    pause_notices.reconcile_pause_notices(root)
    rows = _dialogue.chat_rows(root)
    assert any(r["chat_id"] == project["chat_id"] and "saved recovery authority" in r["text"] for r in rows)
    assert original(root, "alpha") == before
    assert "_budget_pause_resume" not in task
