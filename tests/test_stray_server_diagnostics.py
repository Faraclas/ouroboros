"""Report-only server census: scope words and verified serving-tree exclusion."""
from types import SimpleNamespace
import logging
import os
import pathlib
import sys

import pytest

from ouroboros import agent_startup_checks as checks, context_health, platform_layer as pl
from ouroboros import server_process


@pytest.fixture
def census(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "IS_WINDOWS", False)
    monkeypatch.setattr("ouroboros.config.REPO_DIR", pathlib.Path("/opt/ouroboros"))
    commands = {1006841: "/usr/bin/python3 -c multiprocessing.forkserver /opt/ouroboros/server.py",
                700: "/usr/bin/python3 /opt/ouroboros/server.py",
                800: "/usr/bin/python3 /other/ouroboros/server.py"}
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        assert argv[:3] == ["pgrep", "-U", str(os.getuid())]
        return SimpleNamespace(stdout="1006841\n700\n800\n", returncode=0)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(pl, "process_command", lambda pid: commands.get(pid, ""))
    monkeypatch.setattr(pl, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(pl, "_process_children", lambda: {46: [1006695], 1006695: [1006841]})
    binding = {"pid": 46, "fingerprint": {"source": "process_start_time", "start_time": "birth", "cmd_sha256": "cmd"}}
    monkeypatch.setattr(server_process, "read_service_bindings", lambda root: {"main": binding})
    monkeypatch.setattr(server_process, "service_binding_is_live", lambda record: record == binding)
    # Any signalling is a regression, even if a future probe starts calling it.
    def no_signal(*args, **kwargs):
        pytest.fail("a diagnostic must not signal processes")
    monkeypatch.setattr(pl, "force_kill_pid", no_signal)
    monkeypatch.setattr(pl, "kill_pid_tree", no_signal)
    return SimpleNamespace(env=SimpleNamespace(drive_root=tmp_path), commands=commands, binding=binding, seen=seen)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
def test_verified_serving_descendant_in_other_group_is_not_stray(census):
    result, issues = checks.check_stray_server_processes(census.env)
    assert issues == 1
    assert {p["pid"]: p["scope"] for p in result["processes"]} == {700: "same_install", 800: "foreign"}
    assert len(census.seen) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
@pytest.mark.parametrize("failure", ["missing", "unreadable", "stale", "changed", "ancestry_none", "ancestry_error"])
def test_uncertain_serving_tree_does_not_hide_candidates(census, monkeypatch, failure):
    if failure == "missing":
        monkeypatch.setattr(server_process, "read_service_bindings", lambda root: {})
    elif failure == "unreadable":
        def unreadable(root):
            raise PermissionError("binding unreadable")
        monkeypatch.setattr(server_process, "read_service_bindings", unreadable)
    elif failure in {"stale", "changed"}:
        calls = []
        def live(binding):
            calls.append(binding)
            return failure == "changed" and len(calls) == 1
        monkeypatch.setattr(server_process, "service_binding_is_live", live)
    elif failure == "ancestry_none":
        monkeypatch.setattr(pl, "_process_children", lambda: None)
    else:
        def broken():
            raise OSError("ps failed")
        monkeypatch.setattr(pl, "_process_children", broken)
    result, issues = checks.check_stray_server_processes(census.env)
    assert issues == 1
    assert {p["pid"] for p in result["processes"]} == {1006841, 700, 800}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
def test_single_tree_snapshot_and_binding_checks_not_per_candidate(census, monkeypatch):
    calls = []
    def snapshot():
        calls.append("snapshot")
        return {46: [1006695], 1006695: [1006841]}
    monkeypatch.setattr(pl, "_process_children", snapshot)
    checks.check_stray_server_processes(census.env)
    assert calls == ["snapshot"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
def test_verified_root_itself_and_descendant_are_excluded(census, monkeypatch):
    # A worker's census need not already know the main PID via its own parent.
    census.commands[46] = "/usr/bin/python3 /opt/ouroboros/server.py"
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(stdout="46\n1006841\n700\n", returncode=0))
    result, _ = checks.check_stray_server_processes(census.env)
    assert [p["pid"] for p in result["processes"]] == [700]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
def test_unavailable_scope_stays_unknown(census, monkeypatch):
    from ouroboros import launcher_server_reaper
    def unavailable(root):
        raise OSError("classification unavailable")
    monkeypatch.setattr(launcher_server_reaper, "install_server_path_forms", unavailable)
    result, _ = checks.check_stray_server_processes(census.env)
    assert {p["scope"] for p in result["processes"]} == {"unknown"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX census")
def test_scope_aware_startup_log(census, monkeypatch, caplog):
    monkeypatch.setattr(server_process, "read_service_bindings", lambda root: {})
    with caplog.at_level(logging.WARNING, logger=checks.__name__):
        result, _ = checks.check_stray_server_processes(census.env)
    assert any(p["scope"] == "same_install" for p in result["processes"])
    assert "outside this install" not in caplog.text
    assert "same_install" in caplog.text and "foreign" in caplog.text


@pytest.mark.parametrize("scopes,expected", [
    (["same_install"], ["same-install: 1", "foreign: 0", "unknown: 0"]),
    (["foreign"], ["same-install: 0", "foreign: 1", "unknown: 0"]),
    (["unknown"], ["same-install: 0", "foreign: 0", "unknown: 1"]),
    (["same_install", "foreign", None, "future"], ["same-install: 1", "foreign: 1", "unknown: 2"]),
])
def test_health_words_keep_scope_and_unknown(scopes, expected, tmp_path, monkeypatch):
    rows = [{"pid": 700 + i, **({"scope": scope} if scope else {})} for i, scope in enumerate(scopes)]
    monkeypatch.setattr(checks, "check_stray_server_processes", lambda env: ({"status": "stray_processes", "processes": rows}, 1))
    monkeypatch.setitem(context_health._STRAY_PROBE_CACHE, "ts", 0.0)
    note = context_health._stray_server_note(SimpleNamespace(drive_root=tmp_path))
    assert all(part in note for part in expected)
    assert "outside this install" not in note
    assert "Another install may be mutating" not in note
    assert "exclusive custody" in note


def test_health_pid_preview_discloses_omitted_count(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "check_stray_server_processes", lambda env: ({"status": "stray_processes", "processes": [{"pid": i, "scope": "unknown"} for i in range(7)]}, 1))
    monkeypatch.setitem(context_health._STRAY_PROBE_CACHE, "ts", 0.0)
    note = context_health._stray_server_note(SimpleNamespace(drive_root=tmp_path))
    assert "+2 more" in note
    assert "unknown: 7" in note
