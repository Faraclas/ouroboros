"""Filesystem names round-trip through task state, hashes and HTTP JSON."""
import hashlib
import json
import os

import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from ouroboros import artifacts
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.task_status import load_effective_task_result
from ouroboros.tools.join_ledger import _child_result_sha256
from ouroboros.tools.task_result_views import authority_selection, render_result_view
from ouroboros.utils import machine_json


def _store(root):
    store = artifacts.task_artifact_dir_path(root, "names", create=True)
    name = os.fsdecode(b"fixture_\xff.dat")
    if name == "fixture_ÿ.dat" or os.name == "nt":
        pytest.skip("POSIX surrogateescape filesystem names")
    (store / name).write_bytes(b"retained evidence")
    (store / "café.txt").write_bytes(b"valid unicode")
    return store, name


@pytest.mark.parametrize("measure", [False, True])
def test_artifact_listing_can_be_published_and_read_without_renaming(tmp_path, measure):
    store, name = _store(tmp_path)
    rows = artifacts.collect_task_artifact_records(tmp_path, "names", measure=measure)
    write_task_result(tmp_path, "names", "completed", result="the final answer", artifacts=rows)
    stored = load_task_result(tmp_path, "names")
    assert stored["status"] == "completed" and stored["result"] == "the final answer"
    assert next(row for row in stored["artifacts"] if row["name"] == name)["path"] == str(store / name)
    effective = load_effective_task_result(tmp_path, "names")
    assert json.loads(machine_json(effective))["result"] == "the final answer"
    assert len(_child_result_sha256(effective)) == 64
    assert (store / name).read_bytes() == b"retained evidence"


@pytest.mark.parametrize("view", ["answer", "summary", "evidence"])
def test_short_result_views_keep_surrogate_names_json_safe(tmp_path, view):
    store, name = _store(tmp_path)
    write_task_result(tmp_path, "names", "completed", result="done")
    data = load_effective_task_result(tmp_path, "names")
    raw = render_result_view(data, task_id="names", view=view, known_result_sha256=None,
                             child_result_sha256=_child_result_sha256(data), outcome_summary="",
                             capability_delta={}, effort={})
    raw.encode("utf-8")
    assert json.loads(raw)["status"] == "completed"
    selected = authority_selection(data, "names", ["status"], source_only=True,
                                   digest="", start=None, end=None)
    assert selected["status"] == "available"
    assert (store / name).exists()


def test_task_http_json_keeps_exact_filesystem_path(tmp_path, monkeypatch):
    from ouroboros.gateway import tasks
    store, name = _store(tmp_path)
    write_task_result(tmp_path, "names", "completed", result="done")
    app = Starlette()
    app.state.drive_root = tmp_path
    request = Request({"type": "http", "app": app, "path_params": {"task_id": "names"}})
    response = tasks._task_get_response(request)
    assert response.status_code == 200
    payload = json.loads(response.body)
    rows = payload["artifacts"]
    assert any(row["path"] == str(store / name) for row in rows)


def test_regular_unicode_json_and_child_hash_are_unchanged():
    data = {"status": "completed", "result": "café 蛇 🐍", "artifacts": []}
    expected = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    assert machine_json(data) == expected
    payload = {"status": "completed", "result": data["result"], "trace_summary": None,
               "artifact_status": "", "artifacts": []}
    original = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    assert _child_result_sha256(data) == hashlib.sha256(original.encode("utf-8")).hexdigest()


def test_json_representation_distinguishes_literal_escape_and_native_name(tmp_path):
    from ouroboros.utils import append_jsonl, json_dumps
    original = {"fixture_\udcff.dat": ["literal \\udcff", "蛇 🐍", "\"\\\n"]}
    text = json_dumps(original, ensure_ascii=False, indent=2)
    assert json.loads(text) == original
    assert os.fsencode(next(iter(json.loads(text)))) == b"fixture_\xff.dat"
    path = tmp_path / "rows.jsonl"
    assert append_jsonl(path, original, live=False)
    assert json.loads(path.read_bytes()) == original


def test_directory_zip_refuses_unrepresentable_member_without_losing_files(tmp_path):
    from ouroboros.gateway.task_archive import serve_directory_archive
    store, name = _store(tmp_path)
    nested = store / "evidence"
    nested.mkdir()
    (store / name).rename(nested / name)
    rows = artifacts.collect_task_artifact_records(tmp_path, "names")
    write_task_result(tmp_path, "names", "completed", result="done", artifacts=rows)
    response = serve_directory_archive(tmp_path, "names", "evidence.zip", "evidence", other_selectors=False)
    assert response.status_code == 503
    assert json.loads(response.body)["reason_code"] == "artifact_archive_unavailable"
    assert (nested / name).read_bytes() == b"retained evidence"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_real_terminal_pipeline_preserves_answer_and_native_artifacts(tmp_path, status):
    from types import SimpleNamespace
    from ouroboros import agent_task_pipeline as pipeline
    store, name = _store(tmp_path)
    task = {"id": "names", "type": "task", "_is_direct_chat": True,
            "text": "retain the evidence", "chat_id": 1}
    outcome = {"reason_code": "runtime_failure" if status == "failed" else "",
               "final_answer": "final preserved answer",
               "outcome_axes": {"execution": {"status": "infra_failed" if status == "failed" else "completed"}}}
    pipeline._store_task_result(SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path),
                               task, "final preserved answer", {"rounds": 1},
                               {"tool_calls": []}, loop_outcome=outcome)
    saved = load_task_result(tmp_path, "names")
    assert saved["status"] == status
    assert saved["result"] == "final preserved answer"
    assert any(row["name"] == name for row in saved["artifacts"])
    assert (store / name).read_bytes() == b"retained evidence"


@pytest.mark.parametrize("view", ["answer", "summary", "evidence", "full"])
def test_registered_task_result_views_and_known_hash_preserve_failed_history(tmp_path, view):
    from ouroboros.tools.registry import ToolRegistry
    store, name = _store(tmp_path)
    write_task_result(tmp_path, "names", "failed", result="historical failed attempt",
                      reason="task_done_lifecycle_fault")
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    for condition in [None, _child_result_sha256(load_effective_task_result(tmp_path, "names"))]:
        arguments = {"task_id": "names", "view": view}
        if condition:
            arguments["known_result_sha256"] = condition
        text = registry.execute("get_task_result", arguments)
        text.encode("utf-8")
        assert "failed" in text and "child_result_sha256" in text
        assert "UnicodeEncodeError" not in text
    text = registry.execute("get_task_result", {"task_id": "names", "include_authority": True})
    text.encode("utf-8")
    payload = json.loads(text)
    assert payload["authority"]["status"] == "failed"
    text = registry.execute("get_task_result", {"task_id": "names", "authority_fields": [], "source_only": True})
    text.encode("utf-8")
    assert json.loads(text)["status"] == "available"
    assert load_task_result(tmp_path, "names")["result"] == "historical failed attempt"
    assert (store / name).exists()


def test_real_asgi_detail_keeps_native_names_and_valid_download(tmp_path):
    from starlette.testclient import TestClient
    from starlette.routing import Route
    from ouroboros.gateway import tasks
    store, name = _store(tmp_path)
    write_task_result(tmp_path, "names", "failed", result="preserved failure")
    app = Starlette(routes=[Route("/api/tasks/{task_id}", tasks.api_task_get)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/api/tasks/names")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed" and data["result"] == "preserved failure"
    assert any(row["path"] == str(store / name) for row in data["artifacts"])
    assert any(row["name"] == "café.txt" for row in data["artifacts"])


def test_task_sse_snapshot_and_valid_unicode_formatting(tmp_path):
    from ouroboros.gateway.task_events import _sse
    _, name = _store(tmp_path)
    event = {"type": "snapshot", "result": {"artifacts": [{"name": name}]}}
    wire = _sse(event, event_id=1)
    wire.encode("utf-8")
    assert json.loads(wire.split("data: ")[1]) == event
    normal = {"text": "café 🐍"}
    assert _sse(normal, event_id=2) == f'id: 2\nevent: task_event\ndata: {json.dumps(normal, ensure_ascii=False)}\n\n'


def test_websocket_task_done_preserves_clients_and_native_names(tmp_path, monkeypatch):
    import asyncio
    from ouroboros.gateway import ws
    _, name = _store(tmp_path)
    sent = []
    class Client:
        async def send_text(self, text):
            sent.append(text.encode("utf-8"))
    client = Client()
    monkeypatch.setattr(ws, "_ws_clients", [client])
    event = {"type": "log", "data": {"artifact_bundle": {"artifacts": [{"name": name}]}}}
    asyncio.run(ws.broadcast_ws(event))
    assert ws._ws_clients == [client]
    assert json.loads(sent[0]) == event


def test_real_asgi_logs_return_retained_task_done(tmp_path):
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ouroboros.gateway.logs import api_logs_tail
    from ouroboros.utils import append_jsonl
    _, name = _store(tmp_path)
    write_task_result(tmp_path, "names", "failed", result="historical failure")
    append_jsonl(tmp_path / "logs/events.jsonl", {"type": "task_done", "task_id": "names",
                  "artifact_bundle": {"artifacts": [{"name": name}]}}, live=False)
    app = Starlette(routes=[Route("/api/logs/{name}", api_logs_tail)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/api/logs/events?task_id=names")
    assert response.status_code == 200
    assert response.json()["entries"][0]["artifact_bundle"]["artifacts"][0]["name"] == name


def test_completion_observations_keep_native_delivery_text(tmp_path):
    from ouroboros.task_finalization import build_completion_observations, completion_source_projection
    _, name = _store(tmp_path)
    task = {"id": "names"}
    trace = {"tool_calls": [{"tool": "send_file", "status": "ok", "result": "sent " + name}]}
    observations = build_completion_observations(tmp_path, task, trace)
    assert observations["source_status"] == "available"
    source = completion_source_projection(tmp_path, "names", {"completion_observations": observations})
    assert source["range_required"] is True
    full = completion_source_projection(tmp_path, "names", {"completion_observations": observations},
                                        0, source["complete_chars"])
    assert json.loads(full["text"])["delivery_results"][0]["result"] == "sent " + name
