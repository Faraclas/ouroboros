"""Selection declarations survive real extension child catalogs and dispatch."""
import json
from pathlib import Path

import pytest

from ouroboros import extension_loader
from ouroboros.tools.registry import ToolRegistry
from tests._shared import clean_extension_runtime_state
from tests.test_extension_loader import _prepare_extension, _add_fake_native_dep, _mark_isolated_deps_installed


@pytest.mark.parametrize("native", [False, True])
def test_extension_selection_survives_reload_catalog_and_dispatch(tmp_path, monkeypatch, native):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    from ouroboros import safety, extension_process_runner as runner
    monkeypatch.setattr(safety, "check_safety", lambda *a, **k: (True, ""))
    catalogs, catalog_child = [], runner._catalog_child
    def counted(*args, **kwargs):
        catalogs.append(True)
        return catalog_child(*args, **kwargs)
    monkeypatch.setattr(runner, "_catalog_child", counted)
    plugin = '''
def read(ctx, segment=None):
    return 'Exact child selection.' * 900

def register(api):
    api.register_tool('read', read, description='Chosen window',
        schema={'type':'object','properties':{'segment':{'type':'integer'}}},
        result_selection={'arguments':['segment']})
'''
    clean_extension_runtime_state()
    try:
        loaded, repo, drive = _prepare_extension(tmp_path, "selection_child", plugin, permissions=["tool"],
            extra_frontmatter="dependencies:\n  - dummy_pkg\ncatalog_inputs: {settings: [], environment: []}\n" if native else "")
        if native:
            _add_fake_native_dep(loaded)
            _mark_isolated_deps_installed(drive, loaded)
        for _ in range(2):
            assert extension_loader.load_extension(loaded, lambda: {}, drive_root=drive) is None
            name = extension_loader.extension_surface_name("selection_child", "read")
            descriptor = extension_loader.get_tool(name)
            assert descriptor["result_selection"] == {"default":"automatic", "arguments":["segment"]}
            assert bool(descriptor.get("out_of_process")) is native
            registry = ToolRegistry(repo_dir=Path(__file__).resolve().parents[1], drive_root=drive)
            registry._ctx.task_contract = {}
            selected = registry.execute_result(name, {"segment": 9})
            automatic = registry.execute_result(name, {})
            assert selected.code == automatic.code == "OK", (selected.text, automatic.text)
            assert selected.text == automatic.text == 'Exact child selection.' * 900
            assert dict(selected.result_selection) == {"view": [None, None]}
            assert dict(automatic.result_selection) == {"view": None}
            extension_loader.unload_extension("selection_child")
        assert len(catalogs) == (1 if native else 0)
    finally:
        clean_extension_runtime_state()


def test_child_catalog_rejects_bad_declaration_and_preserves_legacy():
    from ouroboros.extension_child_catalog import _validate_child_tool_descriptor
    from ouroboros.contracts.plugin_api import ExtensionRegistrationError
    name = extension_loader.extension_surface_name("selected", "read")
    original = {"name":name, "schema":{"type":"object", "properties":{"segment":{}}}}
    assert _validate_child_tool_descriptor("selected", dict(original))["result_selection"] == {}
    with pytest.raises(ExtensionRegistrationError):
        _validate_child_tool_descriptor("selected", {**original, "result_selection":{"arguments":["missing"]}})


@pytest.mark.parametrize("generation,expected", [("1.3", [None, None]), ("2.0", [None, None]), ("2.1", None)])
def test_legacy_extension_windows_survive_but_new_unmarked_tools_do_not_guess(tmp_path, generation, expected):
    from ouroboros.extension_plugin_api import PluginAPIImpl
    from ouroboros.extension_registry_state import _PluginAPIConfig
    from ouroboros.tool_capabilities import declared_result_view
    api = PluginAPIImpl(_PluginAPIConfig(skill_name="legacy_selection", permissions=["tool"],
        env_allowlist=[], state_dir=tmp_path, settings_reader=lambda: {},
        plugin_api_generation=generation))
    schema = {"type":"object", "properties":{"limit":{"type":"integer"}}}
    api.register_tool("read", lambda **kw: "body", description="legacy control", schema=schema)
    descriptor = next(iter(api._staged.tools.values()))
    assert declared_result_view({"limit": 17}, descriptor["result_selection"]) == {"view": expected}
    # An explicit automatic policy overrides even a legacy adapter.
    api.register_tool("bulk", lambda **kw: "body", description="unmarked bulk", schema=schema,
                      result_selection={})
    bulk = next(d for d in api._staged.tools.values() if d["name"].endswith("_bulk"))
    assert declared_result_view({"limit": 17}, bulk["result_selection"]) == {"view": None}
