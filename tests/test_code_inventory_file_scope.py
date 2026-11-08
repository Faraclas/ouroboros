"""Explicit-file inventories avoid unrelated cache hydration, not admission or parsing."""
from __future__ import annotations

import json
import subprocess

import pytest

from ouroboros import code_intelligence as ci
from ouroboros.tools.registry import ToolRegistry


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "picked.py").write_text("def target():\n    return helper()\n", encoding="utf-8")
    (repo / "other.py").write_text("from picked import target\ntarget()\n", encoding="utf-8")
    data = tmp_path / "data"
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    ci.build_code_inventory(repo, drive_root=data)
    return repo, data, registry


FILE_QUERIES = [
    ("symbols", {}), ("digest", {}), ("definition", {"query": "target"}),
    ("callees", {"query": "target"}), ("relevant_files", {"query": "target"}),
]


@pytest.mark.parametrize("op,options", FILE_QUERIES)
def test_real_registry_file_modes_never_load_full_cache(project, monkeypatch, op, options):
    repo, data, registry = project
    cache = ci.inventory_cache_path(repo, data)
    before, before_mtime = cache.read_bytes(), cache.stat().st_mtime_ns
    # Cold current evidence is the reference, including coverage/header, not a
    # manually reconstructed expected output or stale cached outline.
    cold = ToolRegistry(repo_dir=repo, drive_root=data / "cold")
    expected = cold.execute("query_code", {"op": op, "path": "picked.py", **options})
    assert "picked.py" in expected and "QUERY_CODE_ERROR" not in expected

    def forbidden(*args, **kwargs):
        pytest.fail("explicit file loaded unrelated inventory cache")

    monkeypatch.setattr(ci, "load_cached_inventory", forbidden)
    actual = registry.execute("query_code", {"op": op, "path": "picked.py", **options})
    assert actual == expected
    assert cache.read_bytes() == before and cache.stat().st_mtime_ns == before_mtime


@pytest.mark.parametrize("op", ["impact", "references", "callers"])
def test_file_selectors_that_search_root_keep_their_effective_scope(project, monkeypatch, op):
    repo, data, registry = project
    original, loaded = ci.load_cached_inventory, []

    def observed(*args, **kwargs):
        loaded.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(ci, "load_cached_inventory", observed)
    result = registry.execute("query_code", {"op": op, "path": "picked.py", "query": "target"})
    assert "other.py" in result and "QUERY_CODE_ERROR" not in result
    assert len(loaded) == (1 if op == "impact" else 0)  # occurrences use parse=False


@pytest.mark.parametrize("scope", ["", ".", "pkg"])
def test_directory_and_root_reuse_stay_intact(project, monkeypatch, scope):
    repo, data, _ = project
    (repo / "pkg").mkdir()
    (repo / "pkg" / "inside.py").write_text("def inside(): pass\n", encoding="utf-8")
    ci.build_code_inventory(repo, drive_root=data)
    cache = ci.inventory_cache_path(repo, data)
    before = cache.read_bytes()
    original, loaded = ci.load_cached_inventory, []
    monkeypatch.setattr(ci, "load_cached_inventory", lambda *a: (loaded.append(a[0]), original(*a))[1])
    monkeypatch.setattr(ci, "_file_fact", lambda *a: pytest.fail("unchanged directory/root reparsed"))
    monkeypatch.setattr(ci.CodeInventory, "to_json", lambda *a: pytest.fail("unchanged full cache serialized"))
    result = ci.build_code_inventory(repo, drive_root=data, scope=scope)
    assert loaded == [repo]
    assert len(result.files) == (1 if scope == "pkg" else 3)
    assert cache.read_bytes() == before


def test_file_freshness_name_nfkc_and_full_cache_preservation(project, monkeypatch):
    repo, data, registry = project
    cache = ci.inventory_cache_path(repo, data)
    before = cache.read_bytes()
    (repo / "picked.py").write_text("def ﬁx():\n    return helper()\n", encoding="utf-8")
    result = registry.execute("query_code", {"op": "definition", "query": "fix", "path": "picked.py"})
    assert "function fix() [outline]" in result
    absent = ci.build_code_inventory(repo, scope="picked.py", drive_root=data, name="target")
    assert absent.files[0].disposition == "name_absent"
    (repo / "picked.py").rename(repo / "renamed.py")
    assert not ci.build_code_inventory(repo, scope="picked.py", drive_root=data).files
    assert ci.build_code_inventory(repo, scope="renamed.py", drive_root=data).files[0].symbols[0].name == "fix"
    (repo / "renamed.py").unlink()
    assert not ci.build_code_inventory(repo, scope="renamed.py", drive_root=data).files
    assert cache.read_bytes() == before


@pytest.mark.parametrize("shape", ["oversized", "sensitive", "binary", "grammar", "denied", "excluded", "escape", "literal", "nested"])
def test_file_branch_uses_existing_admission_and_parser(project, monkeypatch, shape):
    repo, data, _ = project
    selected = repo / "picked.py"
    options = {}
    expected = "indexed"
    if shape == "oversized":
        monkeypatch.setattr(ci, "_MAX_INDEX_FILE_BYTES", 8)
        expected = "oversized"
    elif shape == "sensitive":
        selected = repo / "api_token.json"
        selected.write_text('{"secret": "fixture"}', encoding="utf-8")
        expected = "sensitive"
    elif shape == "binary":
        selected.write_bytes(b"\x00not source")
        expected = "binary"
    elif shape == "grammar":
        selected = repo / "picked.js"
        selected.write_text("function target(){}", encoding="utf-8")
        monkeypatch.setattr(ci, "_ts_parser", lambda _: None)
        expected = "structural_unavailable:javascript"
    elif shape == "denied":
        options["path_allowed"] = lambda _: False
    elif shape == "excluded":
        options["exclude_paths"] = [selected]
    elif shape == "escape":
        outside = repo.parent / "outside.py"
        outside.write_text("def outside(): pass", encoding="utf-8")
        selected = repo / "escape.py"
        selected.symlink_to(outside)
        expected = "path_escape"
    elif shape == "literal":
        selected = repo / "picked[1].py"
        selected.write_text("def literal(): pass", encoding="utf-8")
    elif shape == "nested":
        nested = repo / "child"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        selected = nested / "picked.py"
        selected.write_text("def nested(): pass", encoding="utf-8")
    cache = ci.inventory_cache_path(repo, data)
    before = cache.read_bytes()
    result = ci.build_code_inventory(repo, scope=selected.relative_to(repo).as_posix(), drive_root=data, **options)
    assert [f.disposition for f in result.files] == ([] if shape in {"denied", "excluded"} else [expected])
    if shape == "nested":
        assert result.nested_roots == ["child"]
    assert cache.read_bytes() == before


def test_file_query_ignores_malformed_unrelated_cache_but_root_repairs_it(project):
    repo, data, registry = project
    cache = ci.inventory_cache_path(repo, data)
    raw = json.loads(cache.read_text(encoding="utf-8"))
    raw["files"][1]["symbols"][0:0] = [{"name": "broken", "line_start": "bad"}]
    cache.write_text(json.dumps(raw), encoding="utf-8")
    before = cache.read_bytes()
    result = registry.execute("query_code", {"op": "symbols", "path": "picked.py"})
    assert "target()" in result and "QUERY_CODE_ERROR" not in result
    assert cache.read_bytes() == before
    fresh = ci.build_code_inventory(repo, drive_root=data)
    assert len(fresh.files) == 2
    assert ci.load_cached_inventory(repo, data) is not None
    assert cache.read_bytes() != before


@pytest.mark.parametrize("root", ["active_workspace", "system_repo", "user_files"])
def test_resource_roots_share_file_no_load_and_denial(project, monkeypatch, root):
    repo, data, registry = project
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.query_code import _query_code
    from ouroboros.tools import query_code as qc
    if root == "user_files":
        from pathlib import Path
        # The user home is separate from system/data, including their soft parent.
        monkeypatch.setattr(Path, "home", lambda: repo)
        monkeypatch.setenv("OUROBOROS_USER_FILES_ROOT", str(repo))
    system = repo.parent / "system" if root == "user_files" else repo
    system.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=system, system_repo_dir=system, drive_root=data)
    path = str(repo / "picked.py") if root == "user_files" else "picked.py"
    monkeypatch.setattr(ci, "load_cached_inventory", lambda *a: pytest.fail("file root loaded cache"))
    result = _query_code(ctx, "symbols", root=root, path=path)
    assert "picked.py:1 function target()" in result, result
    monkeypatch.setattr(qc, "_visible_file", lambda *a, **kw: False)
    assert "TOOL_ACCESS_BLOCKED" in _query_code(ctx, "symbols", root=root, path=path)


def test_empty_scoped_views_skip_load_but_empty_root_retains_reuse(project, monkeypatch):
    repo, data, _ = project
    original, loaded = ci.load_cached_inventory, []
    monkeypatch.setattr(ci, "load_cached_inventory", lambda *a: (loaded.append(a[0]), original(*a))[1])
    assert not ci.build_code_inventory(repo, scope="missing.py", drive_root=data).files
    assert not ci.build_code_inventory(repo, scope="picked.py", drive_root=data, path_allowed=lambda p: False).files
    assert loaded == []
    (repo / "picked.py").unlink()
    (repo / "other.py").unlink()
    ci.build_code_inventory(repo, drive_root=data)
    assert loaded == [repo]
    monkeypatch.setattr(ci.CodeInventory, "to_json", lambda *a: pytest.fail("unchanged empty cache serialized"))
    assert not ci.build_code_inventory(repo, drive_root=data).files


def test_file_grammar_availability_is_current_not_cached(project, monkeypatch):
    repo, data, registry = project
    target = repo / "missing.js"
    target.write_text("function target(){}", encoding="utf-8")
    # Deterministic missing-grammar control, regardless of installed grammars.
    with monkeypatch.context() as absent:
        absent.setattr(ci, "_ts_parser", lambda _: None)
        ci.build_code_inventory(repo, drive_root=data)
    original = ci._file_fact
    parsed = []
    def counted(root, path):
        parsed.append(path.name)
        return original(root, path)
    monkeypatch.setattr(ci, "_file_fact", counted)
    registry.execute("query_code", {"op": "symbols", "path": "missing.js"})
    assert parsed == ["missing.js"]
