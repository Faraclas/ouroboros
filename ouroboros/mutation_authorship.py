"""Explicit current-byte authorship decisions, separate from historical baselines.

A late first observation cannot prove who authored its dirt. The acting root may
attribute specific current bytes with retained evidence, for review to weigh;
this never rewrites that observation or claims a historically clean checkout.
"""
from __future__ import annotations

from ouroboros.task_results import load_task_result, STATUS_RUNNING
from ouroboros.task_store import patch_document
from ouroboros.utils import safe_relpath, utc_now_iso


def matching_authorship(evidence, baseline, root, fingerprints):
    """Only exact current bytes/modes of this baseline's named surface qualify."""
    owned = set()
    for row in evidence.get('authorship_decisions') or []:
        if (row.get('baseline_hash') == baseline.get('baseline_hash')
                and row.get('canonical_root') == str(root)):
            owned.update(path for path, fp in row.get('fingerprints', {}).items()
                         if fp == fingerprints.get(path) and exact_fingerprint(fp))
    return owned


def exact_fingerprint(fp):
    return (fp.get('kind') == 'missing' or fp.get('kind') == 'symlink' and 'target' in fp
            or fp.get('kind') == 'file' and fp.get('sha256') and fp.get('git_mode'))


def carry_authorship(evidence, old_hash, baseline):
    """Keep byte-bound judgment in a new epoch, never turn it into clean history."""
    carried = []
    surfaces = {row['canonical_root']: row.get('git', {}) for row in baseline['surfaces']}
    for row in evidence.get('authorship_decisions') or []:
        if row.get('baseline_hash') != old_hash:
            continue
        git = surfaces.get(row['canonical_root'], {})
        remaining = {path: fp for path, fp in row['fingerprints'].items()
                     if path in git.get('dirty_paths', []) and fp == git.get('dirty_fingerprints', {}).get(path)}
        if remaining:
            carried.append({**row, 'baseline_hash': baseline['baseline_hash'], 'fingerprints': remaining,
                            'carried_from': old_hash})
    return [*evidence.get('authorship_decisions', []), *carried]


def record_authorship(results_root, task_id, repo, *, baseline_hash, paths, reason, evidence_refs):
    """Record the author's decision, not a proof or a grant over foreign content.

    Original baseline and prior decisions stay intact. Current fingerprints
    make a subsequent edit require a new decision instead of blanket adoption.
    Caller admits and retains the evidence files before entering this owner.
    """
    from ouroboros.mutation_attribution import _canonical_root, _git_path_fingerprints, _run_git

    current = load_task_result(results_root, task_id, strict=True) or {}
    evidence = current.get('mutation_evidence') or {}
    baseline = evidence.get('baseline') or {}
    if (not baseline_hash or baseline.get('baseline_hash') != baseline_hash
            or baseline.get('owner_id') != task_id or baseline.get('owner_kind') != 'task_root'
            or current.get('status') != STATUS_RUNNING):
        raise ValueError('current root baseline identity is required')
    root = _canonical_root(repo)
    surfaces = [row for row in baseline.get('surfaces') or [] if row.get('canonical_root') == str(root)]
    if len(surfaces) != 1 or not isinstance(surfaces[0].get('git'), dict):
        raise ValueError('one Git baseline surface is required')
    git = surfaces[0]['git']
    selected = sorted(set(safe_relpath(path) for path in paths))
    if (not selected or not set(selected) <= set(git.get('dirty_paths') or [])
            or git.get('dirty_overflow') or _run_git(root, 'rev-parse', 'HEAD').strip() != git.get('base_commit')):
        raise ValueError('select exact preexisting paths on the unchanged baseline commit')
    fingerprints = _git_path_fingerprints(root, selected)
    if not reason.strip() or not evidence_refs or not all(exact_fingerprint(fp) for fp in fingerprints.values()):
        raise ValueError('exact path fingerprints, rationale and retained evidence are required')
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.utils import json_dumps
    source = store_actor_source_bytes(results_root, task_id, category='tool_results', source_id='historical-baseline',
                                     data=json_dumps(baseline, ensure_ascii=False).encode('utf-8'), extension='json')
    decision = {'task_id': task_id, 'baseline_hash': baseline_hash, 'canonical_root': str(root),
                'fingerprints': fingerprints, 'reason': reason, 'evidence_refs': evidence_refs, 'baseline_source': source,
                'basis': 'author_judgment_not_historical_cleanliness'}
    outcome = {}

    def commit(stored):
        observed = stored.get('mutation_evidence') or {}
        if stored.get('status') != STATUS_RUNNING or observed.get('baseline') != baseline:
            raise ValueError('task or baseline changed before authorship publication')
        prior = observed.get('authorship_decisions') or []
        same = next((row for row in prior if {k: v for k, v in row.items() if k != 'at'} == decision), None)
        outcome['decision'] = same or {**decision, 'at': utc_now_iso()}
        if same:
            return None
        return {**stored, 'mutation_evidence': {**observed,
                'authorship_decisions': [*prior, outcome['decision']]}}

    patch_document(results_root, task_id, commit)
    return outcome['decision']
