"""Reopen uses the existing disabled/Restore state, independently of Close.

Actual Close/Reopen hooks are tested by the lifecycle owner. These exercise its
schedule-writer contract and the scheduler's tombstone refusal/Restore paths.
"""
from __future__ import annotations

import copy
import json

import pytest

from tests.test_schedule_occurrence import q, _project, _row, _rows

pytestmark = pytest.mark.serial


def test_ordinary_overdue_disable_restore_keeps_single_catchup(q):
    _row(q, cron=True, intent={"kind": "explicit_none"})
    due = _rows(q)["s1"]["next_run_at"]
    assert q.queue.mutate_scheduled_task("disable", "s1", reason="pause", actor="owner")["ok"]
    assert q.queue.mutate_scheduled_task("restore", "s1", reason="resume", actor="owner")["ok"]
    assert _rows(q)["s1"]["next_run_at"] == due
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1


@pytest.mark.parametrize("failure", ["table", "origin", "audit"])
def test_close_keeps_its_fence_when_schedule_authority_is_unavailable(q, monkeypatch, failure):
    from ouroboros import projects_registry as registry, project_management, task_results
    from supervisor import schedule_lifecycle
    from supervisor.queue_schedules import _scheduled_tasks_path

    project = _project(q)
    if failure == "origin":
        _row(q, cron=True, intent={"kind": "explicit_none"}, metadata={"origin_task_id": "unknown"})
        def unreadable(*args, **kwargs):
            raise OSError("origin unreadable")
        monkeypatch.setattr(task_results, "load_task_result", unreadable)
    else:
        _row(q, cron=True, project_id=project["id"], intent={"kind": "explicit_none"})
    path = _scheduled_tasks_path(q.root)
    if failure == "table":
        path.write_text("{torn", encoding="utf-8")
    elif failure == "audit":
        monkeypatch.setattr(schedule_lifecycle, "_audit_schedule_mutation", lambda **kw: False)
    before = path.read_bytes()
    assert registry.begin_project_deletion(q.root, project["id"])["lifecycle"] == "deleting"
    assert registry.complete_project_deletion(q.root, project["id"])["lifecycle"] == "tombstoned"
    assert path.read_bytes() == before
    with pytest.raises((ValueError, RuntimeError, OSError)):
        project_management.reopen_project(q.root, project["id"])
    assert registry.get_reserved_project(q.root, project["id"])["lifecycle"] == "tombstoned"
    assert path.read_bytes() == before


@pytest.mark.parametrize("membership", ["scope", "chat", "room_default", "bound_origin", "scoped_origin"])
def test_pause_matches_recorded_room_membership_not_shared_folders(q, membership):
    from ouroboros.projects_registry import bind_task_to_project
    from ouroboros.task_results import write_task_result
    from supervisor.schedule_lifecycle import pause_project_schedules

    project = _project(q)
    _project(q, "other")
    kwargs = {"project_id": "", "chat_id": 1, "intent": {"kind": "explicit_none"}}
    if membership == "scope":
        kwargs["project_id"] = project["id"]
    elif membership == "chat":
        kwargs["chat_id"] = project["chat_id"]
    elif membership == "room_default":
        kwargs["intent"] = {"kind": "room_default", "project_id": project["id"]}
    else:
        kwargs["metadata"] = {"origin_task_id": "origin"}
        if membership == "bound_origin":
            bind_task_to_project(q.root, "origin", project["id"], origin={"absent": "system"})
        else:
            write_task_result(q.root, "origin", "completed", project_id=project["id"], chat_id=project["chat_id"])
    _row(q, "member", cron=True, **kwargs)
    _row(q, "other", cron=True, project_id="other", chat_id=1,
         intent={"kind": "explicit_resource", "root": "/same/shared/folder"})
    _row(q, "disabled", cron=True, project_id="proj", intent={"kind": "explicit_none"}, enabled=False)
    before = copy.deepcopy(_rows(q))

    paused = pause_project_schedules(q.root, project["id"], project["chat_id"])

    assert paused["ok"] and paused["schedule_ids"] == ["member"]
    rows = _rows(q)
    assert rows["member"]["enabled"] is False
    assert rows["member"]["next_run_at"] == before["member"]["next_run_at"]
    assert rows["other"] == before["other"] and rows["disabled"] == before["disabled"]
    assert pause_project_schedules(q.root, project["id"], project["chat_id"])["changed"] is False
    events = [json.loads(line) for line in (q.root / "logs/events.jsonl").read_text().splitlines()]
    facts = [row for row in events if row.get("reason") == "project_closed:proj"]
    assert [row["phase"] for row in facts] == ["intent", "outcome"]


@pytest.mark.parametrize("cron", [False, True])
def test_pause_prevents_future_ticks_until_explicit_restore_and_preserves_once_semantics(q, cron):
    from supervisor.schedule_lifecycle import pause_project_schedules
    project = _project(q)
    _row(q, cron=cron, project_id=project["id"], chat_id=project["chat_id"], intent={"kind": "explicit_none"})
    pause_project_schedules(q.root, project["id"], project["chat_id"])
    for _ in range(3):
        q.queue.check_scheduled_tasks()
    assert q.pending == [] and not _rows(q)["s1"].get("completed_at")
    restored = q.queue.mutate_scheduled_task("restore", "s1", reason="owner chose schedule", actor="owner")
    assert restored["ok"] and _rows(q)["s1"]["enabled"]
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1
    assert bool(_rows(q)["s1"].get("completed_at")) is (not cron)
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1


@pytest.mark.parametrize("cron", [False, True])
def test_tombstone_hold_cannot_reenable_old_cron_after_reopen(q, cron):
    from supervisor.schedule_occurrence import _refused
    from supervisor.queue_schedules import _write_scheduled_tasks, load_schedule_store, schedule_transaction
    from ouroboros.projects_registry import begin_project_deletion, complete_project_deletion
    from ouroboros.project_management import reopen_project
    project = _project(q)
    _row(q, cron=cron, project_id=project["id"], intent={"kind": "explicit_none"})
    begin_project_deletion(q.root, project["id"])
    complete_project_deletion(q.root, project["id"])
    with schedule_transaction(q.root):
        data = load_schedule_store(q.root)
        _refused(data["tasks"][0], "project_routing_fence",
                 {"_project_lifecycle": "tombstoned", "_project_id": project["id"]})
        _write_scheduled_tasks(data, q.root)
    row = _rows(q)["s1"]
    assert row["enabled"] is cron
    if cron:
        assert row["hold"]["reason"] == "project_routing_fence"
    for _ in range(3):
        q.queue.check_scheduled_tasks()
    assert q.pending == []
    reopen_project(q.root, project["id"])
    assert not _rows(q)["s1"]["enabled"]
    for _ in range(3):
        q.queue.check_scheduled_tasks()
    assert q.pending == []
    restored = q.queue.mutate_scheduled_task("restore", "s1", reason="explicit", actor="owner")
    if cron:
        assert restored["ok"] and _rows(q)["s1"]["enabled"]
    else:
        assert restored["status"] == "consumed_not_rearmed" and not _rows(q)["s1"]["enabled"]


def test_unreadable_table_and_failed_audit_refuse_without_overwriting_schedule_bytes(q, monkeypatch):
    from supervisor import schedule_lifecycle as lifecycle
    from supervisor.queue_schedules import ScheduleRefused, ScheduleStoreUnreadable, _scheduled_tasks_path
    project = _project(q)
    _row(q, project_id=project["id"], intent={"kind": "explicit_none"})
    path = _scheduled_tasks_path(q.root)
    original = path.read_bytes()
    monkeypatch.setattr(lifecycle, "_audit_schedule_mutation", lambda **_kw: False)
    with pytest.raises(ScheduleRefused):
        lifecycle.pause_project_schedules(q.root, project["id"], project["chat_id"])
    assert path.read_bytes() == original
    path.write_text("{torn", encoding="utf-8")
    with pytest.raises(ScheduleStoreUnreadable):
        lifecycle.pause_project_schedules(q.root, project["id"], project["chat_id"])
    assert path.read_text() == "{torn"


@pytest.mark.parametrize('legacy', [False, True])
def test_actual_close_reopen_hooks_pause_without_tick_and_restore_new_incarnation(q, monkeypatch, legacy):
    from ouroboros.projects_registry import begin_project_deletion, complete_project_deletion, get_reserved_project
    from ouroboros.project_management import reopen_project
    from supervisor.queue_schedules import _write_scheduled_tasks, load_schedule_store, schedule_transaction
    from supervisor import schedule_lifecycle

    project = _project(q)
    _project(q, 'other')
    _row(q, 'member', cron=True, project_id=project['id'], chat_id=project['chat_id'], intent={'kind': 'explicit_none'})
    _row(q, 'owner-room', cron=True, chat_id=project['chat_id'], intent={'kind': 'explicit_none'})
    _row(q, 'other', cron=True, project_id='other', chat_id=1, intent={'kind': 'explicit_none'}, enabled=False)
    original_other = copy.deepcopy(_rows(q)['other'])
    if legacy:
        # A registry closed by an older version, no schedule hook ever ran.
        from ouroboros.projects_registry import _registry_path
        data = json.loads(_registry_path(q.root).read_text())
        data['projects'][0]['lifecycle'] = 'tombstoned'
        _registry_path(q.root).write_text(json.dumps(data))
    else:
        begin_project_deletion(q.root, project['id'])
        complete_project_deletion(q.root, project['id'])
        assert _rows(q)['member']['enabled']  # Close does not depend on scheduler availability
    reopened = reopen_project(q.root, project['id'])
    assert reopened['lifecycle'] == 'active'
    assert not _rows(q)['member']['enabled'] and not _rows(q)['owner-room']['enabled']
    assert _rows(q)['other'] == original_other
    for _ in range(3):
        q.queue.check_scheduled_tasks()
    assert q.pending == []
    restored = q.queue.mutate_scheduled_task('restore', 'member', reason='explicit restore', actor='owner')
    assert restored['ok']
    with schedule_transaction(q.root):
        table = load_schedule_store(q.root)
        next(row for row in table['tasks'] if row['id'] == 'member')['next_run_at'] = '2000-01-01T00:00:00+00:00'
        _write_scheduled_tasks(table, q.root)
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1
    assert q.pending[0]['_project_admission']['project']['routing_incarnation'] == reopened['routing_incarnation']
    assert get_reserved_project(q.root, project['id'])['routing_incarnation'] == reopened['routing_incarnation']


def test_no_enabled_member_has_no_schedule_write_or_audit(q, monkeypatch):
    from supervisor import schedule_lifecycle as lifecycle
    from ouroboros.projects_registry import begin_project_deletion, complete_project_deletion
    from ouroboros.project_management import reopen_project
    project = _project(q)
    _row(q, 'disabled', cron=True, project_id=project['id'], enabled=False)
    before = copy.deepcopy(_rows(q))
    monkeypatch.setattr(lifecycle, '_write_scheduled_tasks', lambda *a, **k: pytest.fail('no rows need writing'))
    monkeypatch.setattr(lifecycle, '_audit_schedule_mutation', lambda **k: pytest.fail('no mutation to audit'))
    begin_project_deletion(q.root, project['id'])
    complete_project_deletion(q.root, project['id'])
    reopen_project(q.root, project['id'])
    assert _rows(q) == before


@pytest.mark.parametrize('origin_evidence', ['binding', 'result', 'intent'])
def test_pause_respects_dispatch_destination_over_origin(q, origin_evidence):
    from ouroboros.projects_registry import bind_task_to_project, begin_project_deletion, complete_project_deletion
    from ouroboros.project_management import reopen_project
    from ouroboros.project_admission import task_project_membership
    from ouroboros.task_results import write_task_result
    from supervisor.queue_schedules import _task_from_schedule
    from supervisor.schedule_lifecycle import pause_project_schedules

    origin, destination = _project(q), _project(q, 'other')
    if origin_evidence == 'binding':
        bind_task_to_project(q.root, 'origin', origin['id'], origin={'absent': 'system'})
    elif origin_evidence == 'result':
        write_task_result(q.root, 'origin', 'completed', project_id=origin['id'], chat_id=origin['chat_id'])
    intent = ({'kind': 'room_default', 'project_id': origin['id']} if origin_evidence == 'intent'
              else {'kind': 'explicit_none'})
    _row(q, cron=True, project_id=destination['id'], chat_id=destination['chat_id'], intent=intent,
         metadata={} if origin_evidence == 'intent' else {'origin_task_id': 'origin'})
    before = copy.deepcopy(_rows(q)['s1'])
    # Use the actual public writer's normalized template and dispatch builder.
    task = _task_from_schedule(before)
    assert task_project_membership(q.root, task)[0] == destination['id']
    assert pause_project_schedules(q.root, origin['id'], origin['chat_id'])['schedule_ids'] == []
    assert _rows(q)['s1'] == before
    begin_project_deletion(q.root, origin['id'])
    complete_project_deletion(q.root, origin['id'])
    reopen_project(q.root, origin['id'])
    assert _rows(q)['s1'] == before
    q.queue.check_scheduled_tasks()
    assert len(q.pending) == 1 and q.pending[0]['project_id'] == destination['id']


def test_pause_keeps_dispatch_host_origin_root_membership(q):
    from ouroboros.projects_registry import bind_task_to_project
    from ouroboros.project_admission import task_project_membership
    from supervisor.queue_schedules import _task_from_schedule
    from supervisor.schedule_lifecycle import pause_project_schedules

    project = _project(q)
    bind_task_to_project(q.root, 'ancestor', project['id'], origin={'absent': 'system'})
    _row(q, cron=True, chat_id=1, intent={'kind': 'explicit_none'},
         metadata={'origin_task_id': 'child', 'origin_root_task_id': 'ancestor'})
    record = _rows(q)['s1']
    assert 'origin_root_task_id' not in record['task']['metadata']
    assert task_project_membership(q.root, _task_from_schedule(record))[0] == project['id']
    assert pause_project_schedules(q.root, project['id'], project['chat_id'])['schedule_ids'] == ['s1']
    q.queue.check_scheduled_tasks()
    assert q.pending == []
