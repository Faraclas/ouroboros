"""Relevance defers objects, never cache validity or rich writeback."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
import subprocess

import pytest

from ouroboros import code_intelligence as ci, code_navigation as nav
from ouroboros.tools.registry import ToolRegistry


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'a.py').write_text('import os\ndef target():\n    return os.getcwd()\n', encoding='utf-8')
    (root / 'b.py').write_text('from a import target\ndef stable():\n    return target()\n', encoding='utf-8')
    data = tmp_path / 'data'
    full = ci.build_code_inventory(root, drive_root=data)
    return root, data, full


def light(root, data, **kw):
    return ci.build_code_inventory(root, drive_root=data, _relevance=True, **kw)


def test_warm_relevance_defers_objects_without_writing(project, monkeypatch):
    root, data, full = project
    (root / '.env').write_text('PRIVATE=fixture', encoding='utf-8')
    (root / 'large.py').write_bytes(b'x' * (ci._MAX_INDEX_FILE_BYTES + 1))
    ci.build_code_inventory(root, drive_root=data)
    cache = ci.inventory_cache_path(root, data)
    before = cache.read_bytes(), cache.stat().st_mtime_ns
    import ouroboros.maintenance_retirement as retirement
    monkeypatch.setattr(retirement, 'artifact_changed', lambda *a: pytest.fail('unchanged cache rewritten'))
    inv = light(root, data)
    picked = next(f for f in inv.files if f.path == 'a.py')
    assert picked.call_sites is None and picked.import_facts is None
    assert nav.inventory_view(inv, op='relevant_files', query='target').rows
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
    assert next(f for f in ci.load_cached_inventory(root, data).files if f.path == 'a.py').call_sites


@pytest.mark.parametrize('query_args', [
    {'op': 'relevant_files', 'query': 'target stable'},
    {'op': 'relevant_files', 'query': 'target', 'offset': 1, 'limit': 1, 'lang': 'python'},
    {'op': 'relevant_files', 'query': 'target', 'path': 'a.py'},
])
def test_public_reply_matches_full_and_rich_query_survives(project, monkeypatch, query_args):
    root, data, full = project
    registry = ToolRegistry(repo_dir=root, drive_root=data)
    original = ci.build_code_inventory
    with monkeypatch.context() as eager:
        eager.setattr(ci, 'build_code_inventory', lambda *a, **k: original(*a, **{**k, '_relevance': False}))
        expected = registry.execute('query_code', query_args)
    assert registry.execute('query_code', query_args) == expected
    assert 'Calls:' in registry.execute('query_code', {'op': 'digest'})
    assert 'hash-validated' in registry.execute('query_code', {'op': 'impact', 'query': 'a.py'})


def test_changed_full_and_partial_refresh_preserve_rich_siblings(project, monkeypatch):
    root, data, full = project
    old = (root / 'a.py').stat()
    (root / 'a.py').write_text('import os\ndef changed():\n    return os.getcwd()\n', encoding='utf-8')
    os.utime(root / 'a.py', ns=(old.st_atime_ns, old.st_mtime_ns))
    with monkeypatch.context() as limited:
        limited.setattr(ci, '_MAX_ENUMERATION_PATHS', 1)
        partial = light(root, data)
    assert partial.enumeration_limits
    stored = ci.load_cached_inventory(root, data)
    b = next(f for f in stored.files if f.path == 'b.py')
    assert b == next(f for f in full.files if f.path == 'b.py')
    current = light(root, data)
    assert current.files[0].symbols[0].name == 'changed'
    assert ci.load_cached_inventory(root, data).to_json()['files'] == ci.build_code_inventory(root, persist=False).to_json()['files']
    assert 'Calls:' in nav.inventory_view(ci.load_cached_inventory(root, data), op='digest').rows[1]


def test_filtered_refresh_never_persists(project, monkeypatch):
    root, data, full = project
    cache = ci.inventory_cache_path(root, data)
    before = cache.read_bytes(), cache.stat().st_mtime_ns
    (root / 'a.py').write_text('def new(): pass\n', encoding='utf-8')
    for options in ({'scope': 'a.py'}, {'path_allowed': lambda p: p.name != 'b.py'},
                    {'exclude_paths': [root / 'b.py']}, {'name': 'new'}, {'parse': False}):
        light(root, data, **options)
        assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


VALUES = [None, 0, 1, -2, 1.5, float('nan'), float('inf'), True, False,
          '', '12', 'bad', [], [1], {}, {'x': 1}]


@pytest.mark.parametrize('field', ['call_sites', 'import_facts', 'exports', 'symbols'])
@pytest.mark.parametrize('value', VALUES)
def test_whole_cache_validity_matches_eager(project, field, value):
    root, data, full = project
    raw = full.to_json()
    raw['files'][0][field] = value
    cache = ci.inventory_cache_path(root, data)
    cache.write_text(json.dumps(raw), encoding='utf-8')
    def outcome(deferred):
        try:
            if deferred:
                inv, sources = ci._load_cached_inventory(root, data, _relevance=True)
            else:
                inv = ci.load_cached_inventory(root, data)
            return 'valid' if inv is not None else 'invalid'
        except Exception as exc:
            return type(exc).__name__
    assert outcome(True) == outcome(False)


@pytest.mark.parametrize('kind', ['call_sites', 'import_facts'])
@pytest.mark.parametrize('value', VALUES)
def test_omitted_item_validity_matches_eager(project, kind, value):
    root, data, full = project
    raw = full.to_json()
    row = raw['files'][0][kind][0]
    for key in list(row):
        mutated = deepcopy(raw)
        mutated['files'][0][kind][0][key] = value
        cache = ci.inventory_cache_path(root, data)
        cache.write_text(json.dumps(mutated), encoding='utf-8')
        expected = ci.load_cached_inventory(root, data)
        actual, sources = ci._load_cached_inventory(root, data, _relevance=True)
        assert (actual is None) == (expected is None), (kind, key, value)
        if actual is not None:
            # Separately decoded NaNs need canonical-value comparison, not NaN == NaN.
            upgraded = ci._file_from_json(sources[id(actual.files[0])])
            assert json.dumps(asdict(upgraded), sort_keys=True) == json.dumps(asdict(expected.files[0]), sort_keys=True)


@pytest.mark.parametrize('item', [{}, {'unexpected': 'broken'}, {'specifier': 'x', 'line': 1, 'column': 2, 'slot': 's', 'extra': 3}, None, 1, 'x'])
def test_malformed_unused_import_invalidates_before_query(project, item):
    root, data, full = project
    raw = full.to_json()
    raw['files'][1]['import_facts'] = [item]
    cache = ci.inventory_cache_path(root, data)
    cache.write_text(json.dumps(raw), encoding='utf-8')
    assert ci.load_cached_inventory(root, data) is None
    inv, _ = ci._load_cached_inventory(root, data, _relevance=True)
    assert inv is None
    light(root, data)
    assert ci.load_cached_inventory(root, data).files == full.files


def test_duplicate_paths_bounded_tail_keeps_each_original_record(project, monkeypatch):
    root, data, full = project
    raw = full.to_json()
    duplicate = deepcopy(raw['files'][1])
    duplicate['call_sites'][0]['name'] = 'distinct_tail'
    raw['files'].insert(0, duplicate)
    cache = ci.inventory_cache_path(root, data)
    def run(deferred):
        cache.write_text(json.dumps(raw), encoding='utf-8')
        with monkeypatch.context() as limited:
            limited.setattr(ci, '_MAX_ENUMERATION_PATHS', 1)
            ci.build_code_inventory(root, drive_root=data, _relevance=deferred)
        return ci.load_cached_inventory(root, data).to_json()['files']
    assert run(True) == run(False)
