"""Explicit capture is independent of which invocation produced the current bytes."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import zipfile

import pytest

from ouroboros.artifacts import collect_task_artifact_records, registered_task_artifact
from tests.test_declared_output_early_answer import project, local_project  # noqa: F401
from tests.test_docker_declared_outputs import shell_docker  # noqa: F401


def _invoke(registry, tool, cwd, outputs, body="pass"):
    args = {"cwd": str(cwd), "outputs": outputs}
    if tool == "run_script":
        args.update(interpreter=sys.executable, script=body)
    else:
        args["cmd"] = [sys.executable, "-c", body]
    result = registry.execute_result(tool, args)
    assert result.code == "OK", result.text
    return result


def _records(registry):
    ctx = registry._ctx
    return collect_task_artifact_records(ctx.drive_root, ctx.task_id, require_registered=True)


def _directory_bytes(rows):
    found = {}
    for row in rows:
        if row["name"].endswith(".zip"):
            with zipfile.ZipFile(row["path"]) as archive:
                found[pathlib.Path(row["path"])] = archive.read("member.txt")
    return found


@pytest.mark.parametrize("backend", ["native", "local", "docker-simulated"])
@pytest.mark.parametrize("tool", ["run_command", "run_script"])
@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_no_write_capture_refreshes_a_previously_published_output(request, backend, tool, directory):
    registry, active, *_ = request.getfixturevalue({"native": "project", "local": "local_project", "docker-simulated": "shell_docker"}[backend])
    output = "bundle" if directory else "report.txt"
    relative = "bundle/member.txt" if directory else output
    setup = "from pathlib import Path; " + ("Path('bundle').mkdir(); " if directory else "")
    first = _invoke(registry, tool, active, [output],
                    setup + f"Path({relative!r}).write_text('A\\n', encoding='utf-8')")
    assert first.meta["artifact_registered"]
    old = _records(registry)
    if directory:
        assert list(_directory_bytes(old).values()) == [b"A\n"]
    else:
        assert pathlib.Path(old[0]["path"]).read_bytes() == b"A\n"

    # A separate successful invocation changes the source but does not request capture.
    if backend == "docker-simulated":
        # This fixture simulates output-address pinning only; mutate its host bind
        # between captures. Native/local above exercise the public undeclared call.
        (active / relative).write_bytes(b"B\n")
    else:
        _invoke(registry, tool, active, [],
                f"from pathlib import Path; Path({relative!r}).write_text('B\\n', encoding='utf-8')")
    refreshed = _invoke(registry, tool, active, [output])
    assert refreshed.meta.get("artifact_registered"), refreshed.text
    rows = _records(registry)
    if directory:
        assert sorted(_directory_bytes(rows).values()) == [b"A\n", b"B\n"]
        for row in rows:
            if row["name"].endswith(".manifest.json"):
                ledger = json.loads(pathlib.Path(row["path"]).read_text(encoding="utf-8"))
                with zipfile.ZipFile(pathlib.Path(row["path"]).with_name(ledger["zip_name"])) as archive:
                    assert hashlib.sha256(archive.read("member.txt")).hexdigest() == ledger["files"][0]["sha256"]
    else:
        assert len(rows) == 1 and rows[0]["path"] == old[0]["path"]
        assert pathlib.Path(rows[0]["path"]).read_bytes() == b"B\n"
        versions = pathlib.Path(registry._ctx.drive_root) / "task_results/artifact_versions" / registry._ctx.task_id / output
        assert [p.read_bytes() for p in versions.iterdir()] == [b"A\n"]
        assert rows[0]["sha256"] == hashlib.sha256(b"B\n").hexdigest()
        assert registered_task_artifact(registry._ctx.drive_root, registry._ctx.task_id, output)["sha256"] == rows[0]["sha256"]

    # Repeating the declaration leaves one canonical identity, no false previous version.
    _invoke(registry, tool, active, [output])
    assert _records(registry) == rows
    if not directory:
        assert [p.read_bytes() for p in versions.iterdir()] == [b"A\n"]


@pytest.mark.parametrize("tool", ["run_command", "run_script"])
@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_preexisting_declared_output_is_captured_without_a_prior_export(project, tool, directory):
    registry, active, _ = project
    output = active / ("bundle" if directory else "report.txt")
    if directory:
        output.mkdir()
        (output / "member.txt").write_bytes(b"existing")
    else:
        output.write_bytes(b"existing")
    result = _invoke(registry, tool, active, [output.name])
    assert result.meta["artifact_registered"], result.text
    rows = _records(registry)
    if directory:
        assert list(_directory_bytes(rows).values()) == [b"existing"]
    else:
        assert pathlib.Path(rows[0]["path"]).read_bytes() == b"existing"


@pytest.mark.serial
@pytest.mark.parametrize("backend", ["native", "local"])
def test_service_stop_captures_current_unchanged_files_and_directories(request, backend):
    registry, active, *_ = request.getfixturevalue({"native": "project", "local": "local_project", "docker-simulated": "shell_docker"}[backend])
    (active / "bundle").mkdir()
    (active / "bundle/member.txt").write_bytes(b"current-dir")
    (active / "report.txt").write_bytes(b"current-file")
    started = registry.execute_result("start_service", {
        "name": "existing-outputs", "cmd": [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(60)"],
        "cwd": str(active), "outputs": ["report.txt", "bundle"],
        "readiness": {"log_contains": "READY", "timeout_sec": 5},
    })
    try:
        assert started.status == "ok", started.text
    finally:
        stopped = registry.execute_result("stop_service", {"name": "existing-outputs"})
    payload = json.loads(stopped.text)
    assert not payload["artifact_output_failed"], stopped.text
    assert stopped.meta.get("artifact_registered"), stopped.text
    rows = _records(registry)
    assert next(pathlib.Path(r["path"]).read_bytes() for r in rows if r["name"] == "report.txt") == b"current-file"
    assert list(_directory_bytes(rows).values()) == [b"current-dir"]


def _trace(tool, args, result):
    from ouroboros.loop_tool_execution import _extract_result_metadata, _is_tool_execution_failure

    error = _is_tool_execution_failure(True, result.text, result, fn_name=tool)
    return {"tool": tool, "args": args, "result": result.text, "is_error": error,
            **_extract_result_metadata(tool, result.text, error, result)}


def test_capture_does_not_clear_failed_production_but_a_corrected_producer_does(project):
    from ouroboros.outcomes import _classify_tool_errors, reviewable_effect_projection

    registry, active, _ = project
    producer = {"cwd": str(active), "outputs": ["partial.txt"], "cmd": [sys.executable, "-c",
        "from pathlib import Path; import sys; Path('partial.txt').write_text('partial', encoding='utf-8'); sys.exit(1)"]}
    failed = registry.execute_result("run_command", producer)
    assert failed.code == "SHELL_EXIT_ERROR", failed.text
    capture_args = {"cwd": str(active), "outputs": ["partial.txt"], "cmd": [sys.executable, "-c", "pass"]}
    capture = registry.execute_result("run_command", capture_args)
    assert capture.meta.get("artifact_registered") and capture.meta.get("artifact_produced_paths") == [], capture.text
    calls = [_trace("run_command", producer, failed), _trace("run_command", capture_args, capture)]
    assert calls[-1]["artifact_produced_paths"] == []
    classified = _classify_tool_errors({"tool_calls": calls})
    assert classified["recovered"] == [] and len(classified["cosmetic"]) == 1
    assert reviewable_effect_projection({"tool_calls": calls}), "capture remains a real reviewable effect"
    assert pathlib.Path(_records(registry)[0]["path"]).read_bytes() == b"partial"

    # A corrected, different command which produces the output keeps recovery credit.
    fixed_args = {**capture_args, "cmd": [sys.executable, "-c",
        "from pathlib import Path; Path('partial.txt').write_text('complete', encoding='utf-8')"]}
    fixed = registry.execute_result("run_command", fixed_args)
    assert fixed.meta.get("artifact_registered") and fixed.meta.get("artifact_produced_paths"), fixed.text
    fixed_trace = _trace("run_command", fixed_args, fixed)
    # Timeout/signal failures must not be hidden by capture-only, but may be repaired
    # by a successful producer of the same output. Original typed failures stay in trace.
    for status, extra in (("non_zero_exit", {}), ("timeout", {}), ("non_zero_exit", {"exit_code": -9})):
        original = {**calls[0], "status": status, **extra}
        without_producer = _classify_tool_errors({"tool_calls": [original, calls[1]]})
        assert without_producer["recovered"] == []
        assert _classify_tool_errors({"tool_calls": [original, calls[1], fixed_trace]})["recovered"]


def test_no_write_capture_recovers_a_real_missing_output_error(project):
    from ouroboros.outcomes import _classify_tool_errors

    registry, active, _ = project
    args = {"cwd": str(active), "outputs": ["later.txt"], "cmd": [sys.executable, "-c", "pass"]}
    failed = registry.execute_result("run_command", args)
    assert failed.code == "ARTIFACT_OUTPUT_ERROR", failed.text
    (active / "later.txt").write_bytes(b"now available")
    captured = registry.execute_result("run_command", args)
    classified = _classify_tool_errors({"tool_calls": [_trace("run_command", args, failed),
                                                     _trace("run_command", args, captured)]})
    assert captured.meta.get("artifact_produced_paths") == [] and classified["recovered"]
    assert not classified["unresolved"]


def test_same_invocation_rewrites_preserve_bounded_previous_versions(project):
    registry, active, _ = project
    for number in range(8):
        _invoke(registry, "run_command", active, ["report.txt"],
                f"from pathlib import Path; Path('report.txt').write_text({str(number)!r}, encoding='utf-8')")
    versions = pathlib.Path(registry._ctx.drive_root) / "task_results/artifact_versions" / registry._ctx.task_id / "report.txt"
    assert [p.read_bytes() for p in sorted(versions.iterdir())] == [b"2", b"3", b"4", b"5", b"6"]
    _invoke(registry, "run_command", active, ["report.txt"])
    assert [p.read_bytes() for p in sorted(versions.iterdir())] == [b"2", b"3", b"4", b"5", b"6"]


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_missing_canonical_capture_is_recreated(project, directory):
    registry, active, _ = project
    output = "bundle" if directory else "report.txt"
    relative = "bundle/member.txt" if directory else output
    _invoke(registry, "run_command", active, [output], "from pathlib import Path; " +
            ("Path('bundle').mkdir(); " if directory else "") + f"Path({relative!r}).write_bytes(b'current')")
    rows = _records(registry)
    for row in rows:
        pathlib.Path(row["path"]).unlink()
    result = _invoke(registry, "run_command", active, [output])
    assert result.meta.get("artifact_produced_paths") == []
    assert _records(registry) == rows


def test_directory_rewrite_with_restored_metadata_still_captures_current_content(project):
    registry, active, _ = project
    _invoke(registry, "run_command", active, ["bundle"],
            "from pathlib import Path; Path('bundle').mkdir(); Path('bundle/member.txt').write_bytes(b'A')")
    result = _invoke(registry, "run_command", active, ["bundle"],
        "from pathlib import Path; import os; p=Path('bundle/member.txt'); s=p.stat(); p.write_bytes(b'B'); os.utime(p, ns=(s.st_atime_ns,s.st_mtime_ns))")
    assert sorted(_directory_bytes(_records(registry)).values()) == [b"A", b"B"]
    assert result.meta.get("artifact_produced_paths") == [], "metadata baseline is conservative provenance, never freshness authority"


def test_changed_sibling_output_does_not_recover_an_unchanged_failed_target(project):
    from ouroboros.outcomes import _classify_tool_errors

    registry, active, _ = project
    (active / "partial.txt").write_bytes(b"incomplete")
    args = {"cwd": str(active), "outputs": ["partial.txt", "other.txt"], "cmd": [sys.executable, "-c",
        "from pathlib import Path; Path('other.txt').write_bytes(b'unrelated')"]}
    capture = registry.execute_result("run_command", args)
    produced = capture.meta["artifact_produced_paths"]
    assert produced == sorted(["other.txt", str(active / "other.txt")])
    assert pathlib.Path(next(r["path"] for r in _records(registry) if r["name"] == "partial.txt")).read_bytes() == b"incomplete"
    for status, extra in (("non_zero_exit", {}), ("timeout", {}), ("non_zero_exit", {"exit_code": -9})):
        failed = {"tool": "run_command", "is_error": True, "status": status,
                  "args": {"cwd": str(active), "outputs": ["partial.txt"], "cmd": ["failed-producer"]}, **extra}
        classified = _classify_tool_errors({"tool_calls": [failed, _trace("run_command", args, capture)]})
        assert not classified["recovered"]
        assert classified["unresolved"] or classified["cosmetic"]


def test_unavailable_baseline_captures_without_inventing_production(project, monkeypatch):
    from ouroboros.tools import shell_outputs

    registry, active, _ = project
    monkeypatch.setattr(shell_outputs, "_snapshot_declared_outputs", lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable baseline")))
    result = _invoke(registry, "run_command", active, ["new.txt"],
                     "from pathlib import Path; Path('new.txt').write_bytes(b'current')")
    assert result.meta["artifact_produced_paths"] == []
    assert pathlib.Path(_records(registry)[0]["path"]).read_bytes() == b"current"


@pytest.mark.parametrize("status", ["artifact_output_error", "artifact_output_undeclared"])
def test_unrelated_existing_capture_does_not_clear_capture_error(project, status):
    from ouroboros.outcomes import _classify_tool_errors

    registry, active, _ = project
    (active / "old.txt").write_bytes(b"old")
    captured = _invoke(registry, "run_command", active, ["old.txt"])
    failure = {"tool": "run_command", "is_error": True, "status": status,
               "args": {"cwd": str(active), "outputs": ["missing.txt"], "cmd": ["missing-producer"]}}
    calls = [failure, _trace("run_command", {"cwd": str(active), "outputs": ["old.txt"], "cmd": ["capture"]}, captured)]
    assert not _classify_tool_errors({"tool_calls": calls})["recovered"]


def test_same_relative_spelling_in_another_cwd_does_not_recover(project):
    from ouroboros.outcomes import _classify_tool_errors

    registry, active, _ = project
    a, b = active / "a", active / "b"
    a.mkdir(); b.mkdir()
    produced = _invoke(registry, "run_command", b, ["report.txt"],
                       "from pathlib import Path; Path('report.txt').write_bytes(b'unrelated')")
    failure = {"tool": "run_command", "is_error": True, "status": "timeout",
               "args": {"cwd": str(a), "outputs": ["report.txt"], "cmd": ["failed-producer"]}}
    later = _trace("run_command", {"cwd": str(b), "outputs": ["report.txt"], "cmd": ["other-producer"]}, produced)
    assert not _classify_tool_errors({"tool_calls": [failure, later]})["recovered"]
    failure["args"]["cwd"] = str(b)
    assert _classify_tool_errors({"tool_calls": [failure, later]})["recovered"]


@pytest.mark.serial
@pytest.mark.parametrize("tool", ["run_command", "run_script", "stop_service"])
def test_many_declared_outputs_capture_without_metadata_overflow(project, tool):
    registry, active, _ = project
    outputs = [f"{index:03}-{'x' * 90}.txt" for index in range(100)]
    body = f"from pathlib import Path; [Path(p).write_bytes(b'current') for p in {outputs!r}]"
    if tool == "stop_service":
        started = registry.execute_result("start_service", {
            "name": "many-outputs", "cmd": [sys.executable, "-c", body + "; import time; print('READY',flush=True); time.sleep(60)"],
            "cwd": str(active), "outputs": outputs, "readiness": {"log_contains": "READY", "timeout_sec": 5},
        })
        try:
            assert started.code == "OK", started.text
        finally:
            result = registry.execute_result("stop_service", {"name": "many-outputs"})
        assert result.code == "OK", result.text
        assert json.loads(result.text)["artifact_output_failed"] is False
    else:
        result = _invoke(registry, tool, active, outputs, body)
    assert len(_records(registry)) == 100
    assert result.meta.get("artifact_produced_paths") == []
    assert "production recovery" in result.text


def test_unknown_default_cwd_is_not_a_shared_service_output_context():
    from ouroboros.outcomes import _classify_tool_errors

    failed = {"tool": "run_command", "is_error": True, "status": "timeout",
              "args": {"outputs": ["report.txt"], "cmd": ["failed"]}}
    later = {"tool": "stop_service", "is_error": False, "status": "ok", "artifact_registered": True,
             "args": {"name": "builder"}, "artifact_produced_paths": ["report.txt", "/other/report.txt"]}
    assert not _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]


def test_recovery_keeps_absolute_target_and_legacy_positive_controls():
    from ouroboros.outcomes import _classify_tool_errors

    failed = {"tool": "run_command", "is_error": True, "status": "timeout",
              "args": {"cwd": "/a", "outputs": ["report.txt"], "cmd": ["failed"]}}
    later = {"tool": "stop_service", "is_error": False, "status": "ok", "artifact_registered": True,
             "args": {"name": "builder"}, "artifact_produced_paths": ["/a/report.txt"]}
    assert _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]
    later["artifact_produced_paths"] = ["/b/report.txt", "report.txt"]
    assert not _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]
    # Old traces had no produced-path field; their historical interpretation stays.
    del later["artifact_produced_paths"]
    later["args"] = {"cwd": "/a", "outputs": ["legacy.txt"]}
    assert _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]


@pytest.mark.parametrize("status", ["artifact_output_error", "timeout"])
def test_default_cwd_declared_output_recovery_remains_supported(status):
    from ouroboros.outcomes import _classify_tool_errors

    failed = {"tool": "run_command", "is_error": True, "status": status,
              "args": {"outputs": ["report.txt"], "cmd": ["failed"]}}
    later = {"tool": "run_script", "is_error": False, "status": "ok", "artifact_registered": True,
             "args": {"outputs": ["report.txt"], "script": "corrected producer"},
             "artifact_produced_paths": ["report.txt", "/actual/report.txt"]}
    assert _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]
    later["args"]["outputs"] = ["other.txt"]
    later["artifact_produced_paths"] = ["other.txt", "/actual/other.txt"]
    assert not _classify_tool_errors({"tool_calls": [failed, later]})["recovered"]
