"""Installed-host ABI checks using only temporary state and staged registrations.

No load/enable/publish/skill lifecycle call occurs. Dispatch process execution is
replaced at its leaf to inspect real host root selection, not claimed real OOP.
"""
import asyncio
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import plugin
from board_store import BoardStore
from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.extension_plugin_api import PluginAPIImpl
from ouroboros.extension_registry_state import _PluginAPIConfig
from ouroboros.skill_loader import skill_state_dir_path
from ouroboros.tools.tool_context import ToolContext
from ouroboros import extension_process_runner as runner

ROOT = Path(__file__).resolve().parent


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.canonical = self.root / "canonical"
        self.state = skill_state_dir_path(self.canonical, "project-boards")

    def tearDown(self):
        self.temp.cleanup()

    def context(self, task_id="root", role="", acting=False):
        ctx = ToolContext(repo_dir=ROOT, drive_root=self.root / ("private-" + task_id))
        ctx.budget_drive_root = str(self.canonical)
        ctx.task_id = task_id
        ctx.project_id = "p"
        ctx.current_chat_id = 52
        ctx.task_metadata = {"budget_drive_root": str(self.canonical)}
        ctx.task_contract = {}
        if role:
            ctx.task_metadata["delegation_role"] = role
        if acting:
            ctx.task_metadata["tool_profile"] = "acting_subagent"
            ctx.task_constraint = TaskConstraint(mode="acting_subagent", surface="external_workspace")
        return ctx

    def api(self):
        return PluginAPIImpl(_PluginAPIConfig(skill_name="project-boards", permissions=["tool", "route", "fs"],
            env_allowlist=[], state_dir=self.state, settings_reader=lambda: {}, drive_root=self.canonical,
            plugin_api_generation="2.0"))

    def register(self, api, module=plugin):
        with patch("ouroboros.config.DATA_DIR", self.canonical):
            module.register(api)
        self.assertEqual(api.get_state_dir(), str(self.state))
        return {name.rsplit("_board_", 1)[-1]: row for name, row in api._staged.tools.items()}

    def test_registration_reports_missing_internal_host_symbols(self):
        from ouroboros import projects_registry, tool_access
        for module, name, message in (
            (projects_registry, 'get_reserved_project', 'strict project registry reader'),
            (tool_access, 'active_tool_profile', 'canonical tool-profile reader'),
        ):
            with self.subTest(symbol=name), patch.object(module, name, None):
                api = self.api()
                with self.assertRaisesRegex(RuntimeError, message):
                    with patch('ouroboros.config.DATA_DIR', self.canonical):
                        plugin.register(api)
                self.assertFalse(api._staged.tools)
                self.assertFalse(api._staged.routes)

    def test_host_package_style_import_uses_own_sibling_not_global_shadow(self):
        key = "boards_test_package"
        spec = importlib.util.spec_from_file_location(key, ROOT / "plugin.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
            self.assertEqual(module.BoardStore.__module__, key + ".board_store")
            self.register(self.api(), module)
        finally:
            for name in list(sys.modules):
                if name == key or name.startswith(key + "."):
                    del sys.modules[name]

    def test_real_registry_closed_item_retained_and_strict_missing(self):
        from ouroboros import projects_registry as projects
        projects.create_project(self.canonical, "p", name="Temporary verification room")
        store = BoardStore(self.state, plugin.project_reader(str(self.canonical)))
        item = store.mutate("capture", {"project_id": "p", "title": "Original", "request_id": "real-registry"}, plugin.INTERFACE_ACTOR)["item"]
        projects.begin_project_deletion(self.canonical, "p")
        projects.complete_project_deletion(self.canonical, "p")
        self.assertEqual(projects.get_reserved_project(self.canonical, "p", strict=True)["lifecycle"], "tombstoned")
        changed = store.mutate("update", {"item_id": item["id"], "expected_revision": 1,
            "request_id": "closed-edit", "changes": {"outcome": "Still retained"}}, plugin.INTERFACE_ACTOR)
        self.assertEqual(changed["item"]["outcome"], "Still retained")
        self.assertEqual(store.export("p")["items"][0]["id"], item["id"])
        self.assertEqual(store.project_reader("absent"), None)

    def test_actual_api_staging_canonical_root_helper_attribution_and_ctx_abi(self):
        api = self.api()
        with patch.object(plugin, "project_reader", return_value=lambda pid: {"id": pid}):
            tools = self.register(api)
        root = self.context()
        helper = self.context("acting-helper", "subagent", True)
        capture = tools["capture"]["handler"]
        first = json.loads(capture(root, project_id="p", title="Root", request_id="root"))
        second = json.loads(capture(helper, project_id="other", title="Helper", request_id="helper"))
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["item"]["created_by"]["task_id"], "acting-helper")
        self.assertEqual(second["item"]["created_by"]["source_project_id"], "p")
        self.assertFalse(root.drive_root.exists())
        self.assertFalse(helper.drive_root.exists())
        self.assertTrue((self.state / "boards.sqlite3").is_file())
        self.assertTrue(all(row["wants_ctx"] for row in tools.values()))
        self.assertEqual(len(api._staged.routes), 6)
        self.assertFalse(api._staged.supervised_tasks)

    def test_actual_process_dispatch_chooses_same_canonical_root_for_root_and_helper(self):
        seen = []
        skill = SimpleNamespace(name="project-boards", skill_dir=ROOT)
        def child(payload, **kwargs):
            seen.append((payload, kwargs))
            return {"result": "{}"}
        with patch.object(runner, "_skill_for_dispatch", return_value=skill), \
             patch.object(runner, "_base_env_for_skill", return_value={}), \
             patch.object(runner, "_extension_has_model_credentials", return_value=False), \
             patch.object(runner, "_operator_settings_for_child", return_value=None), \
             patch.object(runner, "_run_child", side_effect=child):
            for ctx in (self.context(), self.context("helper", "subagent", True)):
                self.assertEqual(runner.dispatch_extension_tool_subprocess({"skill": "project-boards", "name": "x", "skills_repo_path": str(ROOT)}, ctx, {}), "{}")
        self.assertEqual(len(seen), 2)
        for payload, kwargs in seen:
            self.assertEqual(Path(payload["drive_root"]), self.canonical)
            self.assertEqual(kwargs["drive_root"], self.canonical)
            self.assertEqual(skill_state_dir_path(Path(payload["drive_root"]), "project-boards"), self.state)
            self.assertNotEqual(payload["ctx"]["drive_root"], payload["drive_root"])

    def test_actual_isolated_context_readonly_is_preserved_and_acting_gap_is_explicit(self):
        readonly = self.context("readonly", "subagent")
        readonly.task_metadata["tool_profile"] = "local_readonly_subagent"
        copied = runner._apply_tool_context_payload(self.context("copy"), runner._tool_context_payload(readonly))
        with self.assertRaises(Exception) as caught:
            plugin.agent_actor(copied)
        self.assertEqual(caught.exception.code, "readonly")
        acting = self.context("acting", "subagent", True)
        # Host isolated serialization currently omits task_constraint; a fresh child
        # consequently resolves conservative read-only despite the frozen profile.
        copied = runner._apply_tool_context_payload(ToolContext(repo_dir=ROOT, drive_root=self.canonical), runner._tool_context_payload(acting))
        with self.assertRaises(Exception) as caught:
            plugin.agent_actor(copied)
        self.assertEqual(caught.exception.code, "readonly")
        self.assertEqual(plugin.agent_actor(acting)["task_id"], "acting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
