"""CPU facts are bound-thread endpoints, never watchdog CPU or retrospective dispatch."""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from supervisor import active_activity
from supervisor.active_activity import DirectActivityRegistry, TurnExecution
from supervisor.log_addressing import TurnEventQueue
from ouroboros import server_liveness


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(wall=10., cpu=2., calls=[])
    monkeypatch.setattr(active_activity, "time", SimpleNamespace(monotonic=lambda: value.wall))

    def cpu():
        value.calls.append(threading.current_thread())
        return value.cpu

    monkeypatch.setattr(active_activity, "cpu_clock", cpu)
    return value


def test_bound_receipt_cpu_survives_frame_transport_and_backdated_dispatch(clock):
    turn = TurnExecution(1, owner_turn=True)
    clock.wall, clock.cpu = 12., 3.
    turn.bind_thread()  # same-thread bind preserves the interval
    turn.advance("context_built")
    frames = queue.Queue()
    transport = TurnEventQueue(frames, "t", 1, on_frame=turn.observe)
    clock.wall, clock.cpu = 13., 3.25
    transport.put({"type": "main_llm_call_state", "task_id": "t", "phase": "started", "llm_call_id": "c"})
    clock.wall, clock.cpu = 30., 8.
    transport.put({"type": "main_llm_call_state", "task_id": "t", "phase": "finished", "llm_call_id": "c"})
    frames.get_nowait()
    row = frames.get_nowait()["direct_turn"]
    assert row["phase_cpu_sec"] == {"accepted": 1., "context_built": .25}
    assert row["phases"] == {"accepted": 2., "context_built": 1., "requested": 0.}
    assert row["dispatch"] == "unobserved_call_start"
    assert all(len(mark) == 2 for mark in turn.marks)
    # An explicit historical receipt endpoint may not use this caller's current CPU.
    other = TurnExecution(1)
    assert "phase_cpu_sec" not in other.receipt(until=31.)


def test_foreign_dispatch_and_foreign_end_never_sample_caller_cpu(clock):
    registry = DirectActivityRegistry()
    turn = registry.begin_execution(1)
    clock.wall, clock.cpu = 11., 2.5
    turn.observe("main_llm_call_state", {"phase": "started", "llm_call_id": "c"})
    sampled = len(clock.calls)
    clock.wall, clock.cpu = 15., 999.
    rows = []

    def foreign():
        frame = {"phase": "observed", "llm_call_id": "c", "model_activity": {"phase": "dispatching"}}
        turn.observe("main_llm_call_state", frame)
        rows.append(turn.receipt())
        registry.end_execution(turn)

    thread = threading.Thread(target=foreign)
    thread.start(); thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(clock.calls) == sampled
    assert rows[0]["phase_cpu_sec"] == {"accepted": .5}
    assert "requested" not in rows[0]["phase_cpu_sec"]
    assert turn.phase_cpu(1, turn.ended_at) is None


def test_rebinding_marks_spanning_threads_unknown_then_resumes_own_intervals(clock):
    turn = TurnExecution(1)
    clock.wall, clock.cpu = 11., 3.
    turn.advance("admitted")

    def wake():
        turn.bind_thread()
        clock.wall, clock.cpu = 15., 50.
        turn.advance("started")
        clock.wall, clock.cpu = 17., 50.75
        turn.advance("context_building")

    thread = threading.Thread(target=wake)
    thread.start(); thread.join(timeout=5)
    assert not thread.is_alive()
    row = turn.receipt(until=18.)
    assert row["phase_cpu_sec"] == {"accepted": 1., "started": .75}
    turn.bind_thread()  # back to the original Thread object is still a NEW epoch
    clock.wall, clock.cpu = 19., 4.
    turn.advance("context_built")
    assert "context_building" not in turn.receipt()["phase_cpu_sec"]


def test_watch_reads_only_published_completed_points_including_wakes(clock, monkeypatch):
    registry = DirectActivityRegistry()
    monkeypatch.setattr(active_activity, "get_direct_activity_registry", lambda: registry)
    rows = []
    monkeypatch.setattr(server_liveness._TurnPhaseWatch, "_journal", staticmethod(rows.append))
    turn = registry.begin_execution(1)
    turn.advance("context_building")
    watch = server_liveness._TurnPhaseWatch(1.)
    sampled = len(clock.calls)
    watch.tick(12., {})
    watch.close(13.)
    assert len(clock.calls) == sampled  # even a watcher on the bound thread cannot sample
    assert rows[-1]["then"] == "unwatched" and "thread_cpu_sec" not in rows[-1]
    watch.tick(13., {})
    clock.wall, clock.cpu = 20., 2.75
    turn.advance("context_built")
    sampled = len(clock.calls)
    watch.tick(21., {})
    assert len(clock.calls) == sampled
    closed = next(row for row in rows if row.get("then") == "context_built")
    assert closed["thread_cpu_sec"] == .75 and closed["stood_sec"] == 10.
    watch.tick(22., {})
    clock.wall, clock.cpu = 23., 3.
    registry.end_execution(turn)
    watch.tick(24., {})
    assert rows[-1]["then"] == "ended" and rows[-1]["thread_cpu_sec"] == .25


@pytest.mark.parametrize("mode", ["legacy", "backdated", "unavailable"])
def test_unknown_cpu_does_not_invent_zero_or_replace_primary_result(clock, monkeypatch, mode):
    turn = TurnExecution(1, marks=[("accepted", 9.)]) if mode == "legacy" else TurnExecution(1)
    if mode == "unavailable":
        def unavailable():
            raise OSError("clock unavailable")
        monkeypatch.setattr(active_activity, "cpu_clock", unavailable)
    clock.wall, clock.cpu = 15., 3.
    turn.advance("started", at=12. if mode == "backdated" else None)
    assert turn.phase == "started"
    assert "phase_cpu_sec" not in turn.receipt(until=16.)


def test_real_direct_lane_publishes_own_cpu_without_provider(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from supervisor import workers
    from tests.test_direct_turn_phase_stall import _Actor, _lane, _pass, _answer
    out = _lane(monkeypatch, tmp_path)

    def behave(actor, task):
        _pass(actor, task, "context_building_started")
        before = time.thread_time()
        while time.thread_time() - before < .01:
            sum(range(30))
        _pass(actor, task, "context_building_finished")
        identity = {"type": "main_llm_call_state", "task_id": task["id"], "llm_call_id": "c"}
        actor._event_queue.put({**identity, "phase": "started"})
        actor._event_queue.put({**identity, "phase": "observed", "model_activity": {"phase": "dispatching"}})
        return _answer(task)

    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: _Actor(behave))
    workers.handle_chat_direct(1, "inert fixture", task_metadata={
        "client_message_id": "m", "origin_message_ref": {
            "chat_id": 1, "client_message_id": "m", "ts": "2026-11-12T00:00:00+00:00", "text_sha256": "0" * 64}})
    frames = []
    while not out.bus.empty():
        frames.append(out.bus.get_nowait())
    row = next(frame["direct_turn"] for frame in frames if "direct_turn" in frame)
    assert row["phase_cpu_sec"]["context_building"] >= .01
    assert row["dispatch"] == "observed_send"


def test_real_wait_has_wall_time_without_charging_other_threads():
    turn = TurnExecution(1)
    turn.advance("context_building")
    threading.Event().wait(.03)
    turn.advance("context_built")
    row = turn.receipt()
    assert 0 <= row["phase_cpu_sec"]["context_building"] < row["phases"]["context_building"]
