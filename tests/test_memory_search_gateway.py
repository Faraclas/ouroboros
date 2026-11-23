"""Gateway, contract, and FTS5 integration tests for GET /api/memory/search (WI-2.3)."""

from __future__ import annotations

import json
import pathlib

from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import memory_index
from ouroboros.chronicle_store import ChronicleStore
from ouroboros.gateway.router import collect_routes
from ouroboros.gateway.schema import contract_document


def _seed_memory(drive_root: pathlib.Path) -> None:
    kn_global = drive_root / "memory" / "knowledge"
    kn_global.mkdir(parents=True, exist_ok=True)
    (kn_global / "active-decision.md").write_text(
        "---\ntitle: Active Decision\ntype: note\nsummary: Active deployment rule\nupdated: 2026-11-23\n---\n"
        "Always verify origin push after local adoption of viper_search_alpha.\n",
        encoding="utf-8",
    )
    (kn_global / "retired-decision.md").write_text(
        "---\ntitle: Retired Decision\ntype: note\narchive:\n  at: '2026-10-01T00:00:00Z'\n  reason: superseded\n---\n"
        "Old rule mentioning viper_search_alpha from last month.\n",
        encoding="utf-8",
    )

    store = ChronicleStore(drive_root)
    mind = {
        "kind": "mind",
        "task_id": "t1",
        "focus": {
            "role": "root",
            "task_id": "t1",
            "parent_task_id": "",
            "root_task_id": "t1",
            "chat_id": 1,
            "room_id": "1",
        },
        "route": "unknown",
    }
    old_page = store.publish_page(
        room_id="1",
        text="Earlier page about viper_search_alpha before revision.",
        covers={"room_id": "1", "rows": ["r1"], "stream_span": [1, 1]},
        author=mind,
    ).record
    part = store.publish_part(
        room_id="1",
        text="Old part about viper_search_alpha.",
        member_ids=[old_page["id"]],
        author=mind,
        expected_sequence=store.room_head("1"),
    ).record
    acc = store.publish_account(
        room_id="1",
        text="Current account about viper_search_alpha after revision.",
        sources=[part["id"]],
        author=mind,
    ).record
    store.select_account(acc["id"], replaces=[part["id"]], author=mind, reason="current story", shown=True)

    proj_dir = drive_root / "projects" / "alpha-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "closure_brief.md").write_text(
        "---\nschema_version: 1\nproject_id: alpha-proj\nclosed_at: '2026-11-22T12:00:00+00:00'\n"
        "title: Alpha Closure\nsummary: Closed alpha project for viper_search_alpha\n---\n"
        "# Alpha Closure\n\nClosed alpha project for viper_search_alpha.\n",
        encoding="utf-8",
    )
    reg_path = drive_root / "state" / "projects.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(
        json.dumps(
            {
                "_schema_version": 2,
                "projects": [
                    {
                        "id": "alpha-proj",
                        "name": "Alpha Project",
                        "chat_id": 4242,
                        "lifecycle": "tombstoned",
                        "closed_at": "2026-11-22T12:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    memory_index.rebuild_index(drive_root)


def _client(tmp_path: pathlib.Path) -> TestClient:
    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    return TestClient(app)


def test_api_memory_search_contract_published() -> None:
    doc = contract_document()
    entry = next((row for row in doc["http"] if row["path"] == "/api/memory/search"), None)
    assert entry is not None
    assert entry["method"] == "GET"
    assert entry["typed"] is True
    assert set(entry["query"]) == {"query", "q", "scope", "include_archived", "limit"}
    assert "MemorySearchResponse" in doc["schemas"]
    props = doc["schemas"]["MemorySearchResponse"]["properties"]
    assert set(props["hits"]["items"]["properties"]) >= {
        "doc_key", "family", "kind", "status", "scope", "date_ymd",
        "canonical_address", "badge", "title", "snippet", "score",
    }
    assert set(props) == {
        "query",
        "scope",
        "include_archived",
        "limit",
        "hit_count",
        "elapsed_ms",
        "rebuilt",
        "hits",
    }


def test_api_memory_search_queries_filters_and_badges(tmp_path: pathlib.Path) -> None:
    _seed_memory(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/api/memory/search", params={"query": "viper_search_alpha"})
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["query"] == "viper_search_alpha"
    assert body["include_archived"] is False
    assert body["hit_count"] >= 3
    statuses = {hit["status"] for hit in body["hits"]}
    assert "ARCHIVED" not in statuses
    assert "SUPERSEDED" not in statuses
    assert "ACTIVE" in statuses
    assert "CLOSED" in statuses
    for hit in body["hits"]:
        assert hit["badge"] == f"[{hit['status']} | {hit['date_ymd'] or 'unknown'} | {hit['scope']} | {hit['canonical_address']}]"
        assert hit["canonical_address"]

    # q= alias and scope=project:alpha-proj
    scoped = client.get("/api/memory/search", params={"q": "viper_search_alpha", "scope": "project:alpha-proj"})
    assert scoped.status_code == 200
    scoped_body = scoped.json()
    assert scoped_body["scope"] == "project:alpha-proj"
    assert scoped_body["hit_count"] == 1
    assert scoped_body["hits"][0]["kind"] == "closure_brief"
    assert scoped_body["hits"][0]["canonical_address"] == "memory_read(node_id='closure_brief:alpha-proj')"

    # include_archived=true exposes ARCHIVED and SUPERSEDED
    archived = client.get(
        "/api/memory/search",
        params={"query": "viper_search_alpha", "include_archived": "true", "limit": "20"},
    )
    assert archived.status_code == 200
    arch_statuses = {hit["status"] for hit in archived.json()["hits"]}
    assert "ARCHIVED" in arch_statuses
    assert "SUPERSEDED" in arch_statuses


def test_api_memory_search_validates_inputs(tmp_path: pathlib.Path) -> None:
    _seed_memory(tmp_path)
    client = _client(tmp_path)

    assert client.get("/api/memory/search").status_code == 400
    assert client.get("/api/memory/search", params={"query": "   "}).status_code == 400
    assert client.get("/api/memory/search", params={"query": "x", "scope": "bad_scope"}).status_code == 400
    assert client.get("/api/memory/search", params={"query": "x", "include_archived": "maybe"}).status_code == 400
    assert client.get("/api/memory/search", params={"query": "x", "limit": "0"}).status_code == 400
    assert client.get("/api/memory/search", params={"query": "x", "limit": "51"}).status_code == 400
    assert client.get("/api/memory/search", params={"query": "x", "limit": "abc"}).status_code == 400


def test_search_readonly_never_creates_or_locks_index(tmp_path: pathlib.Path) -> None:
    assert memory_index.search_readonly(tmp_path, "anything") is None
    assert not memory_index.index_path(tmp_path).exists()
    assert not memory_index.lock_path(tmp_path).exists()

    # Passive GET /api/memory/search must not create or lock the index either.
    client = _client(tmp_path)
    missing = client.get("/api/memory/search", params={"query": "anything"})
    assert missing.status_code == 503
    assert missing.json()["reason_code"] == "index_not_built"
    assert not memory_index.index_path(tmp_path).exists()
    assert not memory_index.lock_path(tmp_path).exists()

    _seed_memory(tmp_path)
    memory_index.lock_path(tmp_path).unlink(missing_ok=True)
    res = memory_index.search_readonly(tmp_path, "viper_search_alpha")
    assert res is not None and res.hit_count >= 3
    assert not memory_index.lock_path(tmp_path).exists()
