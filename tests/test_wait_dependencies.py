"""Wait explanations are authored context, not executable prerequisite policy."""
from types import SimpleNamespace

import pytest

from ouroboros import model_sleep
from ouroboros.gateway.work_status import classify_member, roll_up_work
from ouroboros.gateway.work_projection import _wait_targets
from ouroboros.task_results import write_task_result


DEPENDENCY = {"waiting_for": "Install the checked change", "why": "Tests must finish first",
              "release": "The test owner confirms its result", "on": ["test-owner"]}


def test_selectors_do_not_turn_dependencies_into_wakes(tmp_path):
    write_task_result(tmp_path, "test-owner", "running")
    ctx = SimpleNamespace(drive_root=tmp_path, budget_drive_root=tmp_path)
    selected = model_sleep.selectors(ctx, wake_after_sec=60, dependency=DEPENDENCY)
    assert selected["dependency"] == DEPENDENCY
    assert selected["tasks"] == selected["senders"] == []
    assert selected["any_mail"] is True
    assert model_sleep.public_dependency({"mode": "cold", **selected}) == DEPENDENCY
    selected["dependency"]["on"].append("other")
    assert DEPENDENCY["on"] == ["test-owner"]


@pytest.mark.parametrize("value", [{}, {**DEPENDENCY, "why": ""}, {**DEPENDENCY, "on": ["absent"]}])
def test_invalid_explanations_do_not_arm_sleep(tmp_path, value):
    ctx = SimpleNamespace(drive_root=tmp_path, budget_drive_root=tmp_path)
    with pytest.raises(ValueError):
        model_sleep.selectors(ctx, dependency=value)
    assert not hasattr(ctx, "_model_sleep")


def sleeping(task_id, target, room):
    sleep = {"mode": "warm", "tasks": [], "senders": [target], "dependency": {
        **DEPENDENCY, "on": [target]}}
    member = classify_member({"activity_id": task_id, "phase": "thinking", "task_attempt": 1}, {
        "owner_wait": {"state": "waiting", "reason": "sleep", "task_attempt": 1, "sleep": sleep}})
    work = roll_up_work(task_id, [member], episode={"task_id": task_id, "state": member}, name=task_id, room=room)
    work["project_id"] = task_id
    return work


def test_chain_resolution_is_one_hop_and_termination_never_satisfies_release(tmp_path, monkeypatch):
    a, b = sleeping("a", "b", 11), sleeping("b", "test-owner", 12)
    write_task_result(tmp_path, "test-owner", "completed")
    # A model answer with delivery/post-work still owed is not a settled target.
    _wait_targets([b], tmp_path)
    assert b["because"][0]["targets"][0]["state"] == "unknown"
    from ouroboros import task_store
    # Resolve a discharged terminal version through the same typed-column seam.
    monkeypatch.setattr(task_store, "task_versions", lambda *_: {"test-owner": (2, "completed", False)})
    _wait_targets([a, b], tmp_path)
    assert a["because"][0]["targets"][0] == {"task_id": "b", "name": "b", "state": "live",
        "selected_as_wake": True, "work_id": "b", "room": 12, "project_id": "b", "activity": "waiting"}
    target = b["because"][0]["targets"][0]
    from ouroboros.task_store import task_versions
    assert target["state"] == "ended", task_versions(tmp_path, ["test-owner"])
    assert b["because"][0]["dependency"]["release"] == DEPENDENCY["release"]
    assert "satisfied" not in b["because"][0]
    # The next census no longer carries an ended sleep's reasons.
    a["because"] = []
    a["members"]["states"] = {}
    _wait_targets([a], tmp_path)
    assert a["because"] == []


def test_unreadable_target_status_remains_unknown_without_full_body_reads(tmp_path, monkeypatch):
    from ouroboros import task_store
    work = sleeping("a", "absent", 11)
    monkeypatch.setattr(task_store, "task_versions", lambda *_: (_ for _ in ()).throw(OSError("unreadable")))
    _wait_targets([work], tmp_path)
    assert work["because"][0]["targets"][0]["state"] == "unknown"
    assert "room" not in work["because"][0]["targets"][0]


def test_stale_sleep_attempt_and_old_checkpoint_do_not_claim_dependency():
    row = {"activity_id": "a", "phase": "thinking", "task_attempt": 2}
    wait = {"state": "waiting", "reason": "sleep", "task_attempt": 1,
            "sleep": {"mode": "warm", "dependency": DEPENDENCY}}
    assert not classify_member(row, {"owner_wait": wait})["reasons"]
    assert model_sleep.public_dependency({"mode": "warm"}) == {}
    assert model_sleep.public_dependency({"mode": "in_slot", "dependency": DEPENDENCY}) == {}


@pytest.mark.parametrize("mode", ["warm", "cold"])
def test_public_tool_checkpoint_and_restored_sleep_keep_explanation(tmp_path, mode):
    import json
    from tests.test_model_sleep import _ctx, _result
    from ouroboros.tools.control_task_results import _await_messages
    from ouroboros.owner_wait import checkpoint_owner_wait
    from ouroboros.peer_roster import _sleep_facts

    _result(tmp_path, "sleeper")
    _result(tmp_path, "test-owner")
    ctx = _ctx(tmp_path)
    reply = json.loads(_await_messages(ctx, mode=mode, senders=["test-owner"], dependency=DEPENDENCY))
    assert reply["reason"] == "sleep_armed"
    checkpoint = checkpoint_owner_wait(ctx, [{"role": "user", "content": "wait"}], {}, {}, 1, [], set())
    restored = json.loads(json.dumps(checkpoint))
    assert model_sleep.public_dependency(restored["sleep"]) == DEPENDENCY
    assert _sleep_facts(restored["sleep"])["dependency"] == DEPENDENCY
    assert model_sleep.public_wake_sources(restored["sleep"])["tasks"] == []


def test_dependency_requires_sleep_mode_through_public_handler(tmp_path):
    from tests.test_model_sleep import _ctx
    from ouroboros.tools.control_task_results import _await_messages
    ctx = _ctx(tmp_path)
    assert "requires mode=warm or cold" in _await_messages(ctx, dependency=DEPENDENCY)
    assert not getattr(ctx, "_model_sleep", None)


def test_member_targets_use_the_member_state_not_its_work_rollup(tmp_path):
    owner = sleeping("owner", "helper", 11)
    helper = classify_member({"activity_id": "helper", "phase": "queued", "task_attempt": 1})
    member = classify_member({"activity_id": "root", "phase": "thinking", "task_attempt": 1})
    grouped = roll_up_work("root", [member, helper], episode={"task_id": "root", "state": member}, name="Build", room=12)
    _wait_targets([owner, grouped], tmp_path)
    ref = owner["because"][0]["targets"][0]
    assert ref["activity"] == "queued" and ref["name"] == "helper — Build"
    # An ended episode with a live helper is not the helper itself.
    grouped["members"]["states"].pop("root")
    owner["because"][0]["dependency"]["on"] = ["root"]
    owner["because"][0]["wake"]["senders"] = []
    _wait_targets([owner, grouped], tmp_path)
    assert owner["because"][0]["targets"][0]["state"] == "unknown"


def test_in_slot_targets_come_only_from_current_unexpired_lease(tmp_path):
    import time
    from ouroboros.gateway.state import _raw_work_facts
    from ouroboros.gateway.work_projection import _member_raw
    leases = {
        "live": {"kind": "in_slot_wait", "task_attempt": 2, "until_ts": time.time() + 60, "wait_for_tasks": ["child"]},
        "old": {"kind": "in_slot_wait", "task_attempt": 1, "until_ts": time.time() + 60, "wait_for_tasks": ["old"]},
        "expired": {"kind": "in_slot_wait", "task_attempt": 2, "until_ts": 1, "wait_for_tasks": ["expired"]}}
    row = {"activity_id": "a", "phase": "thinking", "task_attempt": 2}
    facts = _member_raw(row, _raw_work_facts({"active_operation_leases": leases}), None, "a")
    assert facts["wait_for_tasks"] == ["child"]
    state = classify_member(row, facts)
    assert state["activity"] == "working", "a held worker slot is not sleeping cognition"
    assert state["reasons"][0]["code"] == "in_slot_wait"
    assert state["reasons"][0]["ids"] == ["child"]
