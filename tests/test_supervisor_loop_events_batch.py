"""The supervisor loop's events phase is BOUNDED and pays one budget-projection write per turn.

Two starvation classes of the same live incident: a producer that keeps the worker
event queue non-empty used to hold the loop inside its drain until the queue was
empty, so an owner message waited behind every queued event; and every ``llm_usage``
event paid its own ledger render plus STATE_LOCK write. One events pass now drains at
most N events or T seconds (``runtime_limits``), FIFO, the remainder next turn; bridge
intake runs every turn and only polls when the pass stopped at its bound (its 1 s wait
would idle a turn that has work); and the compatibility projection is written once per
turn after intake, kept dirty on failure and retried no more often than a bounded interval.
Every guard is pinned in both directions.
"""

from __future__ import annotations

import queue
import time
from types import SimpleNamespace

import pytest

from ouroboros import server_liveness as sl


def _ctx(dispatched: list):
    return SimpleNamespace(dispatched=dispatched, restarts=[], DRIVE_ROOT=None)


@pytest.fixture
def dispatch_spy(monkeypatch):
    """Route ``dispatch_event`` to the ctx so a test sees order and count."""
    from supervisor import events as events_mod

    monkeypatch.setattr(events_mod, "dispatch_event", lambda evt, ctx: ctx.dispatched.append(evt["n"]))


def _liveness():
    return [time.monotonic(), {}, time.thread_time(), None]


def test_one_events_pass_is_count_bounded_and_intake_runs_with_events_still_queued(monkeypatch, dispatch_spy):
    """A producer that keeps the queue non-empty still lets an owner message be taken
    by intake within one turn: the pass stops at its bound, the remainder stays queued
    in FIFO order and the turn's next step (intake) sees the queue non-empty."""
    monkeypatch.setattr(sl, "SUPERVISOR_EVENT_BATCH_MAX_EVENTS", 5)
    monkeypatch.setattr(sl, "SUPERVISOR_EVENT_BATCH_MAX_SEC", 60.0)
    event_q: "queue.Queue" = queue.Queue()
    for n in range(12):
        event_q.put({"type": "worker_event", "n": n, "ts": "2026-09-25T00:00:00+00:00"})
    ctx = _ctx([])
    liveness = _liveness()
    inbox = ["owner message"]
    turns = []

    def one_turn():
        backlog = sl.drain_worker_events(event_q, ctx, liveness, on_restart=lambda evt, c: None)
        taken = inbox.pop() if inbox else None  # bridge intake of this turn
        turns.append((backlog, event_q.qsize(), taken))

    one_turn()
    assert turns == [(True, 7, "owner message")], turns
    assert ctx.dispatched == [0, 1, 2, 3, 4]
    assert liveness[sl._LAG] is not None, "drained events are lag-observed"
    one_turn()
    one_turn()
    assert ctx.dispatched == list(range(12))
    assert turns[1:] == [(True, 2, None), (False, 0, None)]


def test_one_events_pass_is_time_bounded(monkeypatch):
    from supervisor import events as events_mod

    monkeypatch.setattr(sl, "SUPERVISOR_EVENT_BATCH_MAX_EVENTS", 1000)
    monkeypatch.setattr(sl, "SUPERVISOR_EVENT_BATCH_MAX_SEC", 0.05)
    monkeypatch.setattr(events_mod, "dispatch_event",
                        lambda evt, ctx: (ctx.dispatched.append(evt["n"]), time.sleep(0.03)))
    event_q: "queue.Queue" = queue.Queue()
    for n in range(50):
        event_q.put({"type": "worker_event", "n": n})
    ctx = _ctx([])

    assert sl.drain_worker_events(event_q, ctx, _liveness(), on_restart=lambda evt, c: None) is True
    assert 1 <= len(ctx.dispatched) < 50
    assert event_q.qsize() == 50 - len(ctx.dispatched)


def test_restart_requests_are_routed_and_an_empty_queue_ends_the_pass(dispatch_spy):
    event_q: "queue.Queue" = queue.Queue()
    event_q.put({"type": "worker_event", "n": 1})
    event_q.put({"type": "restart_request", "n": 2})
    event_q.put({"type": "worker_event", "n": 3})
    ctx = _ctx([])

    backlog = sl.drain_worker_events(
        event_q, ctx, _liveness(), on_restart=lambda evt, c: c.restarts.append(evt["n"]))

    assert backlog is False and ctx.dispatched == [1, 3] and ctx.restarts == [2]


def test_a_dequeued_send_is_released_after_its_handler_returned_or_raised(monkeypatch, caplog):
    """The consumer owns the one release of a send's delivery membership: after the
    handler returned, after it raised, and after a restart intercept alike. A bus
    without the membership (a plain queue) is fail-soft, logged once per class."""
    import logging

    from supervisor import events as events_mod
    from supervisor.event_observation import ObservedEventQueue

    did = "final:t:0123456789abcdef"

    def dispatch(evt, ctx):
        ctx.dispatched.append(evt["n"])
        if evt.get("boom"):
            raise RuntimeError("handler failed")

    monkeypatch.setattr(events_mod, "dispatch_event", dispatch)
    bus = ObservedEventQueue()
    bus.put({"type": "send_message", "n": 1, "task_id": "t", "delivery_id": did, "boom": True})
    bus.put({"type": "send_message", "n": 2, "task_id": "t", "delivery_id": did})
    bus.put({"type": "restart_request", "n": 3, "delivery_id": did})
    ctx = _ctx([])
    with pytest.raises(RuntimeError):
        sl.drain_worker_events(bus, ctx, _liveness(), on_restart=lambda evt, c: c.restarts.append(evt["n"]))
    assert ctx.dispatched == [1] and bus.delivery_outstanding(did) is True, "one of two puts released"
    assert sl.drain_worker_events(bus, ctx, _liveness(), on_restart=lambda evt, c: c.restarts.append(evt["n"])) is False
    assert ctx.dispatched == [1, 2] and ctx.restarts == [3]
    assert bus.delivery_outstanding(did) is False

    monkeypatch.setattr(sl, "_RELEASE_FAILURES_SEEN", set())
    plain: "queue.Queue" = queue.Queue()
    for n in (4, 5):
        plain.put({"type": "send_message", "n": n, "task_id": "t", "delivery_id": did})
    with caplog.at_level(logging.WARNING, logger=sl.log.name):
        assert sl.drain_worker_events(plain, ctx, _liveness(), on_restart=lambda evt, c: None) is False
    assert ctx.dispatched == [1, 2, 4, 5]
    assert sum("Delivery membership release failed" in rec.message for rec in caplog.records) == 1


def test_flush_writes_once_when_dirty_and_clears_only_on_success(monkeypatch):
    writes = []
    ctx = SimpleNamespace(budget_projection_dirty=True,
                          update_budget_from_usage=lambda usage: writes.append(usage) or True)

    sl.flush_budget_projection(ctx)
    assert writes == [{}] and ctx.budget_projection_dirty is False
    sl.flush_budget_projection(ctx)
    assert writes == [{}], "a clean context pays nothing"

    monkeypatch.setattr(sl, "BUDGET_PROJECTION_RETRY_SEC", 30.0)
    clock = [1000.0]
    monkeypatch.setattr(sl.time, "monotonic", lambda: clock[0])
    outcomes = [False, RuntimeError("ledger unreadable"), True]

    def writer(usage):
        writes.append(usage)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ctx = SimpleNamespace(budget_projection_dirty=True, update_budget_from_usage=writer)
    sl.flush_budget_projection(ctx)
    assert len(writes) == 2 and ctx.budget_projection_dirty is True, "a refused write keeps the flag"
    sl.flush_budget_projection(ctx)
    assert len(writes) == 2, "no retry inside the bounded interval"
    clock[0] += 30.0
    sl.flush_budget_projection(ctx)
    assert len(writes) == 3 and ctx.budget_projection_dirty is True, "an exception keeps the flag too"
    clock[0] += 30.0
    sl.flush_budget_projection(ctx)
    assert len(writes) == 4 and ctx.budget_projection_dirty is False and not outcomes


def test_n_llm_usage_events_in_one_turn_produce_one_write_after_intake(monkeypatch, tmp_path):
    """N events in one drain -> one writer call, after bridge intake; the event
    rows carry ``deferred``."""
    import json

    from supervisor import events as events_mod

    (tmp_path / "logs").mkdir()
    order = []
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, dispatched=[], RUNNING={},
                          bridge=SimpleNamespace(push_log=lambda payload: None),
                          update_budget_from_usage=lambda usage: order.append("write") or True)
    monkeypatch.setattr(events_mod, "dispatch_event",
                        lambda evt, c: events_mod._handle_llm_usage(evt, c))
    event_q: "queue.Queue" = queue.Queue()
    for n in range(8):
        event_q.put({"type": "llm_usage", "task_id": f"t{n}", "usage": {"prompt_tokens": 1, "cost": 0.01}})

    sl.drain_worker_events(event_q, ctx, _liveness(), on_restart=lambda evt, c: None)
    order.append("intake")
    sl.flush_budget_projection(ctx)

    assert order == ["intake", "write"]
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 8 and {row["projection_update_status"] for row in rows} == {"deferred"}
    assert ctx.budget_projection_dirty is False


class _InboxBridge:
    """The chat bridge the loop opens, over a real inbox: every intake call records its wait,
    what it took and the loop thread's clocks at entry, and blocks exactly as the bridge does
    (``queue.Queue.get`` with that timeout), so an idle tick really waits."""

    def __init__(self, queued=()):
        self._broadcast_fn = None
        self._inbox: "queue.Queue" = queue.Queue()
        for item in queued:
            self._inbox.put(item)
        self.calls: list = []

    def get_updates(self, offset, timeout=10):
        entered, cpu = time.monotonic(), time.thread_time()
        try:
            taken = self._inbox.get(timeout=timeout)
        except queue.Empty:
            taken = None
        self.calls.append((timeout, taken, entered, cpu))
        return []  # what intake took is recorded; routing it is not this test's subject


def _ticks(monkeypatch, tmp_path, backlogs, *, queued=(), real_sleep=False):
    """Run the REAL ``server._run_supervisor`` (its collaborators stubbed by the shutdown suite's
    harness) for one tick per scripted drain verdict; returns the bridge and the idle sleeps."""
    import server
    from supervisor import message_bus as bus_mod, workers as workers_mod
    from tests.test_server_shutdown import _run, _supervisor_harness

    intake = server._process_bridge_updates
    rec = _supervisor_harness(monkeypatch, tmp_path, ["ok"] * (len(backlogs) - 1) + ["stop"])
    bridge, verdicts, sleeps = _InboxBridge(queued), list(backlogs), []
    monkeypatch.setattr(workers_mod, "get_event_q_wake", lambda: None)
    monkeypatch.setattr(bus_mod, "LocalChatBridge", lambda _settings: bridge)
    monkeypatch.setattr(server, "_dialogue_bridge", None)
    monkeypatch.setattr(server, "_process_bridge_updates", intake)
    monkeypatch.setattr(server, "drain_worker_events", lambda *_a, **_k: verdicts.pop(0))
    monkeypatch.setattr(server, "time", SimpleNamespace(
        sleep=lambda sec: (sleeps.append(sec), time.sleep(sec) if real_sleep else None),
        monotonic=time.monotonic, time=time.time, thread_time=time.thread_time))
    _run(rec, server)
    assert not verdicts, "one tick per scripted drain verdict"
    return bridge, sleeps


def test_a_tick_whose_drain_stopped_at_its_bound_polls_intake_and_an_idle_tick_keeps_its_wait(monkeypatch, tmp_path):
    """Under a backlog the owner message is still taken in the same tick, without the 1 s wait;
    an idle tick keeps today's wait, and only it pays the idle sleep."""
    bridge, sleeps = _ticks(monkeypatch, tmp_path, [True, False, True], queued=["owner message"])
    assert [(timeout, taken) for timeout, taken, *_ in bridge.calls] == [
        (0, "owner message"), (1, None), (0, None)], bridge.calls
    assert sleeps == [0.5]


def test_an_idle_loop_waits_on_its_inbox_without_spinning(monkeypatch, tmp_path):
    """One idle tick, intake to intake, is the inbox wait plus the idle sleep, and the loop
    thread spends that time blocked: its CPU over the tick stays far below the wall."""
    bridge, sleeps = _ticks(monkeypatch, tmp_path, [False, False], real_sleep=True)
    (_t0, _x0, wall0, cpu0), (_t1, _x1, wall1, cpu1) = bridge.calls
    assert [call[0] for call in bridge.calls] == [1, 1] and sleeps == [0.5, 0.5]
    assert wall1 - wall0 >= 1.4, "the idle wait and the idle sleep both ran"
    assert cpu1 - cpu0 < 0.5, f"an idle tick burned {cpu1 - cpu0:.3f}s of CPU in {wall1 - wall0:.3f}s"


def test_control_lane_event_wakes_idle_loop_and_telemetry_does_not(monkeypatch, tmp_path):
    """A CONTROL_LANE put wakes ObservedEventQueue's wake event; TELEMETRY_LANE does not.
    In the real supervisor loop, a control event arriving during the idle intake wait
    wakes the loop and dispatches without waiting out the 1.5 s idle cadence."""
    import threading
    import server
    from supervisor import message_bus as bus_mod, workers as workers_mod
    from supervisor.event_observation import ObservedEventQueue
    from tests.test_server_shutdown import _run, _supervisor_harness

    wake = threading.Event()
    bus = ObservedEventQueue(wake_event=wake)

    bus.put({"type": "task_heartbeat", "task_id": "t1"})
    assert wake.is_set() is False, "telemetry must not wake the idle loop"
    bus.get_nowait()

    bus.put({"type": "typing_start", "task_id": "t1"})
    assert wake.is_set() is False, "non-routed control telemetry/UI status must not wake the idle loop"
    bus.get_nowait()

    bus.put({"type": "steer_task", "task_id": "t1"})
    assert wake.is_set() is True, "control-lane event must wake the idle loop"
    bus.get_nowait()
    wake.clear()

    # LocalChatBridge: wakes on control event (returns []) and on enqueue() (returns msg),
    # and enqueue() survives a failing wake.set().
    bridge = bus_mod.LocalChatBridge({})
    bridge.set_wake_event(wake)
    t = threading.Timer(0.03, lambda: bus.put({"type": "steer_task", "task_id": "t2"}))
    t.start()
    t0 = time.monotonic()
    updates = bridge.get_updates(0, timeout=1)
    dt = time.monotonic() - t0
    t.join()
    assert updates == [] and dt < 0.75, f"control wake took {dt:.3f}s"
    bus.get_nowait()
    wake.clear()

    bridge.enqueue_local_message("hello", chat_id=1, user_id=1)
    assert wake.is_set() is True
    got = bridge.get_updates(0, timeout=1)
    assert len(got) == 1 and got[0]["message"]["text"] == "hello"

    class _BrokenWake:
        def set(self):
            raise RuntimeError("boom")

        def wait(self, timeout=None):
            return False

    bridge.set_wake_event(_BrokenWake())
    bridge.enqueue_local_message("still queued", chat_id=1, user_id=1)
    assert len(bridge.get_updates(1, timeout=0)) == 1

    # End-to-end in server._run_supervisor with real _process_bridge_updates + LocalChatBridge + ObservedEventQueue
    wake2 = threading.Event()
    real_q = ObservedEventQueue(wake_event=wake2)
    real_bridge = bus_mod.LocalChatBridge({})
    dispatched = []
    emit_at = []
    dispatch_at = []
    intake = server._process_bridge_updates
    rec = _supervisor_harness(monkeypatch, tmp_path, ["ok", "ok", "stop"])
    monkeypatch.setattr(server, "_process_bridge_updates", intake)
    monkeypatch.setattr(workers_mod, "get_event_q", lambda *, create=True: real_q)
    monkeypatch.setattr(workers_mod, "get_event_q_wake", lambda: wake2, raising=False)
    monkeypatch.setattr(bus_mod, "LocalChatBridge", lambda _settings: real_bridge)
    monkeypatch.setattr(server, "_dialogue_bridge", None)
    from supervisor import events as events_mod
    monkeypatch.setattr(events_mod, "dispatch_event",
                        lambda evt, _ctx: (dispatched.append(evt["type"]), dispatch_at.append(time.monotonic())))

    def _emit():
        emit_at.append(time.monotonic())
        real_q.put({"type": "steer_task", "task_id": "t3"})

    t_emit = threading.Timer(0.05, _emit)
    t_emit.start()
    _run(rec, server)
    t_emit.join()
    assert dispatched == ["steer_task"]
    assert emit_at and dispatch_at
    dispatch_dt = dispatch_at[0] - emit_at[0]
    assert dispatch_dt < 0.75, f"wakeable supervisor loop took {dispatch_dt:.3f}s to dispatch"


def test_sync_manager_event_queue_wakes_cross_process():
    """Real EventQueueManager + mp.Event wakes on CONTROL_LANE put across processes."""
    import multiprocessing as mp
    import sys
    from supervisor.event_observation import EventQueueManager, set_manager_wake_event

    ctx = mp.get_context("forkserver" if sys.platform.startswith("linux") else "spawn")
    wake = ctx.Event()
    mgr = EventQueueManager(ctx=ctx)
    mgr.start(initializer=set_manager_wake_event, initargs=(wake,))
    try:
        q = mgr.Queue()
        q.put_nowait({"type": "task_heartbeat", "task_id": "t1"})
        assert wake.wait(timeout=0.05) is False
        q.get_nowait()

        t0 = time.monotonic()
        q.put_nowait({"type": "steer_task", "task_id": "t1"})
        assert wake.wait(timeout=1.0) is True
        assert time.monotonic() - t0 < 0.75
        assert q.get_nowait()["type"] == "steer_task"
    finally:
        mgr.shutdown()

