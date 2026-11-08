"""Project opportunity folding: admission identity and occupancy stay separate."""
import pytest

from ouroboros.project_attention import admitted_project, observe_project


def observe(prior=None, *, ids=(), occupied=False, complete=True, witness=""):
    return observe_project(prior or {}, ordinary_ids=set(ids), occupied=occupied,
                           complete=complete, witness_id=witness)


def test_empty_boot_is_not_an_idle_opportunity():
    assert not observe().get("pending")


def test_busy_to_idle_creates_one_opportunity():
    busy = observe(ids={"ordinary"}, occupied=True, witness="ordinary:1")
    assert busy["eligible"] and not busy.get("pending")
    idle = observe(busy)
    assert idle["pending"]
    considered = admitted_project(idle, "attention")
    assert not observe(considered)["pending"]


def test_short_admission_and_duplicate_release():
    idle = observe(witness="admitted:ordinary")
    assert idle["pending"]
    considered = admitted_project(idle, "attention")
    assert not observe(considered, witness="admitted:ordinary")["pending"]
    assert observe(considered, witness="admitted:later")["pending"]


@pytest.mark.parametrize("kind", ["pending", "held", "child", "run", "service", "upkeep", "unknown"])
def test_any_current_occupancy_defers_the_opportunity(kind):
    busy = observe(witness="admitted:ordinary", occupied=True)
    assert not busy.get("pending"), kind
    assert observe(busy)["pending"]


def test_unreadable_observation_does_not_certify_idle():
    busy = observe(ids={"ordinary"}, occupied=True, witness="ordinary:1")
    assert observe(busy, complete=False) == busy
    assert observe(busy)["pending"]


def test_consideration_and_scouts_block_but_do_not_rearm():
    prior = admitted_project(observe(witness="admitted:ordinary"), "attention")
    # The host excludes the consideration subtree from ordinary witnesses,
    # but still includes it in occupancy. This test checks the fold, not the host.
    scouting = observe(prior, occupied=True)
    assert not scouting["eligible"]
    assert not observe(scouting)["pending"]
    real_work = observe(scouting, ids={"separately-started-work"}, occupied=True, witness="new:1")
    assert observe(real_work)["pending"]


def test_new_work_cancels_pending_until_its_release():
    prior = observe(witness="admitted:first")
    busy = observe(prior, ids={"second"}, occupied=True, witness="second:1")
    assert not busy.get("pending")
    assert observe(busy)["pending"]


def test_admission_requires_actual_identity_and_keeps_input_immutable():
    prior = observe(witness="admitted:ordinary")
    with pytest.raises(ValueError, match="actual task ID"):
        admitted_project(prior, "")
    current = admitted_project(prior, "attention")
    assert prior["pending"]
    assert current["attention_task_id"] == "attention"


def test_unknown_occupancy_retains_short_admission():
    unknown = observe(occupied=True, complete=False, witness="short:1")
    assert unknown["eligible"] and unknown["witness_id"] == "short:1"
    assert not unknown.get("pending")
    idle = observe(unknown)
    assert idle["pending"]
    considered = admitted_project(idle, "attention")
    assert not observe(considered, witness="short:1")["pending"]


def test_project_attention_origin_is_not_an_owner_or_clock():
    from ouroboros.consciousness_authority import (
        consciousness_origin_metadata, is_own_clock_wake,
        is_project_idle_wake, is_self_started_wake, work_kind,
    )
    from ouroboros.task_finalization import wake_cause_title

    metadata = {"initiator": "consciousness", "wake_cause": "project_idle",
                "consciousness_cause": "project_idle", "consciousness_autonomy": "act",
                "project_attention": {"project_id": "example", "name": "Example"}}
    assert is_self_started_wake(metadata)
    assert is_project_idle_wake(metadata)
    assert not is_own_clock_wake(metadata)
    assert work_kind(metadata) == "consciousness"
    assert consciousness_origin_metadata(metadata)["consciousness_cause"] == "project_idle"
    assert consciousness_origin_metadata(metadata, started_root=True)["consciousness_cause"] == "started"
    assert wake_cause_title(metadata) == "Project attention: Example"


from types import SimpleNamespace
from unittest.mock import Mock
from ouroboros import project_attention as attention, store
from tests._store_shared import seed_result


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from supervisor import queue, workers
    from supervisor.active_activity import get_direct_activity_registry
    from supervisor.workspace_preparation import _LIVE
    from supervisor.events_coop_checkpoint import _COOP_CHECKPOINT_INFLIGHT
    from ouroboros.projects_registry import create_project
    from ouroboros import delegate_custody_current, owned_shutdown
    from ouroboros import usage_store
    from tests.test_consciousness_wake_lane import _lane
    _lane(monkeypatch, tmp_path)
    # The lane fixture owns temporary data; prompt bytes come from this source.
    from pathlib import Path
    from ouroboros import config
    monkeypatch.setattr(config, "REPO_DIR", Path(__file__).resolve().parents[1])
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "ADMISSION_RESERVATIONS", {})
    monkeypatch.setattr("supervisor.workspace_preparation._LIVE", {})
    monkeypatch.setattr("supervisor.events_coop_checkpoint._COOP_CHECKPOINT_INFLIGHT", set())
    monkeypatch.setattr(delegate_custody_current, "snapshot", lambda *a, **kw: {"pending": [], "state": {}})
    monkeypatch.setattr(owned_shutdown, "owned_records", lambda *a, **kw: [])
    monkeypatch.setattr(usage_store, "decode_attempts", lambda rows: [])
    registry = get_direct_activity_registry()
    registry.clear()
    project = create_project(tmp_path, "example", name="Example")
    clock = SimpleNamespace(_drive_root=tmp_path, _stopped=False, floor=900,
        _read_state=lambda: {"bg_consciousness_enabled": True},
        _allowance_view=lambda *a, **kw: {"status": "available", "remaining_usd": 10},
        _routing_facts=lambda chat: {}, _next_wake_at=1234, _last_wake_at=1000)
    duty = attention.ProjectAttention(tmp_path, clock)
    yield tmp_path, project, duty, queue, registry
    registry.clear()
    with attention._ADMISSION_LOCK:
        attention._ADMISSIONS.pop(str(tmp_path.resolve()), None)


def lap(duty):
    assert duty._lock.acquire(blocking=False)
    duty._pass()


def episode(root, pid):
    with store.open_store(root, write=False) as txn:
        return txn.get_row(attention.EPISODES, pid)


def ordinary(project, tid="ordinary"):
    return {"id": tid, "project_id": project["id"], "chat_id": project["chat_id"], "metadata": {}}


def test_atomic_dispatch_witness_is_not_import_or_first_write(runtime):
    root, project, duty, queue, registry = runtime
    row = {"task_id": "work", "status": "running", "metadata": {},
           "project_attention_admission": {"project_id": project["id"], "attempt": 1, "mark": "one"}}
    with store.open_store(root, write=True) as txn:
        txn.store_task(row)  # importer/direct upsert: no live update witness
    assert episode(root, project["id"]) is None
    with store.open_store(root, write=True) as txn:
        txn.store_task(row, current={})  # the explicit changed live dispatch mark
    first = episode(root, project["id"])
    assert first["eligible"]
    with store.open_store(root, write=True) as txn:
        attention.consume(txn, project["id"], "attention")
        txn.store_task(row, current=row)
    assert not episode(root, project["id"])["eligible"]


def test_short_direct_admission_survives_until_fold(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    attention.note_admission(root, ordinary(project))
    launches = []
    monkeypatch.setattr(duty, "launch", lambda p, s, **kw: launches.append(p["id"]) or {"admitted": False})
    lap(duty)
    assert launches == [project["id"]]
    assert episode(root, project["id"])["pending"]


def test_queued_and_held_members_block_until_last_release(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    attention.note_admission(root, ordinary(project, "one"))
    queue.PENDING.extend([ordinary(project, "one"), {**ordinary(project, "two"), "_owner_hold": {"reason": "owner"}}])
    launches = Mock(return_value={"admitted": False})
    monkeypatch.setattr(duty, "launch", launches)
    lap(duty)
    assert not launches.called
    queue.PENDING.pop()
    lap(duty)
    assert not launches.called
    queue.PENDING.clear()
    lap(duty)
    assert launches.call_count == 1


@pytest.mark.parametrize("source", ["preparation", "reservation", "direct_preparing", "pending_inputs", "unknown_inputs"])
def test_pre_admission_and_unknown_inputs_never_certify_idle(runtime, monkeypatch, source):
    root, project, duty, queue, registry = runtime
    attention.note_admission(root, ordinary(project))
    launches = Mock()
    monkeypatch.setattr(duty, "launch", launches)
    if source == "preparation":
        monkeypatch.setattr("supervisor.workspace_preparation._LIVE", {"token": object()})
    elif source == "reservation":
        queue.ADMISSION_RESERVATIONS["token"] = "token"
    elif source == "direct_preparing":
        registry.begin_execution(project["chat_id"])
    else:
        duty.tick(ready=True, pending_inputs={"work"} if source == "pending_inputs" else None)
        assert duty._lock.acquire(blocking=False)
        duty._lock.release()
        assert not launches.called
        return
    lap(duty)
    assert not launches.called


@pytest.mark.parametrize("state", ["running", "unknown", "exited"])
def test_natural_retained_service_exit_is_observed_without_store_write(runtime, monkeypatch, state):
    root, project, duty, queue, registry = runtime
    from ouroboros import owned_shutdown, process_custody
    seed_result(root, "ordinary", {"status": "completed", "chat_id": project["chat_id"], "project_id": project["id"]})
    entry = {"owner_task": "ordinary"}
    monkeypatch.setattr(owned_shutdown, "owned_records", lambda *a, **kw: [entry])
    observed = {"state": "live"}
    monkeypatch.setattr(process_custody, "owned_process_state", lambda *a, **kw: observed["state"])
    attention.note_admission(root, ordinary(project))
    launches = Mock(return_value={"admitted": False})
    monkeypatch.setattr(duty, "launch", launches)
    lap(duty)
    assert not launches.called
    observed["state"] = {"running": "live", "unknown": "unknown", "exited": "ended"}[state]
    lap(duty)
    assert launches.call_count == (1 if state == "exited" else 0)


def test_real_direct_lane_binds_project_before_no_work_body_and_does_not_recurse(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent
    from tests.test_consciousness_wake_lane import _wait_for
    seen = []
    class Actor:
        def handle_task(self, task):
            with store.open_store(root, write=False) as txn:
                bound = txn.get_row(attention.EPISODES, project["id"])
            seen.append((task, bound))
            seed_result(root, task["id"], {"status": "completed", "chat_id": task["chat_id"], "metadata": task["metadata"]})
            return []
    monkeypatch.setattr(agent, "make_agent", lambda **kw: Actor())
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert duty.last_result[project['id']]['admitted'], duty.last_result
    assert _wait_for(lambda: bool(seen) and not registry.actors())
    task, bound = seen[0]
    assert task["project_id"] == project["id"] and task["chat_id"] == project["chat_id"]
    assert task["metadata"]["wake_cause"] == "project_idle"
    assert task["metadata"]["model_role"] == "consciousness"
    assert task["metadata"]["project_attention"]["name"] == "Example"
    assert bound["attention_task_id"] == task["id"] and not bound["eligible"]
    lap(duty)
    lap(duty)
    assert len(seen) == 1
    assert duty.clock._next_wake_at == 1234 and duty.clock._last_wake_at == 1000
    attention.note_admission(root, ordinary(project, "later"))
    lap(duty)
    assert _wait_for(lambda: len(seen) == 2 and not registry.actors())


@pytest.mark.parametrize("cause", ["project_idle", "started"])
def test_attention_research_does_not_arm_but_independent_work_does(runtime, cause):
    root, project, duty, queue, registry = runtime
    attention.note_admission(root, {**ordinary(project), "metadata": {"consciousness_cause": cause}})
    attention.flush_admissions(root)
    assert bool(episode(root, project["id"])) == (cause == "started")


def test_required_binding_failure_starts_no_body(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent
    from supervisor import workers
    seen = Mock()
    monkeypatch.setattr(agent, "make_agent", lambda **kw: SimpleNamespace(handle_task=seen))
    def broken(task):
        raise OSError("episode unavailable")
    receipt = workers.handle_wake_direct(project["chat_id"], "opportunity",
        {"initiator": "consciousness", "wake_cause": "project_idle", "consciousness_cause": "project_idle", "project_id": project["id"]},
        bind_input=broken, bind_required=True)
    assert receipt["reason"] == "input_binding_failed"
    assert not seen.called and not registry.actors()


@pytest.mark.parametrize("barrier", ["disabled", "allowance", "admission_race"])
def test_launch_refusal_preserves_episode_and_clock(runtime, monkeypatch, barrier):
    root, project, duty, queue, registry = runtime
    attention.note_admission(root, ordinary(project))
    if barrier == "disabled":
        duty.clock._stopped = True
    elif barrier == "allowance":
        duty.clock._allowance_view = lambda *a, **kw: {"status": "exhausted", "remaining_usd": 0}
    else:
        from supervisor import workers
        real = workers.handle_wake_direct
        def race(*a, **kw):
            queue.PENDING.append(ordinary(project, "racer"))
            return real(*a, **kw)
        monkeypatch.setattr(workers, "handle_wake_direct", race)
    lap(duty)
    assert episode(root, project["id"])["pending"]
    assert not registry.actors()
    assert duty.clock._next_wake_at == 1234


def test_normal_schema9_upgrade_creates_current_launch_index_without_rederivation(runtime):
    from ouroboros import store_hot_facts
    from ouroboros.store_import import upgrade_current_schema
    root, project, duty, queue, registry = runtime
    with store.open_store(root, write=True) as txn:
        txn.set_meta('schema_version', 9)
        before = txn.meta('job:' + store_hot_facts.JOB)
        txn.conn.execute('DROP INDEX task_handoffs_launch')
    upgrade_current_schema(root)
    with store.open_store(root, write=False) as txn:
        assert txn.meta('schema_version') == 10
        assert txn.meta('job:' + store_hot_facts.JOB) == before
        assert not list(txn.conn.execute(f"SELECT task_id FROM task_handoffs INDEXED BY task_handoffs_launch WHERE {store_hot_facts.LAUNCH_CURRENT}"))
    attention.current_owners(root, [], [])


@pytest.mark.parametrize('barrier', ['disabled', 'allowance', 'gate'])
def test_releasing_only_the_refused_control_revisits_pending_episode(runtime, monkeypatch, barrier):
    from ouroboros import agent
    from supervisor import workers
    from tests.test_consciousness_wake_lane import _wait_for
    root, project, duty, queue, registry = runtime
    blocked = {'value': True}
    if barrier == 'disabled':
        duty.clock._read_state = lambda: {'bg_consciousness_enabled': not blocked['value']}
    elif barrier == 'allowance':
        duty.clock._allowance_view = lambda *a, **kw: {'status': 'available', 'remaining_usd': 0 if blocked['value'] else 10}
    else:
        monkeypatch.setattr(workers, 'repo_writer_admission_closed', lambda: 'update' if blocked['value'] else '')
    seen = []
    class Actor:
        def handle_task(self, task):
            seen.append(task)
            return []
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: Actor())
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)  # absorb our own fold; no unrelated store/queue change releases it
    assert not seen and episode(root, project['id'])['pending']
    blocked['value'] = False
    lap(duty)
    assert duty.last_result[project['id']]['admitted'], duty.last_result
    assert _wait_for(lambda: len(seen) == 1 and not registry.actors())


@pytest.mark.parametrize('source', ['preparation', 'checkpoint', 'synthesis', 'store'])
def test_changed_current_owner_before_registration_starts_no_attention(runtime, monkeypatch, source):
    from supervisor import workers
    from ouroboros import agent
    root, project, duty, queue, registry = runtime
    seen = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    real = workers.handle_wake_direct
    def race(*a, **kw):
        if source == 'preparation':
            monkeypatch.setattr('supervisor.workspace_preparation._LIVE', {'late': object()})
        elif source == 'checkpoint':
            monkeypatch.setattr('supervisor.events_coop_checkpoint._COOP_CHECKPOINT_INFLIGHT', {'late'})
        elif source == 'synthesis':
            monkeypatch.setattr('ouroboros.post_task_checkpoint.POST_TASK_SYNTHESIS_INFLIGHT', {(str(root.resolve()), 'late')})
        else:
            with store.open_store(root, write=True) as txn:
                txn.store_task({'task_id': 'late', 'status': 'completed', 'chat_id': project['chat_id'],
                                'project_id': project['id'], 'launch_handoffs': {'claim': {'state': 'dispatched'}}})
        return real(*a, **kw)
    monkeypatch.setattr(workers, 'handle_wake_direct', race)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not seen.called and not registry.actors()
    assert episode(root, project['id'])['pending']


def test_old_ordinary_membership_flicker_without_admission_does_not_rearm():
    done = admitted_project(observe(witness='one:1'), 'attention')
    busy = observe(done, ids={'one'}, occupied=True)
    assert not observe(busy)['pending']


def test_project_close_after_snapshot_before_registration_starts_no_attention(runtime, monkeypatch):
    from supervisor import workers
    from ouroboros import agent
    from ouroboros.projects_registry import begin_project_deletion
    root, project, duty, queue, registry = runtime
    seen = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    real = workers.handle_wake_direct
    def race(*a, **kw):
        assert begin_project_deletion(root, project['id'])['lifecycle'] == 'deleting'
        return real(*a, **kw)
    monkeypatch.setattr(workers, 'handle_wake_direct', race)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not seen.called and not registry.actors()
    assert episode(root, project['id'])['pending']


def test_returned_claim_and_terminal_upkeep_history_do_not_load_task_bodies(runtime, monkeypatch):
    from ouroboros import task_store
    root, project, duty, queue, registry = runtime
    with store.open_store(root, write=True) as txn:
        for i in range(300):
            txn.store_task({'task_id': f'old-{i}', 'status': 'completed',
                'root_phase_checkpoint': {'post_task_synthesis': 'completed'},
                'launch_handoffs': {'old': {'state': 'returned', 'host_unconfirmed': False,
                                          'external_pending': {}, 'control_receipts': []}}})
        txn.store_task({'task_id': 'active-claim', 'status': 'completed',
            'launch_handoffs': {'active': {'state': 'dispatched'}}})
        txn.store_task({'task_id': 'unknown-release', 'status': 'completed',
            'launch_handoffs': {'unknown': {'state': 'returned', 'host_unconfirmed': True}}})
    read = task_store.read_document
    bodies = []
    def traced(source, tid, *a, **kw):
        bodies.append(tid)
        return read(source, tid, *a, **kw)
    monkeypatch.setattr(task_store, 'read_document', traced)
    owners, gaps = attention.current_owners(root, [], [])
    assert not gaps
    assert set(owners) == {'active-claim', 'unknown-release'}
    assert set(bodies) == {'active-claim', 'unknown-release'}


def test_unrelated_store_changes_during_observation_do_not_starve_idle_project(runtime, monkeypatch):
    from ouroboros import agent
    from tests.test_consciousness_wake_lane import _wait_for
    root, project, duty, queue, registry = runtime
    read = attention.current_owners
    counter = []
    def changing(*a, **kw):
        counter.append(1)
        with store.open_store(root, write=True) as txn:
            txn.put_row('unrelated-project-fixture', 'content', {'revision': len(counter)})
        return read(*a, **kw)
    monkeypatch.setattr(attention, 'current_owners', changing)
    seen = []
    class Actor:
        def handle_task(self, task):
            seen.append(task)
            return []
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: Actor())
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert duty.last_result[project['id']]['admitted'], duty.last_result
    assert _wait_for(lambda: len(seen) == 1 and not registry.actors())
    assert len(counter) == 2  # both prepared and final current-custody reads


def test_pre_dispatch_origin_metadata_scout_cannot_arm(runtime):
    root, project, duty, queue, registry = runtime
    # schedule_subagent carries this field before task_dispatch builds metadata.
    attention.note_admission(root, {**ordinary(project), 'origin_metadata': {
        'initiator': 'consciousness', 'consciousness_cause': 'project_idle'}})
    attention.flush_admissions(root)
    assert episode(root, project['id']) is None


def test_dispatch_projection_uses_prepared_scout_origin_not_old_result_metadata(runtime):
    from supervisor.task_admission import project_dispatch_projection
    root, project, duty, queue, registry = runtime
    task = {**ordinary(project, 'scout'), 'metadata': {'consciousness_cause': 'project_idle'}}
    projection = project_dispatch_projection(task)
    marked = projection.project({'status': 'scheduled', 'metadata': {}}, {})
    assert marked['admitted_dispatch'] == 'possible'
    assert 'project_attention_admission' not in marked


def test_required_binding_refusal_does_not_publish_an_orphan_running_result(runtime, monkeypatch):
    from ouroboros import agent, task_store
    from supervisor import workers
    root, project, duty, queue, registry = runtime
    seen, captured = Mock(), []
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    def broken(task):
        captured.append(task['id'])
        raise OSError('binding unavailable')
    receipt = workers.handle_wake_direct(project['chat_id'], 'opportunity', {
        'initiator': 'consciousness', 'wake_cause': 'project_idle',
        'consciousness_cause': 'project_idle', 'project_id': project['id'],
        'project_attention': {'name': 'Example'}}, bind_input=broken, bind_required=True)
    assert receipt['reason'] == 'input_binding_failed' and not seen.called and not registry.actors()
    assert captured and task_store.read_document(root, captured[0]) is None


def test_upkeep_reservation_release_without_store_write_revisits_idle(runtime, monkeypatch):
    from ouroboros import agent
    from tests.test_consciousness_wake_lane import _wait_for
    root, project, duty, queue, registry = runtime
    seed_result(root, 'upkeep', {'status': 'completed', 'chat_id': project['chat_id'], 'project_id': project['id']})
    upkeep = {(str(root.resolve()), 'upkeep'): None}
    monkeypatch.setattr('ouroboros.post_task_checkpoint.POST_TASK_SYNTHESIS_INFLIGHT', upkeep)
    seen = []
    class Actor:
        def handle_task(self, task):
            seen.append(task)
            return []
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: Actor())
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)
    assert not seen
    upkeep.clear()
    lap(duty)
    assert _wait_for(lambda: len(seen) == 1 and not registry.actors())


def test_recent_wake_published_cause_contract_includes_project_idle():
    from typing import get_args, get_type_hints
    from ouroboros.gateway.client_contracts import RecentWake
    assert set(get_args(get_type_hints(RecentWake)['cause'])) == {'own', 'focus', 'project_idle'}
