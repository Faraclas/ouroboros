"""CPU facts are bound-thread endpoints, never watchdog CPU or retrospective dispatch."""
from __future__ import annotations

import queue
import sys
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
    value = SimpleNamespace(wall=10., cpu=2., calls=[], sched={"nvcsw": 10})
    monkeypatch.setattr(active_activity, "_sched_facts", lambda: dict(value.sched))
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
    assert row["phase_sched"] == {"accepted": {"nvcsw": 0}, "context_built": {"nvcsw": 0}}
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
    assert "requested" not in rows[0]["phase_sched"]
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
    assert set(row["phase_sched"]) == {"accepted", "started"}
    turn.bind_thread()  # back to the original Thread object is still a NEW epoch
    clock.wall, clock.cpu = 19., 4.
    turn.advance("context_built")
    assert "context_building" not in turn.receipt()["phase_cpu_sec"]
    assert "context_building" not in turn.receipt()["phase_sched"]


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
    assert "thread_sched" not in rows[-1]
    watch.tick(13., {})
    clock.wall, clock.cpu = 20., 2.75
    clock.sched = {"nvcsw": 13, "run_delay_ns": 1000}
    turn.advance("context_built")
    sampled = len(clock.calls)
    watch.tick(21., {})
    assert len(clock.calls) == sampled
    closed = next(row for row in rows if row.get("then") == "context_built")
    assert closed["thread_cpu_sec"] == .75 and closed["stood_sec"] == 10.
    assert closed["thread_sched"] == {"nvcsw": 3}  # no baseline run-delay observation
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
    assert "phase_sched" not in turn.receipt(until=16.)


def test_real_direct_lane_publishes_own_cpu_without_provider(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from supervisor import workers
    from tests.test_direct_turn_phase_stall import _Actor, _lane, _pass, _answer
    out = _lane(monkeypatch, tmp_path)
    monkeypatch.setattr(active_activity, "_sched_facts", lambda: {"nvcsw": 42})

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
    assert row["phase_sched"]["context_building"] == {"nvcsw": 0}
    assert row["dispatch"] == "observed_send"


def test_real_wait_has_wall_time_without_charging_other_threads():
    turn = TurnExecution(1)
    turn.advance("context_building")
    threading.Event().wait(.03)
    turn.advance("context_built")
    row = turn.receipt()
    assert 0 <= row["phase_cpu_sec"]["context_building"] < row["phases"]["context_building"]


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux scheduling counters")


def _timed_phase(work) -> dict:
    turn = TurnExecution(1)
    turn.advance("context_building")
    work()
    turn.advance("context_built")
    return turn.receipt()


@linux_only
def test_real_blocked_thread_records_switch_activity():
    release = threading.Event()
    timer = threading.Timer(.15, release.set)
    timer.start()
    try:
        row = _timed_phase(lambda: release.wait(5))
    finally:
        release.set()
        timer.join(timeout=5)
    sched = row["phase_sched"]["context_building"]
    assert sched["nvcsw"] >= 1
    assert all(value >= 0 for value in sched.values())


@linux_only
def test_busy_thread_spends_the_phase_on_cpu():
    def spin():
        start = time.thread_time()
        while time.thread_time() - start < .1:
            sum(range(100))
    row = _timed_phase(spin)
    assert row["phase_cpu_sec"]["context_building"] >= .1
    assert all(value >= 0 for value in row["phase_sched"]["context_building"].values())


def test_unreadable_scheduling_sources_are_absent_never_zero(monkeypatch):
    def refuse(path):
        raise OSError(path)
    monkeypatch.setattr(active_activity, "_read_first", refuse)
    facts = active_activity._sched_facts()
    assert "run_delay_ns" not in facts and "throttled_usec" not in facts
    turn = TurnExecution(1)
    t0 = turn.marks[0][1]
    turn._cpu_marks["accepted"] = (t0, turn._cpu_binding, 1., {"nvcsw": 1})
    turn._cpu_marks["context_building"] = (t0 + 1, turn._cpu_binding, 2., {"nvcsw": 4})
    turn.marks.append(("context_building", t0 + 1))
    assert turn.phase_sched(0, t0 + 1) == {"nvcsw": 3}
    legacy = TurnExecution(1)
    legacy._cpu_marks["accepted"] = (legacy.marks[0][1], legacy._cpu_binding, 1.)
    assert legacy.phase_sched(0, legacy.marks[0][1]) == {}


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_sampling_does_not_open_linux_sources(monkeypatch, platform):
    monkeypatch.setattr(active_activity.sys, "platform", platform)
    def unexpected(path):
        pytest.fail(f"non-Linux sampling opened {path}")
    monkeypatch.setattr(active_activity, "_read_first", unexpected)
    turn = TurnExecution(1)
    turn.advance("context_built")
    assert "phase_sched" not in turn.receipt()


@linux_only
def test_sampling_parses_sources_and_preserves_partial_unknowns(monkeypatch):
    sources = {"/proc/thread-self/schedstat": "100 2000 3\n",
               active_activity._THROTTLE_STAT: "usage_usec 99\nthrottled_usec 500\n"}
    monkeypatch.setattr(active_activity, "_read_first", sources.__getitem__)
    facts = active_activity._sched_facts()
    assert facts["run_delay_ns"] == 2000 and facts["throttled_usec"] == 500
    sources["/proc/thread-self/schedstat"] = "bad"
    sources[active_activity._THROTTLE_STAT] = "usage_usec 10\n"
    facts = active_activity._sched_facts()
    assert "run_delay_ns" not in facts and "throttled_usec" not in facts


def test_counter_units_and_resets(clock):
    turn = TurnExecution(1)
    clock.sched = {"nvcsw": 7, "nivcsw": 3, "majflt": 4, "inblock": 20,
                   "run_delay_ns": 1000000000, "throttled_usec": 1000000}
    turn.advance("context_building")
    clock.wall, clock.cpu = 12., 3.
    clock.sched = {"nvcsw": 9, "nivcsw": 1, "majflt": 4, "inblock": 23,
                   "run_delay_ns": 1250000000, "throttled_usec": 1500000}
    turn.advance("context_built")
    assert turn.receipt()["phase_sched"]["context_building"] == {
        "nvcsw": 2, "majflt": 0, "inblock": 3,
        "run_delay_sec": .25, "cgroup_root_throttled_sec": .5}


def test_unavailable_lane_preserves_answer(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from supervisor import workers
    from tests.test_direct_turn_phase_stall import _Actor, _lane, _answers_at_once
    out = _lane(monkeypatch, tmp_path)
    monkeypatch.setattr(active_activity, "_sched_facts", lambda: {})
    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: _Actor(_answers_at_once))
    workers.handle_chat_direct(1, "inert unavailable diagnostics", task_metadata={"client_message_id": "m"})
    frames = []
    while not out.bus.empty():
        frames.append(out.bus.get_nowait())
    assert any(frame.get("text") == "the answer" for frame in frames)
    assert all("phase_sched" not in frame.get("direct_turn", {}) for frame in frames)
