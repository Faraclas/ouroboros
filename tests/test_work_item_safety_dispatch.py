"""Disposable real Work Item handlers through the registry, not owner boards."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import extension_registry_state as state, safety
from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
from ouroboros.extension_plugin_api import PluginAPIImpl
from ouroboros.extension_registry_state import _PluginAPIConfig
from ouroboros.tools.registry import ToolRegistry


@pytest.fixture
def board_registry(tmp_path, monkeypatch):
    from ouroboros import config, extension_loader, extension_process_runner
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    root = Path(__file__).resolve().parents[1] / "skills" / "project-boards"
    spec = importlib.util.spec_from_file_location("safety_board_fixture", root / "plugin.py",
                                                submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "full")
    monkeypatch.setattr(safety, "_run_llm_check", lambda *a, **k: pytest.fail("local bookkeeping invoked Safety model"))
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *a, **k: True)
    monkeypatch.setattr(extension_process_runner, "disclose_inprocess_extension_dispatch", lambda *a, **k: None)
    create_project(tmp_path, "p", name="Disposable Safety board")
    write_task_result(tmp_path, "fixture-work", "running", root_task_id="fixture-work")
    api = PluginAPIImpl(_PluginAPIConfig(skill_name="project-boards", permissions=["tool", "route"],
        env_allowlist=[], state_dir=tmp_path / "state" / "skills" / "project-boards",
        settings_reader=lambda: {}, drive_root=tmp_path, plugin_api_generation="2.0"))
    plugin.register(api)
    manifest = parse_skill_manifest_text((root / "SKILL.md").read_text(encoding="utf-8"))
    bundle = state._ExtensionRegistrations(generation_digest="fixture", loaded_skill=SimpleNamespace(manifest=manifest))
    monkeypatch.setitem(state._extensions, "project-boards", bundle)
    for name, descriptor in api._staged.tools.items():
        monkeypatch.setitem(state._tools, name, {**descriptor, "extension_generation": "fixture"})
    registry = ToolRegistry(repo_dir=root.parents[1], drive_root=tmp_path)
    registry._ctx.task_id = "fixture-work"
    registry._ctx.project_id = "p"
    registry._ctx.task_contract = {}
    names = {name.rsplit("_board_", 1)[-1]: name for name in api._staged.tools}
    return registry, names


def test_capture_complete_comment_and_conflict_keep_real_outcome(board_registry):
    registry, names = board_registry
    result = json.loads(registry.execute(names["capture"], {"project_id": "p", "title": "Disposable",
                                                           "request_id": "capture"}))
    assert result["ok"], result
    item = result["item"]
    result = json.loads(registry.execute(names["update"], {"item_id": item["id"], "expected_revision": 1,
        "request_id": "done", "changes": {"disposition": "done", "outcome": "Verified disposable result"}}))
    assert result["ok"] and result["item"]["disposition"] == "done", result
    duplicate = json.loads(registry.execute(names["update"], {"item_id": item["id"], "expected_revision": 1,
        "request_id": "done", "changes": {"disposition": "done", "outcome": "Verified disposable result"}}))
    assert duplicate["ok"] and duplicate["item"]["disposition"] == "done"
    conflict = registry.execute(names["update"], {"item_id": item["id"], "expected_revision": 1,
        "request_id": "stale", "changes": {"disposition": "planned"}})
    assert json.loads(conflict)["ok"] is False
    comment = json.loads(registry.execute(names["comment"], {"item_id": item["id"], "text": "Evidence, not review PASS",
                                                            "request_id": "comment"}))
    assert comment["ok"], comment
    saved = json.loads(registry.execute(names["get"], {"item_id": item["id"]}))
    assert saved["item"]["disposition"] == "done"
    assert saved["item"]["created_by"]["task_id"] == "fixture-work"


def test_readonly_completion_does_not_borrow_enabled_extension(board_registry):
    registry, names = board_registry
    registry._ctx.task_metadata["delegation_role"] = "subagent"
    registry._ctx.task_metadata["tool_profile"] = "local_readonly_subagent"
    result = registry.execute(names["capture"], {"project_id": "p", "title": "Forbidden", "request_id": "readonly"})
    assert "readonly" in result.lower() or "read-only" in result.lower(), result
    registry._ctx.task_metadata = {}
    rows = json.loads(registry.execute(names["find"], {"project_id": "p"}))
    assert not rows["items"]


@pytest.mark.parametrize("place_state", ["silent", "paused"])
def test_bookkeeping_skip_does_not_override_place_guard(board_registry, monkeypatch, place_state):
    from ouroboros import places
    registry, names = board_registry
    place = places.Place("blocked", "project-boards", "fixture", state=place_state, label="Closed place")
    monkeypatch.setattr(places, "load_places", lambda *a: [place])
    result = registry.execute(names["capture"], {"project_id": "p", "title": "Forbidden", "request_id": "place"})
    assert "RESOURCE_CONSTRAINT_BLOCKED" in result, result
    monkeypatch.setattr(places, "load_places", lambda *a: [])
    assert not json.loads(registry.execute(names["find"], {"project_id": "p"}))["items"]


def test_unavailable_extension_does_not_gain_execution(board_registry, monkeypatch):
    from ouroboros import extension_loader
    registry, names = board_registry
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *a, **k: False)
    monkeypatch.setattr(extension_loader, "unload_extension", lambda *a, **k: None)
    result = registry.execute(names["capture"], {"project_id": "p", "title": "Forbidden", "request_id": "off"})
    assert "not live for this task" in result and "Nothing was executed" in result, result
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *a, **k: True)
    assert not json.loads(registry.execute(names["find"], {"project_id": "p"}))["items"]


def test_owner_pause_keeps_completion_unsent(board_registry, monkeypatch):
    from supervisor.owner_pause_control import request_owner_pause
    from tests._budget_pause_exact_helpers import _install_queue
    registry, names = board_registry
    ctx = registry._ctx
    _, _, workers = _install_queue(ctx.drive_root, monkeypatch)
    workers.RUNNING[ctx.task_id] = {"task": {"id": ctx.task_id, "type": "task", "chat_id": 0,
                                              "root_task_id": ctx.task_id}, "worker_id": 0, "attempt": 1}
    outcome = request_owner_pause(ctx.task_id, request_id="fixture-pause")
    assert outcome.get("ok"), outcome
    result = registry.execute(names["capture"], {"project_id": "p", "title": "Forbidden", "request_id": "pause"})
    assert "pause" in result.lower(), result
