"""A pooled task's fresh drive reuses the canonical inventory read-only, never weaker validity."""
import json
import os
import subprocess

import pytest

from ouroboros import code_intelligence as ci, code_navigation as nav
from ouroboros.tools.registry import ToolRegistry


@pytest.fixture
def seeded(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'a.py').write_text('import os\ndef target():\n    return os.getcwd()\n', encoding='utf-8')
    (root / 'b.py').write_text('from a import target\ndef stable():\n    return target()\n', encoding='utf-8')
    canonical, child = tmp_path / 'canonical', tmp_path / 'child'
    full = ci.build_code_inventory(root, drive_root=canonical)
    seed = ci.inventory_cache_path(root, canonical)
    return root, canonical, child, full, seed, (seed.read_bytes(), seed.stat().st_mtime_ns)


def build(root, child, canonical, **kw):
    return ci.build_code_inventory(root, drive_root=child, seed_drive_root=canonical, **kw)


@pytest.mark.parametrize('relevance', [False, True])
def test_seed_is_reused_read_only_and_matches_cold_build(seeded, monkeypatch, relevance):
    root, canonical, child, full, seed, before = seeded
    cold = ci.build_code_inventory(root, persist=False)
    monkeypatch.setattr(ci, '_file_fact', lambda *a: pytest.fail('hash-valid seed reparsed'))
    inv = build(root, child, canonical, _relevance=relevance)
    rows = nav.inventory_view(inv, op='relevant_files', query='target').rows
    assert rows == nav.inventory_view(cold, op='relevant_files', query='target').rows
    assert (seed.read_bytes(), seed.stat().st_mtime_ns) == before
    # Unchanged content needs no private copy; later calls read the seed again.
    assert not ci.inventory_cache_path(root, child).exists()


def test_same_size_same_mtime_edit_reparses_and_writes_only_own_drive(seeded):
    root, canonical, child, full, seed, before = seeded
    old = (root / 'a.py').stat()
    (root / 'a.py').write_text('import os\ndef change():\n    return os.getcwd()\n', encoding='utf-8')
    os.utime(root / 'a.py', ns=(old.st_atime_ns, old.st_mtime_ns))
    inv = build(root, child, canonical, _relevance=True)
    assert [s.name for s in next(f for f in inv.files if f.path == 'a.py').symbols] == ['change']
    assert (seed.read_bytes(), seed.stat().st_mtime_ns) == before
    own = ci.load_cached_inventory(root, child)
    assert own.to_json()['files'] == ci.build_code_inventory(root, persist=False).to_json()['files']
    assert next(f for f in own.files if f.path == 'b.py').call_sites  # rich writeback, not the light view


@pytest.mark.parametrize('bad', ['{"schema_version": 5, "files": [', 'MALFORMED_RECORD'])
def test_malformed_seed_falls_back_to_a_cold_build(seeded, bad):
    root, canonical, child, full, seed, before = seeded
    if bad == 'MALFORMED_RECORD':
        data = json.loads(seed.read_text(encoding='utf-8'))
        del data['files'][0]['sha256']
        bad = json.dumps(data)
    seed.write_text(bad, encoding='utf-8')
    inv = build(root, child, canonical, _relevance=True)
    assert inv.to_json()['files'] == ci.build_code_inventory(root, persist=False, _relevance=True).to_json()['files']
    assert seed.read_text(encoding='utf-8') == bad


def test_own_cache_wins_over_the_seed(seeded, monkeypatch):
    root, canonical, child, full, seed, before = seeded
    ci.build_code_inventory(root, drive_root=child)
    loaded = []
    original = ci._load_cached_inventory
    monkeypatch.setattr(ci, '_load_cached_inventory',
                        lambda r, d, **k: loaded.append(d) or original(r, d, **k))
    build(root, child, canonical, _relevance=True)
    assert loaded == [child]


def test_filtered_and_non_persisting_views_never_write(seeded):
    root, canonical, child, full, seed, before = seeded
    (root / 'a.py').write_text('def new(): pass\n', encoding='utf-8')
    for options in ({'path_allowed': lambda p: p.name != 'b.py'}, {'exclude_paths': [root / 'b.py']},
                    {'persist': False}):
        inv = build(root, child, canonical, _relevance=True, **options)
        if 'persist' not in options:
            assert [f.path for f in inv.files] == ['a.py']
        assert not ci.inventory_cache_path(root, child).exists()
        assert (seed.read_bytes(), seed.stat().st_mtime_ns) == before


def test_query_code_seeds_pooled_drive_from_canonical_root(seeded, monkeypatch):
    root, canonical, child, full, seed, before = seeded
    registry = ToolRegistry(repo_dir=root, drive_root=child)
    monkeypatch.setattr(registry._ctx, 'budget_drive_root', str(canonical), raising=False)
    monkeypatch.setattr(ci, '_file_fact', lambda *a: pytest.fail('canonical inventory ignored'))
    assert 'a.py' in registry.execute('query_code', {'op': 'relevant_files', 'query': 'target', 'root': 'system_repo'})
    assert (seed.read_bytes(), seed.stat().st_mtime_ns) == before
    same = ToolRegistry(repo_dir=root, drive_root=canonical)
    calls = []
    original = ci.build_code_inventory
    monkeypatch.setattr(ci, 'build_code_inventory', lambda *a, **k: calls.append(k['seed_drive_root']) or original(*a, **k))
    same.execute('query_code', {'op': 'relevant_files', 'query': 'target', 'root': 'system_repo'})
    assert calls == [None]
