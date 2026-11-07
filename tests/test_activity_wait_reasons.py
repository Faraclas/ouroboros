"""The real chooser names its evaluated gate; forecasts never publish causes."""
from types import SimpleNamespace
import pytest
from supervisor import worker_assignment as wa
from ouroboros.gateway.work_status import classify_member, roll_up_room


def task(tid, project="p", **extra):
    return {"id": tid, "project_id": project, **extra}


@pytest.fixture
def gates(monkeypatch, tmp_path):
    pool = SimpleNamespace(DRIVE_ROOT=tmp_path, PENDING=[], MAX_WORKERS=3,
                           _invalid_depth_deferred=lambda *a: False,
                           repo_writer_task_allowed=lambda *a: True,
                           _running_subagent_count=lambda *a: 0,
                           _assignment_depth_reservation_admits=lambda *a: False,
                           _normalize_pending_task_depth=lambda *a: "",
                           _evolution_assignment_error=lambda *a: "",
                           utc_now_iso=lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(wa, "_pool", lambda: pool)
    monkeypatch.setattr(wa, "_ASSIGNMENT_WAITS", {})
    monkeypatch.setattr(wa, "_foreign_roots_refused", lambda *a: False)
    monkeypatch.setattr(wa, "_direct_actor_still_registered", lambda *a: False)
    monkeypatch.setattr(wa, "budget_hold_fact", lambda *a: None)
    monkeypatch.setattr(wa, "continuation_refusal", lambda *a: {})
    monkeypatch.setattr("supervisor.task_lifecycle.root_cancellation_fenced", lambda *a: False)
    monkeypatch.setattr("supervisor.consciousness_carriers.autonomous_work_enabled", lambda *a: True)
    monkeypatch.setattr("supervisor.events_budget.budget_resume_dispatch_allowed", lambda *a: True)
    monkeypatch.setattr("ouroboros.project_placement.refresh_pending_placement", lambda *a: True)
    monkeypatch.setattr("ouroboros.config.get_max_active_subagents_per_root", lambda: 3)
    monkeypatch.setattr("ouroboros.sleep_custody.carrier_receipt", lambda row: {
        "retains_project_lane": row.get("cold", False)})
    monkeypatch.setattr("ouroboros.server_restart.agent_restart_holds", lambda *a: False)
    return pool, SimpleNamespace(BUDGET_ROOT_FENCES={})


def test_idle_worker_does_not_remove_project_blocker(gates):
    pool, queue = gates
    candidate = task("queued")
    pool.PENDING = [candidate]
    running = [{"task": task("owner")}]
    # The chooser is invoked on a genuinely free worker's selection path.
    assert wa._choose_candidate(queue, 1000, set(), set(), running, set()) == (None, False)
    assert candidate == task("queued")
    shown = wa.pending_assignment_view(candidate)
    assert shown["dispatch_wait"]["code"] == "project_busy"
    assert shown["dispatch_wait"]["ids"] == ["owner"]
    row = classify_member({"activity_id": "queued", "phase": "queued"}, shown)
    assert row["reasons"][0]["code"] == "project_busy"
    assert "free worker" not in row["reasons"][0]["text"]
    assert row["reasons"][0]["ids"] == ["owner"]
    assert "release its writer lane" in row["reasons"][0]["text"]


def test_reserved_launch_and_cold_holder_remain_real_blockers(gates):
    pool, queue = gates
    candidate = task("queued")
    pool.PENDING = [candidate, task("cold", cold=True)]
    assert wa._candidate_blocker(queue, candidate, 1000, []) == "project_busy"
    pool.PENDING = [candidate]
    assert wa._candidate_blocker(queue, candidate, 1000,
                                  [{"task": task("reserved", _launch_claim={"token": "claim"})}]) == "project_busy"
    assert wa._candidate_blocker(queue, candidate, 1000, []) == ""


@pytest.mark.parametrize("project,role", [("other", ""), ("", ""), ("p", "subagent")])
def test_surviving_other_project_unscoped_and_subagent_paths(gates, project, role):
    pool, queue = gates
    candidate = task("next", project, delegation_role=role)
    assert wa._candidate_blocker(queue, candidate, 1000, [{"task": task("owner")}]) == ""
    assert wa._candidate_ineligible(queue, candidate, 1000, [{"task": task("owner")}]) is False


def test_forecast_does_not_publish_hypothetical_project_blocker(gates):
    pool, queue = gates
    first, second = task("first"), task("second")
    pool.PENDING = [first, second]
    assert wa._choose_candidate(queue, 1000, set(), set(), [], set(), observe_wait=False) == (0, False)
    assert wa._choose_candidate(queue, 1000, set(), set(), [{"task": first}], {"first"},
                                observe_wait=False) == (None, False)
    assert "dispatch_wait" not in first and "dispatch_wait" not in second
    assert wa.pending_assignment_view(first) == first
    assert wa.pending_assignment_view(second) == second


def test_restart_and_budget_are_distinct_actual_gates(gates, monkeypatch):
    pool, queue = gates
    candidate = task("queued")
    pool.PENDING = [candidate]
    monkeypatch.setattr("ouroboros.server_restart.agent_restart_holds", lambda *a: True)
    assert wa._choose_candidate(queue, 1000, set(), set(), [], set()) == (None, False)
    assert wa.pending_assignment_view(candidate)["dispatch_wait"]["code"] == "restart_pending"
    assert wa._candidate_blocker(queue, candidate, 0, []) == "budget_dispatch"
    assert wa._candidate_ineligible(queue, candidate, 0, []) is True


def test_queued_without_observed_cause_or_with_old_attempt_is_neutral():
    row = {"activity_id": "q", "phase": "queued", "task_attempt": 2}
    for raw in ({}, {"dispatch_wait": {"code": "project_busy", "task_attempt": 1}}):
        assert classify_member(row, raw)["reasons"] == [{
            "code": "queued", "text": "Queued for dispatch; the current hold is not reported"}]


def test_room_exposes_working_service_beside_waiting_and_queued():
    def work(tid, activity, live=False, tails=None):
        return {"work_id": tid, "activity": activity, "transition": "none", "call": "", "turn": [],
                "live": live, "confirmed": True, "tails": tails or []}
    room = roll_up_room([work("sleep", "waiting", True, [{"kind": "service", "state": "live"}]),
                         work("queued", "queued")])
    assert room["activity"] == "queued"  # episode words stay truthful; running tails are a second fact
    assert room["counts"] == {"working": 0, "queued": 1, "waiting": 1, "paused": 0, "unknown": 0}
    assert room["running_services"] == 1 and room["mark"] == "moving"


def test_restoring_a_pending_row_does_not_replay_an_old_dispatch_diagnosis():
    from supervisor.queue_snapshot import _saved_pending_task
    original = {"id": "q", "dispatch_wait": {"code": "project_busy", "task_attempt": 1}}
    decoded = _saved_pending_task({"task": original})
    assert "dispatch_wait" not in decoded
    assert original["dispatch_wait"]["code"] == "project_busy"


def test_released_launch_cannot_replay_same_object_dispatch_ready(gates):
    from threading import RLock
    pool, queue = gates
    queue._queue_lock = RLock()
    candidate = task("queued", _launch_claim={"token": "claim", "wid": "w"})
    pool.PENDING = [candidate]
    worker = SimpleNamespace(busy_task_id="queued", reaping=False)
    wa._record_assignment_wait(candidate, "dispatch_ready")
    wa._release_launch(queue, candidate, worker, "claim")
    assert worker.busy_task_id is None
    assert wa.pending_assignment_view(candidate) == task("queued")
    assert "queued" not in wa._ASSIGNMENT_WAITS


def test_observation_cannot_cross_object_attempt_or_retained_snapshot(gates):
    pool, _ = gates
    candidate = task("queued", _attempt=2)
    pool.PENDING = [candidate]
    before = dict(candidate)
    wa._record_assignment_wait(candidate, "resume_unconfirmed")
    assert candidate == before
    assert wa.pending_assignment_view(candidate)["dispatch_wait"]["task_attempt"] == 2
    assert wa.pending_assignment_view(dict(candidate)) == candidate
    candidate["_attempt"] = 3
    assert wa.pending_assignment_view(candidate) == candidate
    candidate["_attempt"] = 2
    from supervisor.queue_snapshot import _saved_pending_task
    restored = _saved_pending_task({"task": wa.pending_assignment_view(candidate)})
    assert "dispatch_wait" not in restored
    assert "dispatch_wait" not in wa.pending_assignment_view(restored)


@pytest.mark.parametrize("transition", ["same", "copy", "attempt", "restore"])
def test_live_census_keeps_chooser_facts_outside_authority(gates, monkeypatch, transition):
    import copy
    from supervisor import queue as queue_mod
    from supervisor.queue_snapshot import _saved_pending_task
    from ouroboros.gateway.state import _chat_activities_snapshot_safe
    from ouroboros.task_results import write_task_result

    pool, chooser_queue = gates
    candidate = task("queued", chat_id=1, _attempt=2)
    pool.PENDING = [candidate]
    running = [{"task": task("owner", chat_id=1)}]
    assert wa._choose_candidate(chooser_queue, 1000, set(), set(), running, set()) == (None, False)
    observed = wa.pending_assignment_view(candidate)["dispatch_wait"]
    if transition == "copy":
        candidate = dict(candidate)
    elif transition == "attempt":
        candidate["_attempt"] = 3
    elif transition == "restore":
        candidate = _saved_pending_task({"task": wa.pending_assignment_view(candidate)})
    before = copy.deepcopy(candidate)
    for tid, status in (("queued", "scheduled"), ("owner", "running")):
        write_task_result(pool.DRIVE_ROOT, tid, status, chat_id=1)
    monkeypatch.setattr(queue_mod, "PENDING", [candidate])
    monkeypatch.setattr(queue_mod, "RUNNING", {"owner": running[0]})
    monkeypatch.setattr(queue_mod, "BUDGET_ROOT_FENCES", {})
    facts = {}
    rows = _chat_activities_snapshot_safe(pool.DRIVE_ROOT, direct_turns=[], question_details=False,
                                          work_facts=facts)
    assert candidate == before and "dispatch_wait" not in candidate
    queued = next(row for row in rows if row["activity_id"] == "queued")
    reason = classify_member(queued, facts["queued"])["reasons"][0]
    if transition == "same":
        assert facts["queued"].get("dispatch_wait") == observed
        assert reason["code"] == "project_busy" and reason["ids"] == ["owner"]
        assert reason["observed_at"] == observed["observed_at"]
    else:
        assert "dispatch_wait" not in facts["queued"]
        assert reason == {"code": "queued", "text": "Queued for dispatch; the current hold is not reported"}
