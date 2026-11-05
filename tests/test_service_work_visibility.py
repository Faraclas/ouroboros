"""Wait source and exact-start service facts reach the ordinary work projection."""
import json
import queue
import sys
import time
from types import SimpleNamespace

import pytest

from ouroboros.gateway import work_projection as wp, work_status as ws
from ouroboros import service_observations as so
from ouroboros.tools import services
from tests.test_work_status import row, stored
from tests.test_services_tool_v2 import _force_advanced_runtime


def wait(attempt=1, **sleep):
    return {"state": "waiting", "reason": "sleep", "task_attempt": attempt,
            "parked_at": "2026-11-04T20:00:00Z", "sleep": {"mode": "warm", **sleep}}


def observation(state="running", rc=None, **extra):
    return {"task_id": "t", "task_attempt": 1, "services": [{
        "task_id": "t", "service_id": "t:tests", "name": "tests", "started_at": 10.0,
        "state": state, "returncode": rc, "observed_at": time.time(),
        "output_bytes": 42, "output_updated_at": 20.0, **extra}]}


@pytest.mark.parametrize("kind", ["managed_task", "direct_chat"])
@pytest.mark.parametrize("state,rc,shown", [("running", None, "live"), ("exited", 0, "ended"),
                                             ("exited", 7, "ended"), ("unknown", None, "unknown")])
def test_sleep_stays_waiting_while_real_service_fact_moves_room(stored, tmp_path, kind, state, rc, shown):
    snap = wp.works_snapshot(tmp_path, [row("t", kind=kind, sleep_wait=wait(),
                                         service_observations=observation(state, rc))], {})
    [work] = snap["works"]
    assert work["activity"] == "waiting" and work["because"][0]["since"] == "2026-11-04T20:00:00Z"
    [tail] = work["tails"]
    assert (tail["state"], tail["returncode"]) == (shown, rc)
    assert work["live"] is (state == "running")
    assert snap["works_complete"]  # unknown tail does not make every work unconfirmed
    assert snap["rooms"]["42"]["mark"] == ("moving" if state == "running" else "ring" if state == "unknown" else "still")
    assert "outcome" not in work


def test_per_service_time_and_exit_never_decays():
    sources = observation()["services"]
    sources += [{**sources[0], "service_id": "t:old", "state": "exited", "returncode": 7, "observed_at": 1}]
    sources[0]["observed_at"] = 1
    tails = wp._typed_services(sources, 1000)
    assert [t["state"] for t in tails] == ["unknown", "ended"]
    assert tails[1]["returncode"] == 7
    assert tails[1]["started_at"] == "1970-01-01T00:00:10+00:00"


def test_warm_cold_direct_selectors_are_typed_not_dependencies():
    selected = wait(senders=["A"], tasks=["A"], runs=["R"], any_mail=False,
                    wake_at="2026-11-05T10:00:00Z", services=[{"service_id": "t:s", "name": "s",
                                                             "started_at": 10, "pid": 999}])
    entry = ws.classify_member(row("t", sleep_wait=selected))["reasons"][0]
    assert entry["wake"]["senders"] == entry["wake"]["tasks"] == ["A"]
    assert entry["wake"]["runs"] == ["R"] and not entry["wake"]["any_mail"]
    assert entry["wake"]["at"] == "2026-11-05T10:00:00Z"
    assert "pid" not in json.dumps(entry)
    cold = {"sleep": {"mode": "cold", "tasks": ["B"]}, "reason": "sleep",
            "paused_at": "2026-11-04T20:01:00Z", "checkpoint": {"task_attempt": 1}}
    member = ws.classify_member(row("t", "budget_paused", pause_cause="sleep"), {"_budget_pause": cold})
    assert member["activity"] == "waiting" and member["reasons"][0]["wake"]["tasks"] == ["B"]
    assert member["reasons"][0]["since"] == "2026-11-04T20:01:00Z"
    cold["checkpoint"]["task_attempt"] = 2
    stale = ws.classify_member(row("t", "budget_paused", pause_cause="sleep"), {"_budget_pause": cold})
    assert "wake" not in stale["reasons"][0]
    assert ws.classify_member(row("t", sleep_wait=wait(attempt=2)))["reasons"] == []
    assert ws.classify_member(row("t", focus={"text": "waiting for X"}))["reasons"] == []


def test_output_stat_failure_does_not_erase_process(monkeypatch, tmp_path):
    record = SimpleNamespace(task_id="t", service_id="t:s", name="s", started_at=10, log_path=tmp_path / "absent")
    facts = {"started_at": 10, "state": "running", "returncode": None, "pid": 123}
    result = so.observe(record, facts)
    assert result["state"] == "running" and result["output_bytes"] is None
    assert not {"pid", "log_path", "cmd", "cwd"}.intersection(result)


def test_service_only_author_never_revived_or_given_controls(stored, tmp_path, monkeypatch):
    stored["done"] = {"task_id": "done", "status": "completed", "chat_id": 42, "title": "Finished"}
    record = {**observation()["services"][0], "task_id": "done", "service_id": "done:s"}
    monkeypatch.setattr(so, "discover", lambda root, **kwargs: ([record], []))
    snap = wp.works_snapshot(tmp_path, [], {})
    [work] = snap["works"]
    assert work["activity"] == "working" and work["live"] and not work["episode"]["live"]
    assert work["controls"] == [] and work["members"]["ids"] == []
    record["state"] = "unknown"
    snap = wp.works_snapshot(tmp_path, [], {})
    assert snap["works_complete"] and snap["works"][0]["activity"] == "unknown"
    record["state"] = "exited"
    assert wp.works_snapshot(tmp_path, [], {})["works"] == []


def test_live_author_deduplicates_ownership_against_heartbeat(stored, tmp_path, monkeypatch):
    record = observation()["services"][0]
    monkeypatch.setattr(so, "discover", lambda root, **kwargs: ([record], []))
    snap = wp.works_snapshot(tmp_path, [row("t", service_observations=observation())], {})
    assert len(snap["works"][0]["tails"]) == 1


def test_real_service_runs_exits_and_stop_preserves_last_start(stored, tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    _force_advanced_runtime(monkeypatch)
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "data")
    registry._ctx.task_id = "t"
    release = repo / "release"
    cmd = [sys.executable, "-c", "import pathlib,time; print('READY',flush=True); "
           f"p=pathlib.Path({str(release)!r}); "
           "exec('while not p.exists(): time.sleep(0.01)'); raise SystemExit(7)"]
    try:
        start = json.loads(registry.execute("start_service", {"name": "tests", "cmd": cmd,
                           "readiness": {"log_contains": "READY", "timeout_sec": 5}}))
        assert start["state"] == "running"
        obs = so.collect("t", 1)
        assert len(obs["services"]) == 1 and obs["services"][0]["output_bytes"] >= 6
        snap = wp.works_snapshot(tmp_path, [row("t", sleep_wait=wait(), service_observations=obs)], {})
        assert snap["works"][0]["live"]
        release.touch()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            obs = so.collect("t", 1)
            if obs["services"][0]["state"] == "exited":
                break
            time.sleep(.02)
        assert obs["services"][0]["returncode"] == 7
    finally:
        registry.execute("stop_service", {"name": "tests"})


def test_heartbeat_carries_exact_attempt(monkeypatch):
    from ouroboros.agent import OuroborosAgent
    agent = object.__new__(OuroborosAgent)
    agent.tools = SimpleNamespace(_ctx=SimpleNamespace(task_attempt=3))
    agent._event_queue = queue.Queue()
    agent._subagent_progress_meta = lambda phase: {}
    monkeypatch.setattr(so, "collect", lambda tid, attempt: {
        "task_id": tid, "task_attempt": attempt, "observed_at": 100, "services": []})
    agent._emit_task_heartbeat("t", "running")
    message = agent._event_queue.get_nowait()
    assert message["service_observations"]["task_attempt"] == 3
    assert agent._service_observations == message["service_observations"]


def test_heartbeat_never_probes_docker_and_keeps_real_probe_time(monkeypatch):
    from ouroboros import workspace_executor as ex
    record = SimpleNamespace(task_id="t", service_id="t:docker", name="docker", started_at=10,
        executor=SimpleNamespace(kind="docker_exec"), execution_observation={
            "started_at": 10, "state": "running", "returncode": None, "observed_at": 11})
    monkeypatch.setattr(ex, "_services_snapshot", lambda: [record])
    monkeypatch.setattr(services, "_SERVICES", {})
    monkeypatch.setattr(services, "service_execution_facts", lambda *a: pytest.fail("heartbeat backend probe"))
    [view] = so.collect("t", 1)["services"]
    assert view["observed_at"] == 11
    assert wp._typed_services([view], 1000)[0]["state"] == "unknown"


def test_dead_executor_leader_does_not_hide_surviving_group(monkeypatch, tmp_path):
    from ouroboros import process_custody as pc, workspace_executor as ex
    record = {"record_type": "service", "executor_type": "local", "service_custody": {"pid": 999}}
    monkeypatch.setattr(ex, "_load_process_record", lambda path: record)
    monkeypatch.setattr(ex, "_valid_process_record", lambda *a, **k: True)
    monkeypatch.setattr(pc, "_fingerprint_matches", lambda row: False)
    monkeypatch.setattr(pc, "_service_group_observation", lambda row, **kw: "live")
    monkeypatch.setattr("ouroboros.platform_layer.pid_provably_gone", lambda pid: True)
    monkeypatch.setattr(ex, "_docker_pid_state", lambda *a: pytest.fail("passive Docker probe"))
    entry = {"kind": "service", "host_pid": 999, "record_path": str(tmp_path / "record")}
    assert pc.owned_process_state(entry, probe_backend=False) == "live"
    record.update(executor_type="docker_exec")
    assert pc.owned_process_state(entry, probe_backend=False) == "unknown"


def test_direct_census_keeps_only_safe_wait_selectors():
    from supervisor.active_activity import DirectActivityEntry
    entry = DirectActivityEntry("t", 42, parked_wait=wait(services=[{
        "service_id": "t:s", "name": "s", "started_at": 10, "pid": 123, "pgid": 123,
        "backend_pid": "123"}], reviewers=[{"secret_custody": "private"}], sleep_id="private"))
    public = entry.to_dict()
    encoded = json.dumps(public)
    assert all(secret not in encoded for secret in ['"pid"', '"pgid"', 'backend_pid', 'secret_custody', 'sleep_id'])
    reason = ws.classify_member(public)["reasons"][0]
    assert reason["wake"]["services"][0]["started_at"] == "1970-01-01T00:00:10+00:00"


def test_service_only_binding_and_mixed_work_controls(stored, tmp_path, monkeypatch):
    stored["done"] = {"task_id": "done", "status": "completed", "chat_id": 1,
                       "metadata": {"continuation": {"work_id": "shared"}}}
    record = {**observation()["services"][0], "task_id": "done", "service_id": "done:s"}
    monkeypatch.setattr(so, "discover", lambda root, **kwargs: ([record], []))
    bindings = {"done": {"chat_id": 99, "project_id": "moved"}}
    [work] = wp.works_snapshot(tmp_path, [], {}, bindings=bindings)["works"]
    assert work["room"] == 99 and work["project_id"] == "moved"
    raw = {"current": {"continuation": {"work_id": "shared"}}}
    [work] = wp.works_snapshot(tmp_path, [row("current")], raw, bindings=bindings)["works"]
    assert any(t["kind"] == "service" for t in work["tails"])
    assert all(c["target"] != "done" for c in work["controls"])


def test_terminal_observation_saved_before_same_name_replacement(tmp_path, monkeypatch):
    from ouroboros.task_results import write_task_result, load_task_result
    root = tmp_path / "data"
    write_task_result(root, "t", "running", task_attempt=3)
    log_path = tmp_path / "service.log"
    log_path.write_text("old output")
    record = SimpleNamespace(task_id="t", service_id="t:s", name="s", started_at=10,
                             log_path=log_path, drive_root=root)
    so.retain_final_observation(record, {"started_at": 10, "state": "exited", "returncode": 7})
    record.started_at = 20
    so.retain_final_observation(record, {"started_at": 20, "state": "exited", "returncode": 0})
    result = load_task_result(root, "t", strict=True)
    saved = result["service_observations"]
    assert saved["task_attempt"] == 3
    assert [(r["started_at"], r["returncode"]) for r in saved["services"]] == [(10, 7), (20, 0)]
    assert result["status"] == "running"


def test_direct_public_pin_and_epoch_observation_are_one_exact_start(stored, tmp_path):
    from supervisor.active_activity import DirectActivityEntry
    entry = DirectActivityEntry("t", 42, parked_wait=wait(services=[{
        "service_id": "t:tests", "name": "tests", "started_at": 10.0, "pid": 999}]),
        actor=SimpleNamespace(_service_observations=observation()))
    [work] = wp.works_snapshot(tmp_path, [entry.to_dict()], {})["works"]
    assert len(work["tails"]) == 1
    assert work["tails"][0]["state"] == "live"
    assert work["tails"][0]["started_at"] == "1970-01-01T00:00:10+00:00"


def test_discovery_skips_live_authors_before_any_process_probe(monkeypatch, tmp_path):
    records = [{"owner_task": f"t{i}", "kind": "supervised", "ledger_entry": {
        "purpose": "service:s", "service_info": {"service_id": f"t{i}:s"}}} for i in range(1000)]
    monkeypatch.setattr("ouroboros.owned_shutdown.owned_records", lambda *a, **k: records)
    monkeypatch.setattr("ouroboros.process_custody.owned_process_state", lambda *a, **k: pytest.fail("live author probe"))
    assert so.discover(tmp_path, skip_tasks={f"t{i}" for i in range(1000)}) == ([], [])


def test_zombie_is_ended_not_perpetual_unconfirmed_service(monkeypatch):
    from ouroboros import process_custody as pc
    monkeypatch.setattr(pc, "_fingerprint_matches", lambda row: False)
    monkeypatch.setattr(pc, "_service_group_observation", lambda row, **kw: None)
    monkeypatch.setattr("ouroboros.process_containment.pid_is_zombie", lambda pid: True)
    monkeypatch.setattr("ouroboros.platform_layer.pid_provably_gone", lambda pid: False)
    assert pc.owned_process_state({"host_pid": 999, "ledger_entry": {"pid": 999}}, probe_backend=False) == "ended"


def test_service_only_root_does_not_replace_open_run_episode(stored, tmp_path, monkeypatch):
    stored["r"] = {"task_id": "r", "chat_id": 42, "status": "completed",
                   "metadata": {"continuation": {"work_id": "shared"}}}
    stored["z"] = {"task_id": "z", "chat_id": 99, "status": "failed",
                   "metadata": {"continuation": {"work_id": "shared"}}}
    record = {**observation()["services"][0], "task_id": "z", "service_id": "z:s"}
    monkeypatch.setattr(so, "discover", lambda root, **kwargs: ([record], []))
    monkeypatch.setattr(wp, "_open_runs", lambda root: {"r": [SimpleNamespace(run_id="run", task_id="r", source="task")]})
    [work] = wp.works_snapshot(tmp_path, [], {})["works"]
    assert work["episode"]["task_id"] == "r" and work["room"] == 42
    assert all(c["target"] != "z" for c in work["controls"])


@pytest.mark.parametrize("state,expected", [(b"Z", "ended"), (b"S", "live"), (None, "unknown")])
def test_passive_group_state_is_tri_state_without_ps(monkeypatch, state, expected):
    import builtins
    import io
    from ouroboros import process_containment as pc
    real_open, real_listdir = builtins.open, pc.os.listdir
    monkeypatch.setattr(pc._pl, "process_group_is_alive", lambda pgid: True)
    monkeypatch.setattr(pc.os, "listdir", lambda path: ["999"] if path == "/proc" else real_listdir(path))
    monkeypatch.setattr(pc.os.path, "isdir", lambda path: path == "/proc")
    def opened(path, *args, **kwargs):
        if str(path) == "/proc/999/stat":
            if state is None:
                raise PermissionError("unreadable group")
            return io.BytesIO(b"999 (service) " + state + b" 1 77 0")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: pytest.fail("passive ps"))
    assert pc.process_group_state(77, allow_subprocess=False) == expected


def test_unknown_group_does_not_become_confirmed_live(monkeypatch):
    from ouroboros import process_custody as pc
    monkeypatch.setattr(pc, "_fingerprint_matches", lambda row: False)
    monkeypatch.setattr(pc, "_service_group_observation", lambda row, **kw: "unknown")
    assert pc.owned_process_state({"host_pid": 999, "ledger_entry": {"pid": 999}}, probe_backend=False) == "unknown"


@pytest.mark.parametrize("zombie,refuted,group,expected", [
    (True, False, "ended", "ended"), (False, True, None, "ended"),
    (False, False, "unknown", "unknown"),
])
def test_executor_strong_custody_never_falls_back_to_command_only(monkeypatch, tmp_path, zombie, refuted, group, expected):
    from ouroboros import process_custody as pc, workspace_executor as ex
    record = {"record_type": "service", "executor_type": "local", "service_custody": {"pid": 999}}
    monkeypatch.setattr(ex, "_load_process_record", lambda path: record)
    def valid(*a, **kwargs):
        assert kwargs.get("check_identity") is False, "weaker command-only identity fallback"
        return True
    monkeypatch.setattr(ex, "_valid_process_record", valid)
    monkeypatch.setattr(pc, "_fingerprint_matches", lambda row: False)
    monkeypatch.setattr(pc, "_service_group_observation", lambda row, **kw: group)
    monkeypatch.setattr(pc, "measured_identity_mismatch", lambda row: refuted)
    monkeypatch.setattr("ouroboros.process_containment.pid_is_zombie", lambda pid: zombie)
    monkeypatch.setattr("ouroboros.platform_layer.pid_provably_gone", lambda pid: False)
    entry = {"kind": "service", "host_pid": 999, "record_path": str(tmp_path / "record")}
    assert pc.owned_process_state(entry, probe_backend=False) == expected


def test_display_observation_failure_cannot_suppress_heartbeat(monkeypatch):
    from ouroboros.agent import OuroborosAgent
    from ouroboros import service_observations as observations
    events = []
    previous = {"observed_at": 12, "services": [{"service_id": "old-start"}]}
    actor = SimpleNamespace(tools=SimpleNamespace(_ctx=SimpleNamespace(task_attempt=7)),
        _event_queue=SimpleNamespace(put=events.append), _service_observations=previous,
        _subagent_progress_meta=lambda phase: {})
    def unavailable(*args):
        raise RuntimeError("registry observation unavailable")
    monkeypatch.setattr(observations, "collect", unavailable)
    OuroborosAgent._emit_task_heartbeat(actor, "t", "running")
    assert len(events) == 1 and events[0]["type"] == "task_heartbeat"
    assert events[0]["task_attempt"] == 7
    assert events[0]["service_observations"] is previous
    assert actor._service_observations["observed_at"] == 12


def test_forked_native_stop_reaches_canonical_projection_before_copyback(monkeypatch, tmp_path):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import write_task_result, load_task_result
    _force_advanced_runtime(monkeypatch)
    repo, child, canon = (tmp_path / name for name in ("repo", "child", "canonical"))
    repo.mkdir()
    for root in (child, canon):
        write_task_result(root, "fork-author", "running", task_attempt=2, chat_id=42)
    registry = ToolRegistry(repo_dir=repo, drive_root=child)
    registry._ctx.task_id = "fork-author"
    registry._ctx.task_attempt = 2
    registry._ctx.budget_drive_root = str(canon)
    registry._ctx.task_metadata = {"budget_drive_root": str(canon)}
    # Isolated storage/real-process integration, not a live Safety/admission check.
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    try:
        raw = registry.execute("start_service", {"name": "canonical-fact",
            "cmd": [sys.executable, "-c", "import time; print('READY',flush=True); time.sleep(20)"],
            "readiness": {"log_contains": "READY", "timeout_sec": 5}})
        assert raw.startswith("{"), raw
        start = json.loads(raw)
        assert start["state"] == "running", start
        record = services._SERVICES[start["service_id"]]
        assert record.drive_root == canon
        assert record.log_path.is_relative_to(child)
        stopped = json.loads(registry.execute("stop_service", {"name": "canonical-fact"}))
        assert stopped["state"] != "running", stopped
        kept = load_task_result(canon, "fork-author")["service_observations"]
        assert kept["task_attempt"] == 2 and kept["services"][0]["state"] == "exited"
        assert "service_observations" not in load_task_result(child, "fork-author")
        view = wp.works_snapshot(canon, [row("fork-author", task_attempt=2,
            service_observations={"task_id": "fork-author", "task_attempt": 2, "services": []})], {})
        [tail] = view["works"][0]["tails"]
        assert tail["state"] == "ended" and tail["started_at"]
    finally:
        registry.execute("stop_service", {"name": "canonical-fact"})
