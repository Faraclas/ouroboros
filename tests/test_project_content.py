"""Authored Project words use exact-baseline saves, never routing or activity."""
from concurrent.futures import ThreadPoolExecutor
import json
import pathlib
import subprocess
import sys
import threading

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import project_management as management
from ouroboros import projects_registry as registry
from ouroboros.gateway import projects as gateway
from ouroboros.gateway.client_contracts import ProjectContentConflictResponse
from ouroboros.gateway.router import collect_routes
from ouroboros.gateway.schema import validate_ingress
from tests.test_client_contract_bindings import assert_bound


UPDATE = "/api/projects/alpha/update"
TOKEN = "POST /api/projects/{project_id}/update"
EMPTY = {"description": "", "notes": ""}


@pytest.fixture
def content_client(tmp_path, monkeypatch):
    root = tmp_path / "canonical"
    monkeypatch.setattr("ouroboros.config.DATA_DIR", root)
    broadcasts = []
    monkeypatch.setattr(gateway, "_broadcast_projects_changed",
                        lambda pid, chat: broadcasts.append((pid, chat)))
    app = Starlette(routes=collect_routes(data_dir=root))
    app.state.drive_root = root
    app.state.repo_dir = pathlib.Path(__file__).resolve().parents[1]
    with TestClient(app) as client:
        yield client, root, broadcasts


def saved_content(client):
    response = client.get("/api/projects/alpha/detail")
    assert response.status_code == 200
    row = response.json()["project"]
    return {key: row[key] for key in EMPTY}


def save(client, expected, **updates):
    return client.post(UPDATE, json={**updates, "expected_content": expected})


def without_content(row):
    return {key: value for key, value in row.items() if key not in EMPTY}


def test_legacy_reads_are_empty_without_migration(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", name="Alpha", origin="owner_ui")
    path = registry._registry_path(root)
    before = path.read_bytes()
    assert saved_content(client) == EMPTY
    catalog = client.get("/api/projects/catalog")
    assert catalog.status_code == 200
    row = catalog.json()["projects"][0]
    assert row["description"] == "" and "notes" not in row
    assert path.read_bytes() == before
    assert "description" not in json.loads(before)["projects"][0]
    assert "notes" not in json.loads(before)["projects"][0]


@pytest.mark.parametrize("closed", [False, True])
def test_save_preserves_words_and_every_noncontent_room_fact(content_client, closed):
    client, root, broadcasts = content_client
    registry.create_project(root, "alpha", name="Alpha", origin="owner_ui")
    if closed:
        registry.begin_project_deletion(root, "alpha")
        registry.complete_project_deletion(root, "alpha")
    before = registry.get_reserved_project(root, "alpha", strict=True)
    basis = registry.project_admission_view(root, "alpha", frozen=True)
    words = {
        "description": "  Straße_研究 😀\t<script> & \x00  ",
        "notes": "  First_line\r\n\t第二行 😀\n\n<script>x</script>\x00  ",
    }
    response = save(client, EMPTY, **words)
    assert response.status_code == 200
    assert assert_bound(TOKEN, response) == "ProjectUpdateResponse"
    assert {key: response.json()["project"][key] for key in EMPTY} == words
    assert saved_content(client) == words
    raw = json.loads(registry._registry_path(root).read_bytes())["projects"][0]
    assert {key: raw[key] for key in EMPTY} == words
    after = registry.get_reserved_project(root, "alpha", strict=True)
    assert without_content(after) == without_content(before)
    assert registry.project_admission_view(root, "alpha", frozen=True) == basis
    if not closed:
        with registry.project_admission_guard(root, basis):
            pass
        rows = client.get("/api/projects").json()["projects"]
        assert len(rows) == 1
        assert not any(key in rows[0] for key in EMPTY)
        assert not any(key in registry.projects_summary(root)[0] for key in EMPTY)
        state = client.get("/api/state")
        assert state.status_code == 200
        state_row, = state.json()["projects"]
        assert state_row["id"] == "alpha" and not (state_row.keys() & EMPTY.keys())
        assert all(value not in json.dumps(state.json(), ensure_ascii=False) for value in words.values())
    assert broadcasts == [("alpha", before["chat_id"])]


def test_content_public_facade_and_http_conflict_union(content_client):
    from ouroboros.gateway import client_contracts, contracts

    client, _, _ = content_client
    for name in ("ProjectContent", "ProjectContentConflictResponse"):
        assert getattr(contracts, name) is getattr(client_contracts, name)
        assert name in contracts.__all__ and name in client_contracts.__all__
    document = client.get("/api/contract").json()
    route = next(row for row in document["http"] if f"{row['method']} {row['path']}" == TOKEN)
    assert route["responses"]["409"]["schemas"] == ["ProjectContentConflictResponse", "ErrorResponse"]
    conflict = document["schemas"]["ProjectContentConflictResponse"]
    assert conflict["required"] == ["current_content", "error", "reason_code"]
    assert conflict["properties"]["current_content"]["required"] == ["description", "notes"]


def test_partial_clear_and_legacy_name_pin_keep_neighbor_content(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", name="Original")
    assert save(client, EMPTY, notes="  notes\r\n ").status_code == 200
    current = {"description": "", "notes": "  notes\r\n "}
    assert saved_content(client) == current
    assert save(client, current, description="  description_ ").status_code == 200
    current["description"] = "  description_ "
    assert saved_content(client) == current
    legacy = client.post(UPDATE, json={"name": "Renamed", "pinned": True})
    assert legacy.status_code == 200
    assert saved_content(client) == current
    assert save(client, current, description="").status_code == 200
    current["description"] = ""
    assert saved_content(client) == current
    assert save(client, current, notes="").status_code == 200
    assert saved_content(client) == EMPTY
    row = registry.get_project(root, "alpha", strict=True)
    assert row["name"] == "Renamed" and row["pinned"] is True


def test_open_abi_extra_keys_are_tolerated_but_not_compared_or_persisted(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    response = client.post(UPDATE, json={
        "notes": "Kept\r\n", "future_update": "Not content",
        "expected_content": {**EMPTY, "future_baseline": {"revision": 9}}})
    assert response.status_code == 200
    current = {"description": "", "notes": "Kept\r\n"}
    assert saved_content(client) == current
    # Changing an unknown baseline key is not a stale-content conflict.
    response = save(client, {**current, "future_baseline": False}, description="New")
    assert response.status_code == 200
    raw = json.loads(registry._registry_path(root).read_bytes())["projects"][0]
    assert not ({"expected_content", "future_update", "future_baseline"} & raw.keys())
    assert raw["description"] == "New" and raw["notes"] == current["notes"]


def test_fresh_process_reads_same_saved_root_through_public_gateway(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", origin="owner_ui")
    words = {"description": "  Restart_😀 e\u0301  ", "notes": "CRLF\r\nBare\rCR\n😀\x00 "}
    assert save(client, EMPTY, **words).status_code == 200
    # A new interpreter imports this candidate source and reconstructs the public app.
    # Bytes on stdout avoid universal-newline conversion of authored CRLF/bare CR.
    script = '''
import json, pathlib, sys
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ouroboros.gateway.router import collect_routes
root = pathlib.Path(sys.argv[1])
import ouroboros.project_management as source
assert pathlib.Path(source.__file__).resolve() == pathlib.Path.cwd() / 'ouroboros/project_management.py'
app = Starlette(routes=collect_routes(data_dir=root))
app.state.drive_root, app.state.repo_dir = root, pathlib.Path.cwd()
with TestClient(app) as client:
    detail = client.get('/api/projects/alpha/detail')
    catalog = client.get('/api/projects/catalog?q=Restart_')
    assert detail.status_code == catalog.status_code == 200
    print(json.dumps({'detail': detail.json(), 'catalog': catalog.json()}, ensure_ascii=True))
'''
    result = subprocess.run([sys.executable, "-c", script, str(root)],
                            capture_output=True, timeout=60, check=True)
    observed = json.loads(result.stdout)
    assert {key: observed["detail"]["project"][key] for key in EMPTY} == words
    catalog_row, = observed["catalog"]["projects"]
    assert catalog_row["description"] == words["description"] and "notes" not in catalog_row
    assert saved_content(client) == words


def test_concurrent_content_and_legacy_writers_keep_all_fields(content_client):
    from ouroboros.composer_cognition import update_project_cognition

    client, root, _ = content_client
    registry.create_project(root, "alpha", origin="owner_ui")
    words = {"description": "Content winner", "notes": "Exact\r\nnotes\r "}
    start = threading.Barrier(3)

    def content():
        start.wait(timeout=10)
        response = save(client, EMPTY, **words)
        assert response.status_code == 200

    def metadata():
        start.wait(timeout=10)
        response = client.post(UPDATE, json={"name": "Concurrent name", "pinned": True})
        assert response.status_code == 200

    def cognition():
        start.wait(timeout=10)
        update_project_cognition(root, "alpha", subagents_mode="swarm")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(operation) for operation in (content, metadata, cognition)]
        for future in futures:
            future.result(timeout=30)
    row = registry.get_project(root, "alpha", strict=True)
    assert row["name"] == "Concurrent name" and row["pinned"] is True
    assert row["cognition"]["subagents_mode"] == "swarm"
    assert saved_content(client) == words
    stale = save(client, EMPTY, notes="No overwrite")
    assert stale.status_code == 409 and stale.json()["current_content"] == words


def test_supported_unrelated_writers_preserve_content_and_sibling(content_client):
    from ouroboros import composer_cognition, dialogue_store
    from ouroboros.tools.project_journal import record_task_finalization

    client, root, _ = content_client
    registry.create_project(root, "alpha", origin="owner_ui")
    registry.create_project(root, "beta", origin="owner_ui")
    words = {"description": "  Persistent 研究_😀 ", "notes": "Stored\r\nBare\rCR\x00 "}
    sibling = {"description": "Sibling", "notes": "sibling words"}
    assert save(client, EMPTY, **words).status_code == 200
    other = client.post("/api/projects/beta/update", json={**sibling, "expected_content": EMPTY})
    assert other.status_code == 200
    sibling_before = registry.get_reserved_project(root, "beta", strict=True)

    def check():
        assert saved_content(client) == words
        raw = json.loads(registry._registry_path(root).read_bytes())
        assert {key: next(row for row in raw["projects"] if row["id"] == "alpha")[key]
                for key in EMPTY} == words
        assert next(row for row in raw["projects"] if row["id"] == "beta") == sibling_before

    composer_cognition.update_project_cognition(root, "alpha", context_mode="max", subagents_mode="swarm")
    check()
    # Exercise the supported legacy cognition migration writer, not only its no-op path.
    legacy = json.loads(registry._registry_path(root).read_bytes())
    next(row for row in legacy["projects"] if row["id"] == "alpha").pop("cognition")
    registry._registry_path(root).write_text(json.dumps(legacy), encoding="utf-8")
    composer_cognition.ensure_project_cognition(root)
    assert "cognition" in registry.get_project(root, "alpha")
    check()
    registry.update_project(root, "alpha", description="No CAS bypass", notes="No generic content writer")
    check()
    resource = client.post("/api/projects/alpha/resources", json={"path": str(root / "missing")})
    assert resource.status_code == 200
    rid = resource.json()["project"]["resources"][-1]["id"]
    check()
    assert client.post(f"/api/projects/alpha/resources/{rid}/detach", json={}).status_code == 200
    check()
    registry.touch_project(root, "alpha")
    check()
    chat_id = registry.get_project(root, "alpha")["chat_id"]
    dialogue_store.append(root, "chat", {"chat_id": chat_id, "role": "user", "content": "Activity",
                                        "ts": "2026-01-01T00:00:00Z"}, feed=True)
    assert registry.projects_summary(root, chat_id=chat_id)[0]["visible_revision"] > 0
    check()
    # The public finalization writer stamps the registry pointer, not authored words.
    record_task_finalization("alpha", {"id": "finished-root", "root_task_id": "finished-root"},
                             objective="Finished", kind="done", exec_status="completed", drive_root=root)
    assert registry.get_project(root, "alpha")["last_task_result_id"] == "finished-root"
    check()
    assert client.post("/api/projects", json={"id": "alpha"}).json()["project"]["created"] is False
    assert client.post("/api/projects", json={"id": "gamma", "name": "Third"}).status_code == 200
    check()
    registry.begin_project_deletion(root, "alpha")
    registry.fail_project_deletion(root, "alpha", "Fixture: quiescence incomplete")
    assert registry.get_reserved_project(root, "alpha")["delete_error"] == "Fixture: quiescence incomplete"
    check()
    registry.complete_project_deletion(root, "alpha")
    check()


def test_http_close_closed_edit_and_reopen_keep_authored_content(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", origin="owner_ui")
    first = {"description": "Before close", "notes": "Before\r\nclose"}
    assert save(client, EMPTY, **first).status_code == 200
    closed = client.post("/api/projects/alpha/delete", json={})
    assert closed.status_code == 200
    for worker in [thread for thread in threading.enumerate()
                   if thread.name == "project-delete-alpha"]:
        worker.join(timeout=60)
        assert not worker.is_alive(), "Project close did not quiesce"
    assert registry.get_reserved_project(root, "alpha")["lifecycle"] == registry.PROJECT_TOMBSTONED
    assert saved_content(client) == first
    second = {"description": "Closed_研究", "notes": "Edited while closed\r\n"}
    assert save(client, first, **second).status_code == 200
    for body in ({"name": "Not active"}, {"pinned": True}):
        assert client.post(UPDATE, json=body).status_code == 404
    reopened = client.post("/api/projects/alpha/reopen", json={})
    assert reopened.status_code == 200
    assert {key: reopened.json()["project"][key] for key in EMPTY} == second
    assert saved_content(client) == second


def test_catalog_searches_casefolded_description_not_notes(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", name="Alpha")
    registry.create_project(root, "beta", name="Beta")
    words = {"description": "Straße_研究", "notes": "notes_only_unique"}
    assert save(client, EMPTY, **words).status_code == 200
    for query in ("STRASSE", "_研究", "ALPHA"):
        response = client.get("/api/projects/catalog", params={"q": query})
        assert response.status_code == 200
        row, = response.json()["projects"]
        assert row["id"] == "alpha" and row["description"] == words["description"]
        assert "notes" not in row
    response = client.get("/api/projects/catalog", params={"q": "notes_only_unique"})
    assert response.json()["total"] == 0
    assert all("notes" not in row for row in client.get("/api/projects/catalog").json()["projects"])


def test_two_editors_compare_both_fields_and_expose_exact_winner(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    baseline = saved_content(client)
    edits = ({"description": "Editor_one"}, {"notes": "Editor_two\r\n"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda edit: save(client, baseline, **edit), edits))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 409)
    current = {key: winner.json()["project"][key] for key in EMPTY}
    assert saved_content(client) == current
    assert loser.json()["reason_code"] == "project_content_conflict"
    assert loser.json()["current_content"] == current
    assert assert_bound(TOKEN, loser) == "ProjectContentConflictResponse"
    assert not validate_ingress(loser.json(), ProjectContentConflictResponse)
    path = registry._registry_path(root)
    before = path.read_bytes()
    stale = save(client, baseline, description="Must_not_replace_either_field")
    assert stale.status_code == 409 and path.read_bytes() == before
    # Recovery deliberately adopts the supplied baseline; the server never overwrites on conflict.
    assert save(client, stale.json()["current_content"],
                description="Explicit recovery", notes="Kept draft\r\n").status_code == 200


@pytest.mark.parametrize("body", [
    {"description": "x"},
    {"notes": "x", "expected_content": None},
    {"notes": "x", "expected_content": {}},
    {"notes": "x", "expected_content": {"description": ""}},
    {"notes": "x", "expected_content": {"description": "", "notes": False}},
    {"expected_content": EMPTY},
    {"description": None, "expected_content": EMPTY},
    {"description": 1, "expected_content": EMPTY},
    {"description": False, "expected_content": EMPTY},
    {"description": [], "expected_content": EMPTY},
    {"description": {}, "expected_content": EMPTY},
    {"notes": None, "expected_content": EMPTY},
    {"notes": 1, "expected_content": EMPTY},
    {"notes": False, "expected_content": EMPTY},
    {"notes": [], "expected_content": EMPTY},
    {"notes": {}, "expected_content": EMPTY},
    {"description": "x" * 501, "expected_content": EMPTY},
    {"notes": "x" * 20001, "expected_content": EMPTY},
    {"description": "line\n", "expected_content": EMPTY},
    {"description": "line\rbreak", "expected_content": EMPTY},
    {"description": "line\u2028break", "expected_content": EMPTY},
    {"description": "x", "name": "Mixed", "expected_content": EMPTY},
    {"notes": "x", "pinned": True, "expected_content": EMPTY},
    {"name": "Mixed", "expected_content": EMPTY},
])
def test_invalid_or_mixed_content_refuses_before_writing(content_client, body):
    client, root, broadcasts = content_client
    registry.create_project(root, "alpha")
    path = registry._registry_path(root)
    before = path.read_bytes()
    response = client.post(UPDATE, json=body)
    assert response.status_code == 400
    assert path.read_bytes() == before and broadcasts == []


def test_unicode_codepoint_boundaries_are_not_utf16_or_utf8_limits(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    words = {"description": "😀" * 500, "notes": "😀" * 20000}
    assert save(client, EMPTY, **words).status_code == 200
    assert saved_content(client) == words
    path = registry._registry_path(root)
    before = path.read_bytes()
    assert save(client, words, description="😀" * 501).status_code == 400
    assert save(client, words, notes="😀" * 20001).status_code == 400
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["description", "notes", "expected_description", "expected_notes"])
def test_unpaired_surrogates_refuse_before_writing(content_client, field):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    body = {"description": "valid", "expected_content": dict(EMPTY)}
    if field.startswith("expected_"):
        body["expected_content"][field.removeprefix("expected_")] = "\ud800"
    else:
        body[field] = "\ud800"
    path = registry._registry_path(root)
    before = path.read_bytes()
    response = client.post(UPDATE, content=json.dumps(body, ensure_ascii=True).encode("utf-8"),
                           headers={"content-type": "application/json"})
    assert response.status_code == 400 and path.read_bytes() == before


@pytest.mark.parametrize("body", [b"{", b"[]", b"null"])
def test_invalid_json_or_nonobject_body_is_400(content_client, body):
    client, root, broadcasts = content_client
    registry.create_project(root, "alpha")
    path = registry._registry_path(root)
    before = path.read_bytes()
    response = client.post(UPDATE, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert path.read_bytes() == before and broadcasts == []


@pytest.mark.parametrize("invalid", [b"\xff", b"\xed\xa0\x80"])
def test_invalid_utf8_refuses_before_writing(content_client, invalid):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    path = registry._registry_path(root)
    before = path.read_bytes()
    body = b'{"notes":"' + invalid + b'","expected_content":{"description":"","notes":""}}'
    response = client.post(UPDATE, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400 and path.read_bytes() == before


@pytest.mark.parametrize("field", ["description", "notes"])
@pytest.mark.parametrize("value", [None, False, 7, [], {}, "\ud800"])
def test_malformed_saved_content_is_unknown_not_legacy_empty(content_client, field, value):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    path = registry._registry_path(root)
    data = json.loads(path.read_bytes())
    data["projects"][0][field] = value
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    before = path.read_bytes()
    assert client.get("/api/projects/alpha/detail").status_code == 500
    assert client.get("/api/projects/catalog").status_code == 500
    assert save(client, EMPTY, notes="Do not clobber unknown saved words").status_code == 500
    assert path.read_bytes() == before


def test_missing_closing_and_racing_close_do_not_save(content_client, monkeypatch):
    client, root, _ = content_client
    response = save(client, EMPTY, notes="Missing")
    assert response.status_code == 404
    registry.create_project(root, "alpha")
    original = management.update_project_content

    def close_first(*args, **kwargs):
        registry.begin_project_deletion(root, "alpha")
        return original(*args, **kwargs)

    monkeypatch.setattr(management, "update_project_content", close_first)
    response = save(client, EMPTY, notes="Must not save")
    assert response.status_code == 409 and response.json()["reason_code"] == "project_closing"
    assert "current_content" not in response.json()
    path = registry._registry_path(root)
    before = path.read_bytes()
    assert save(client, EMPTY, notes="Still closing").status_code == 409
    assert path.read_bytes() == before
    assert saved_content(client) == EMPTY


def test_failed_save_and_corrupt_registry_preserve_original_bytes(content_client, monkeypatch):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    path = registry._registry_path(root)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        def cannot_save(*args, **kwargs):
            raise OSError("fixture: registry is read-only")
        patch.setattr(registry, "_save", cannot_save)
        assert save(client, EMPTY, notes="Keep draft on client").status_code == 500
    assert path.read_bytes() == before and saved_content(client) == EMPTY
    path.write_bytes(b"{broken")
    before = path.read_bytes()
    assert save(client, EMPTY, notes="Do not replace corrupt registry").status_code == 500
    assert path.read_bytes() == before


def test_invalid_saved_content_does_not_half_commit_resource_or_reopen(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha", working_dir=str(root / "folder"))
    path = registry._registry_path(root)
    data = json.loads(path.read_bytes())
    data["projects"][0]["notes"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    assert client.post("/api/projects/alpha/resources", json={"path": str(root / "other")}).status_code == 500
    assert path.read_bytes() == before
    assert client.post("/api/projects/alpha/resources/default/detach", json={}).status_code == 500
    assert path.read_bytes() == before
    registry.begin_project_deletion(root, "alpha")
    registry.complete_project_deletion(root, "alpha")
    before = path.read_bytes()
    assert client.post("/api/projects/alpha/reopen", json={}).status_code == 500
    assert path.read_bytes() == before
    assert registry.get_reserved_project(root, "alpha")["lifecycle"] == registry.PROJECT_TOMBSTONED


def test_strict_target_write_preserves_unrelated_raw_row(content_client):
    client, root, _ = content_client
    registry.create_project(root, "alpha")
    registry.create_project(root, "beta")
    path = registry._registry_path(root)
    data = json.loads(path.read_bytes())
    other = next(row for row in data["projects"] if row["id"] == "beta")
    other.update(description=None, notes=["unrelated malformed data"], routing_generation="unknown")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert save(client, EMPTY, notes="Healthy selected room").status_code == 200
    after = json.loads(path.read_bytes())
    assert next(row for row in after["projects"] if row["id"] == "beta") == other
