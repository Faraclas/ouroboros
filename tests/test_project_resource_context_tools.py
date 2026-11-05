"""Resources reach real consumers without changing the existing path authority."""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ouroboros.projects_registry import create_project, get_reserved_project
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ouroboros.config as config

    home, data, body = tmp_path / "home", tmp_path / "data", tmp_path / "body"
    home.mkdir()
    data.mkdir()
    body.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setenv("OUROBOROS_USER_FILES_ROOT", str(home))
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_SAFETY_MODE", "off")
    repos = [home / "source", home / "other"]
    for path in repos:
        path.mkdir()
        subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True)
        (path / "input.txt").write_text(path.name, encoding="utf-8")
    create_project(data, "room", name="Room", working_dir=str(repos[0]))
    create_project(data, "foreign", name="Foreign")
    ctx = ToolContext(repo_dir=body, drive_root=data, task_id="root", project_id="room",
                      workspace_root=repos[0], workspace_mode="external", is_direct_chat=True)
    tools = ToolRegistry(repo_dir=body, drive_root=data)
    tools.set_context(ctx)
    return data, repos, ctx, tools


def call(tools, action, project_id="room", **kwargs):
    return tools.execute_result("project_resources", {"action": action, "project_id": project_id, **kwargs})


def test_real_tool_association_and_detach_leave_both_repos_usable_and_cwd_unchanged(world):
    data, repos, ctx, tools = world

    def consumers():
        statuses = []
        for path in repos:
            written = tools.execute_result("write_file", {
                "path": str(path / "note.txt"), "content": "kept\n"})
            read = tools.execute_result("read_file", {"path": str(path / "input.txt")})
            run = tools.execute_result("run_command", {
                "cwd": str(path), "cmd": [sys.executable, "-c", "import os; print(os.getcwd())"]})
            assert "kept" in (path / "note.txt").read_text(), written.text
            assert path.name in read.text and str(path) in run.text
            statuses.extend([written.status, read.status, run.status])
        assert ctx.active_repo_dir() == repos[0]
        return statuses

    before = consumers()
    added = call(tools, "add", path=str(repos[1]), label="Second source")
    assert added.status == "ok", added.text
    during = consumers()
    rows = json.loads(call(tools, "list").text)["resources"]
    assert {row["path"] for row in rows} == {str(path) for path in repos}
    # Root authority, not current-room membership, governs associations.
    assert call(tools, "add", "foreign", path=str(repos[1])).status == "ok"
    for row in rows:
        assert call(tools, "detach", resource_id=row["id"]).status == "ok"
    after = consumers()
    assert before == during == after == ["ok"] * 6
    assert json.loads(call(tools, "list").text)["resources"] == []
    assert get_reserved_project(data, "room")["working_dir"] == str(repos[0])
    assert all((path / ".git").is_dir() and (path / "note.txt").exists() for path in repos)


@pytest.mark.parametrize("mode", ["local_readonly_subagent", "acting_subagent"])
def test_child_lists_own_room_but_cannot_mutate_or_read_foreign_associations(world, mode):
    from ouroboros.contracts.task_constraint import TaskConstraint

    _data, repos, ctx, tools = world
    ctx.is_direct_chat = False
    ctx.task_metadata = {"parent_task_id": "parent", "delegation_role": "subagent"}
    ctx.task_constraint = TaskConstraint(mode=mode, surface="self_worktree" if mode == "acting_subagent" else "",
                                         write_root=str(repos[0]) if mode == "acting_subagent" else "")
    assert call(tools, "list").status == "ok"
    assert call(tools, "list", "foreign").status == "blocked"
    assert call(tools, "add", path=str(repos[1])).status == "blocked"
    assert call(tools, "detach", resource_id="default").status == "blocked"


def test_unknown_and_corrupt_registry_are_not_empty_success(world, monkeypatch):
    import ouroboros.project_management as owner

    _data, _repos, _ctx, tools = world
    missing = call(tools, "list", "missing")
    assert missing.status == "error" and "project_not_found" in missing.text
    monkeypatch.setattr(owner, "list_project_resources", lambda *_: (_ for _ in ()).throw(ValueError("bad registry")))
    unavailable = call(tools, "list")
    assert unavailable.status == "unavailable" and "bad registry" in unavailable.text


def test_resource_tool_keeps_legacy_shape_without_authored_words_but_http_retains_them(world):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from ouroboros.gateway.router import collect_routes

    data, repos, ctx, tools = world
    baseline = json.loads(call(tools, "add", path=str(repos[0])).text)
    app = Starlette(routes=collect_routes(data_dir=data))
    app.state.drive_root, app.state.repo_dir = data, ctx.repo_dir
    words = {"description": "Private_description_研究😀", "notes": "Private_notes\r\nBare\rCR\x00 "}
    with TestClient(app) as client:
        response = client.post("/api/projects/room/update", json={
            **words, "expected_content": {"description": "", "notes": ""}})
        assert response.status_code == 200
        added = call(tools, "add", path=str(repos[1]), label="Second source")
        assert added.status == "ok", added.text
        added_row = json.loads(added.text)
        duplicate = call(tools, "add", path=str(repos[1]), label="Ignored duplicate")
        assert duplicate.status == "ok" and json.loads(duplicate.text) == added_row
        listed = call(tools, "list")
        assert listed.status == "ok"
        assert set(json.loads(listed.text)) == {"project_id", "resources"}
        rid = added_row["resources"][-1]["id"]
        detached = call(tools, "detach", resource_id=rid)
        assert detached.status == "ok"
        for result in (added, duplicate, detached):
            row = json.loads(result.text)
            assert set(row) == set(baseline)  # all legacy mutation fields survive
            assert {key: value for key, value in row.items() if key != "resources"} == {
                key: value for key, value in baseline.items() if key != "resources"}
        for result in (added, duplicate, listed, detached):
            assert all(key not in json.loads(result.text) for key in words)
            assert "Private_description" not in result.text and "Private_notes" not in result.text
        # Management HTTP deliberately returns full detail, unlike the unrelated tool.
        added_http = client.post("/api/projects/room/resources", json={"path": str(repos[1])})
        assert added_http.status_code == 200
        assert {key: added_http.json()["project"][key] for key in words} == words
        assert {key: client.get("/api/projects/room/detail").json()["project"][key]
                for key in words} == words
    assert {key: get_reserved_project(data, "room")[key] for key in words} == words


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_actual_runtime_context_retains_paths_for_project_tasks_and_helpers(world, tmp_path, workspace, declared):
    from ouroboros.context import build_runtime_section
    from ouroboros.project_management import add_project_resource
    from tests.test_doc_context import _make_env_and_memory

    data, repos, _ctx, _tools = world
    add_project_resource(data, "room", str(repos[1]), "Other")
    from ouroboros.project_management import update_project_content
    update_project_content(data, "room", expected_content={"description": "", "notes": ""},
                           description="private-description-sentinel", notes="private-notes-sentinel")
    env, _memory = _make_env_and_memory(tmp_path / "runtime")
    task = {"id": "child" if declared else "root", "project_id": "room", "type": "task",
            "workspace_root": str(repos[0]) if workspace else "",
            "task_contract": {"input_sources": "declared"} if declared else {}}
    text = build_runtime_section(env, task)
    assert str(repos[0]) in text and str(repos[1]) in text
    assert "private-description-sentinel" not in text and "private-notes-sentinel" not in text
    assert "not ownership, access grants or locks" in text
    if declared:
        runtime = json.loads(text.split("## Runtime context\n\n", 1)[1])
        assert "working_dir" not in runtime["project_room"]
        assert "rule" not in runtime["project_room"]


def test_main_manifest_uses_same_resources_without_reopening_every_project(world, monkeypatch):
    from ouroboros import project_management as owner
    from ouroboros.server_routing_context import RootSnapshot, _main_routing_manifest

    data, repos, _ctx, _tools = world
    owner.add_project_resource(data, "room", str(repos[1]))
    owner.update_project_content(data, "room", expected_content={"description": "", "notes": ""},
                                 description="private-description-sentinel", notes="private-notes-sentinel")
    monkeypatch.setattr(owner, "list_project_resources", lambda *_: pytest.fail("manifest reopens registry"))
    host = SimpleNamespace(DRIVE_ROOT=data, RUNNING={}, PENDING=[])
    manifest = _main_routing_manifest(host, RootSnapshot())
    room = next(row for row in manifest["projects"] if row["project_id"] == "room")
    assert {item["path"] for item in room["resources"]} == {str(path) for path in repos}
    assert "resource_rule" not in room
    assert "not ownership, access grants or locks" in manifest["project_resource_rule"]
    assert "private-description-sentinel" not in json.dumps(manifest)
    assert "private-notes-sentinel" not in json.dumps(manifest)


def test_session_references_pin_paths_without_changing_canonical_work_order(world, monkeypatch):
    from ouroboros import subagent_work_order as work, skill_catalogue
    from ouroboros.project_management import add_project_resource, detach_project_resource

    data, repos, ctx, _tools = world
    monkeypatch.setattr(work, "session_design_navigation", lambda *a, **k: "")
    monkeypatch.setattr(work, "session_constitution_pointer", lambda *a, **k: "")
    monkeypatch.setattr(skill_catalogue, "session_skill_index", lambda *_: "")
    task = {"id": "child", "objective": "Read sources", "project_id": "room", "task_contract": {}}
    original = work.work_order_fingerprint(task)
    rows = add_project_resource(data, "room", str(repos[1]))["resources"]
    from ouroboros.project_management import update_project_content
    update_project_content(data, "room", expected_content={"description": "", "notes": ""},
                           description="private-description-sentinel", notes="private-notes-sentinel")
    refs = work.session_host_references(ctx, target_root=str(repos[0]), source_root=str(repos[0]))
    assert "private-description-sentinel" not in refs and "private-notes-sentinel" not in refs
    assert str(repos[0]) in refs and str(repos[1]) in refs
    assert "not additional authority" in refs
    detach_project_resource(data, "room", rows[-1]["id"])
    assert str(repos[1]) in refs  # pinned start request remains exactly what was sent
    assert work.work_order_fingerprint(task) == original


@pytest.mark.parametrize('declared', [False, True])
def test_unregistered_workspace_scope_is_not_an_unavailable_project(world, tmp_path, declared):
    from ouroboros.context import build_runtime_section
    from tests.test_doc_context import _make_env_and_memory

    _data, repos, _ctx, _tools = world
    env, _memory = _make_env_and_memory(tmp_path / 'runtime')
    task = {'id': 'workspace-task', 'project_id': 'proj_0123456789ab', 'type': 'task',
            'workspace_root': str(repos[0]),
            'task_contract': {'input_sources': 'declared'} if declared else {}}
    text = build_runtime_section(env, task)
    assert 'resources_unavailable' not in text and '"project_room"' not in text


def test_unregistered_workspace_session_has_no_spurious_project_warning(world, monkeypatch):
    from ouroboros import subagent_work_order as work, skill_catalogue

    _data, repos, ctx, _tools = world
    ctx.project_id = 'proj_0123456789ab'
    monkeypatch.setattr(work, 'session_design_navigation', lambda *a, **k: '')
    monkeypatch.setattr(work, 'session_constitution_pointer', lambda *a, **k: '')
    monkeypatch.setattr(skill_catalogue, 'session_skill_index', lambda *_: '')
    refs = work.session_host_references(ctx, target_root=str(repos[0]), source_root=str(repos[0]))
    assert 'PROJECT RESOURCE CONTEXT' not in refs and 'resources_unavailable' not in refs
