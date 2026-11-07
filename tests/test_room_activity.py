"""Retained activity uses current canonical room membership, bounded indexed paging and no controls."""
import asyncio
import base64
import json
from types import SimpleNamespace
from typing import get_type_hints

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.activity_history import room_activity_page
from ouroboros.gateway.activity import api_room_activity
from ouroboros.projects_registry import create_project, bind_task_to_project
from ouroboros.task_store import root_ids_page
from tests._store_shared import seed_result, read_result


def seed(root, tid, chat=1, ts="2026-01-01T00:00:00Z", **extra):
    return seed_result(root, tid, {"task_id": tid, "status": "completed", "chat_id": chat,
                                    "ts": ts, "title": tid, **extra})


def test_main_is_not_every_unscoped_or_project_scoped_task(tmp_path):
    project = create_project(tmp_path, "p")
    seed(tmp_path, "main", project_id="p")  # scope alone is not delivery
    seed(tmp_path, "hidden", chat=0, project_id="p")
    seed(tmp_path, "project", chat=project["chat_id"])
    seed(tmp_path, "external-room", chat=987654321)
    seed(tmp_path, "a2a", chat=-76543)
    main = room_activity_page(tmp_path, 1)
    assert {r["task_id"] for r in main["tasks"]} == {"main", "external-room"}
    specific = room_activity_page(tmp_path, project["chat_id"])
    assert [r["task_id"] for r in specific["tasks"]] == ["project"]
    assert main["coverage"]["reached_start"] is True
    assert all("controls" not in r and "live" not in r for r in main["tasks"])


def test_conversion_moves_retained_roots_without_copy_or_leaked_main_rows(tmp_path):
    project = create_project(tmp_path, "p")
    seed(tmp_path, "converted")
    assert [r["task_id"] for r in room_activity_page(tmp_path, 1)["tasks"]] == ["converted"]
    bind_task_to_project(tmp_path, "converted", "p", origin={"absent": "system"})
    assert room_activity_page(tmp_path, 1)["tasks"] == []
    assert [r["task_id"] for r in room_activity_page(tmp_path, project["chat_id"])["tasks"]] == ["converted"]


def test_sparse_page_cursor_advances_over_scanned_not_only_matched_candidates(tmp_path):
    project = create_project(tmp_path, "sparse")
    seed(tmp_path, "old-project", chat=project["chat_id"], ts="2025-01-01T00:00:00Z")
    for i in range(7):
        seed(tmp_path, f"unrelated-{i}", ts=f"2026-01-01T00:00:0{i}Z")
    pages, cursor = [], ""
    for _ in range(6):
        page = room_activity_page(tmp_path, project["chat_id"], limit=2, cursor=cursor, scan_budget=3)
        pages.append(page)
        if not page["next_cursor"]:
            break
        cursor = page["next_cursor"]
    assert pages[0]["tasks"] == [] and pages[0]["next_cursor"]
    assert pages[0]["coverage"]["scanned"] == 3
    assert [r["task_id"] for p in pages for r in p["tasks"]] == ["old-project"]
    assert pages[-1]["coverage"]["reached_start"] is True
    assert len({p["next_cursor"] for p in pages if p["next_cursor"]}) == len(pages) - 1


def test_root_index_excludes_children_and_pages_same_date_and_null_dates(tmp_path):
    for tid, ts in [("b", None), ("a", None), ("d", "2026-01-01"), ("c", "2026-01-01")]:
        seed(tmp_path, tid, ts=ts)
    for i in range(12):
        seed(tmp_path, f"child-{i}", parent_task_id="d", root_task_id="d", delegation_role="subagent")
    seen, before = [], None
    for _ in range(5):
        page = root_ids_page(tmp_path, 1, before=before)
        if not page:
            break
        seen.append(page[0]["task_id"])
        before = [page[0]["ts"], page[0]["task_id"]]
    assert seen == ["d", "c", "b", "a"]


def test_page_preserves_exact_services_and_reads_never_write(tmp_path):
    seed(tmp_path, "author", service_observations={"services": [
        {"service_id": "author:test", "name": "first", "started_at": "2026-01-01T01:00:00Z",
         "state": "exited", "returncode": 7},
        {"service_id": "author:test", "name": "second", "started_at": "2026-01-01T02:00:00Z",
         "state": "exited", "returncode": -9},
        {"service_id": "old", "state": "unknown"}]})
    before = read_result(tmp_path, "author")
    first = room_activity_page(tmp_path, 1)
    second = room_activity_page(tmp_path, 1)
    assert first["tasks"] == second["tasks"]
    entries = first["tasks"][0]["entries"]
    assert len({e["key"] for e in entries}) == 3
    assert all(not e["error"] for e in entries)
    assert first["tasks"][0]["gaps"]
    assert read_result(tmp_path, "author") == before


def test_cursor_bound_to_room_and_conversion_revision(tmp_path):
    seed(tmp_path, "a")
    seed(tmp_path, "b")
    cursor = room_activity_page(tmp_path, 1, limit=1)["next_cursor"]
    with pytest.raises(ValueError):
        room_activity_page(tmp_path, 500, cursor=cursor)
    create_project(tmp_path, "new")
    bind_task_to_project(tmp_path, "a", "new", origin={"absent": "system"})
    with pytest.raises(ValueError, match="activity_view_changed"):
        room_activity_page(tmp_path, 1, cursor=cursor)


@pytest.mark.parametrize("cursor", ["!", "", "W10=", "bnVsbA==", "e30=", "eA=="])
def test_http_input_validation_is_not_a_500(tmp_path, cursor):
    app = Starlette(routes=[Route("/api/rooms/{chat_id}/activity", api_room_activity)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/api/rooms/1/activity", params={"cursor": cursor})
        assert response.status_code == (200 if cursor == "" else 400)
        assert client.get("/api/rooms/0/activity").status_code == 400
        assert client.get("/api/rooms/nope/activity").status_code == 400
        assert client.post("/api/rooms/1/activity").status_code == 405


def test_unreadable_lineage_does_not_skip_the_window(tmp_path, monkeypatch):
    seed(tmp_path, "one")
    monkeypatch.setattr("ouroboros.projects_registry.RoomResolver.warm", lambda *a: False)
    page = room_activity_page(tmp_path, 1)
    assert page["gaps"] and page["coverage"]["scanned"] == 0
    assert not page["coverage"]["reached_start"] and page["next_cursor"]
    monkeypatch.undo()
    retried = room_activity_page(tmp_path, 1, cursor=page["next_cursor"])
    assert [r["task_id"] for r in retried["tasks"]] == ["one"]


def test_activity_contracts_have_browser_twins():
    import pathlib
    from ouroboros.gateway import activity_contracts as contracts
    from tests.test_gateway_parity import _js_typedef_fields
    text = (pathlib.Path(__file__).resolve().parents[1] / "web/modules/activity_types.js").read_text(encoding="utf-8")
    for name in ("ActivityAssessment", "ActivityEntry", "ActivityTask", "ActivityCoverage", "ActivityPageResponse"):
        assert _js_typedef_fields(text, name) == set(get_type_hints(getattr(contracts, name)))


def test_unreadable_presence_leaf_does_not_trap_all_older_history(tmp_path, monkeypatch):
    from ouroboros.task_reads import TaskReads
    seed(tmp_path, "older", ts="2025-01-01T00:00:00Z")
    seed(tmp_path, "lost-provenance", task_contract={"capability_ceiling": {}})
    assert root_ids_page(tmp_path, 1)[0]["presence_authority_recorded"]
    load = TaskReads.load
    monkeypatch.setattr(TaskReads, "load", lambda self, root, tid:
                        None if tid == "lost-provenance" else load(self, root, tid))
    first = room_activity_page(tmp_path, 1, limit=1, scan_budget=1)
    assert not first["tasks"] and first["gaps"]
    assert first["coverage"]["scanned"] == 1
    second = room_activity_page(tmp_path, 1, limit=1, cursor=first["next_cursor"], scan_budget=1)
    assert [row["task_id"] for row in second["tasks"]] == ["older"]


def test_retry_successor_does_not_replace_physical_history_or_cross_rooms(tmp_path):
    project = create_project(tmp_path, "successor-room")
    seed(tmp_path, "original", status="failed", superseded_by="successor", title="Original failed occurrence")
    seed(tmp_path, "successor", chat=project["chat_id"], title="Successful successor")
    page = room_activity_page(tmp_path, 1)
    assert [r["task_id"] for r in page["tasks"]] == ["original"]
    assert page["tasks"][0]["entries"][0]["name"] == "Original failed occurrence"
    assert page["tasks"][0]["entries"][0]["error"] is True
    other = room_activity_page(tmp_path, project["chat_id"])
    assert [r["task_id"] for r in other["tasks"]] == ["successor"]


def test_physical_page_does_not_prefetch_successor_chain(tmp_path, monkeypatch):
    from ouroboros import task_store
    seed(tmp_path, "selected", status="failed", superseded_by="retry-0")
    for i in range(20):
        seed(tmp_path, f"retry-{i}", ts="2025-01-01T00:00:00Z",
             superseded_by=f"retry-{i + 1}" if i < 19 else "")
    fetched = []
    original = task_store.display_documents
    def observe(root, ids, *args, **kwargs):
        ids = list(ids)
        fetched.extend(ids)
        return original(root, ids, *args, **kwargs)
    monkeypatch.setattr(task_store, "display_documents", observe)
    page = room_activity_page(tmp_path, 1, limit=1, scan_budget=1)
    assert [r["task_id"] for r in page["tasks"]] == ["selected"]
    assert fetched == ["selected"], fetched


def test_retained_view_does_not_build_a_global_event_tail(tmp_path, monkeypatch):
    from ouroboros import task_status
    seed(tmp_path, 'retained', status='completed')
    def forbidden(*args, **kwargs):
        raise AssertionError('retained room history must not scan the global events tail')
    monkeypatch.setattr(task_status, '_EventsTailIndex', forbidden)
    page = room_activity_page(tmp_path, 1)
    assert [r['task_id'] for r in page['tasks']] == ['retained']
    assert not page['gaps']


def test_root_cursor_seeks_instead_of_walking_all_newer_roots(monkeypatch):
    import contextlib
    import sqlite3
    from ouroboros import store, task_store
    connection = sqlite3.connect(":memory:")
    connection.executescript("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, ts TEXT, project_id TEXT, "
                             "parent_task_id TEXT, delegation_role TEXT, presence_authority_recorded INTEGER);"
                             + "".join(store.ROOT_WINDOWS))
    connection.executemany("INSERT INTO tasks VALUES(?,?,NULL,NULL,NULL,0)",
                           [(f"root-{i:06d}", f"{i:08d}") for i in range(4000)])
    @contextlib.contextmanager
    def opened(*args, **kwargs):
        yield SimpleNamespace(conn=connection)
    monkeypatch.setattr(task_store, "open_store", opened)
    def measured(before):
        steps = [0]
        def step():
            steps[0] += 1
            return 0
        connection.set_progress_handler(step, 1)
        try:
            rows = root_ids_page(None, 10, before=before)
        finally:
            connection.set_progress_handler(None, 0)
        return rows, steps[0]
    try:
        head, head_steps = measured(None)
        tail, tail_steps = measured(["00000010", "root-000010"])
        assert len(head) == len(tail) == 10
        assert tail_steps < head_steps * 4 + 100, (head_steps, tail_steps)
    finally:
        connection.close()


@pytest.mark.parametrize("boundary", ["bindings", "lineage", "membership"])
def test_http_source_unavailability_is_retryable_not_bad_request(tmp_path, monkeypatch, boundary):
    from ouroboros import projects_registry, room_membership as membership
    seed(tmp_path, "retained")
    def unavailable(*args, **kwargs):
        raise ValueError("project_source_unknown")
    if boundary == "bindings":
        monkeypatch.setattr(projects_registry, "_load_bindings", unavailable)
    elif boundary == "lineage":
        monkeypatch.setattr(projects_registry.RoomResolver, "lineage", unavailable)
    else:
        monkeypatch.setattr(membership, "room_membership", lambda *a: unavailable)
    app = Starlette(routes=[Route("/api/rooms/{chat_id}/activity", api_room_activity)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/api/rooms/1/activity")
    assert response.status_code == 503
    assert response.json()["code"] == "activity_unavailable"
    assert response.headers["Retry-After"] == "1"


def test_http_conversion_cursor_is_conflict_not_unavailability(tmp_path):
    seed(tmp_path, "a")
    seed(tmp_path, "b")
    app = Starlette(routes=[Route("/api/rooms/{chat_id}/activity", api_room_activity)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        cursor = client.get("/api/rooms/1/activity", params={"limit": 1}).json()["next_cursor"]
        create_project(tmp_path, "moved")
        bind_task_to_project(tmp_path, "a", "moved", origin={"absent": "system"})
        response = client.get("/api/rooms/1/activity", params={"cursor": cursor})
    assert response.status_code == 409
    assert response.json()["code"] == "activity_view_changed"
