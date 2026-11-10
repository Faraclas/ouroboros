"""Current-byte author judgment unblocks review without inventing a clean baseline."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from ouroboros import body_candidate
from ouroboros.artifacts import task_artifact_dir_path, read_actor_source_bytes
from ouroboros.mutation_attribution import capture_mutation_baseline, record_terminal_mutation_candidates
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools import git as git_tools
from ouroboros.tools.registry import ToolRegistry
from tests.body_candidate_support import git, isolate, make_ctx, make_serving


@pytest.fixture
def held(tmp_path, monkeypatch):
    serving = make_serving(tmp_path)
    data = isolate(monkeypatch, tmp_path)
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'pro')
    ctx = make_ctx(serving, data, 'author')
    body_candidate.prepare(ctx)  # Legacy/manual direct context: deliberately no baseline yet.
    repo = Path(ctx.repo_dir)
    (repo / 'ouroboros/mod_a.py').write_text("GEN = 'OWN'\n", encoding='utf-8')
    (repo / 'ouroboros/mod_b.py').write_text("GEN = 'FOREIGN'\n", encoding='utf-8')
    write_task_result(data, 'author', 'running')
    capture_mutation_baseline(data, 'author', [{'surface_type':'system_repo', 'host_root':str(repo)}])
    before = copy.deepcopy(load_task_result(data, 'author')['mutation_evidence']['baseline'])
    (repo / 'ouroboros/mod_a.py').write_text("GEN = 'OWN_UPDATED'\n", encoding='utf-8')
    artifact = task_artifact_dir_path(data, 'author', create=True) / 'proof.txt'
    artifact.write_text('Retained host write arguments and reconstruction; historical preimage unavailable.\n', encoding='utf-8')
    decision = {'baseline_hash': before['baseline_hash'], 'paths':['ouroboros/mod_a.py'],
                'reason':'I authored these current bytes; this does not assert past cleanliness.',
                'evidence':[{'path':str(artifact), 'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    registry = ToolRegistry(repo_dir=serving, drive_root=data)
    registry.set_context(ctx)
    return ctx, data, before, decision, registry


def test_guarded_authorship_preserves_baseline_foreign_dirt_and_retained_evidence(held):
    ctx, data, before, decision, registry = held
    assert 'preexisting_dirty_changed' in git_tools._task_attributed_commit_paths(ctx, None)[2]
    result = registry.execute_result('prepare_self_change', {'authorship':decision})
    assert result.status == 'ok', result.text
    selected, evidence, error, _ = git_tools._task_attributed_commit_paths(ctx, None)
    assert error == '' and selected == ['ouroboros/mod_a.py'], evidence
    assert evidence['excluded_preexisting_dirty'] == ['ouroboros/mod_b.py']
    assert evidence['authorship_decisions'][0]['basis'] == 'author_judgment_not_historical_cleanliness'
    stored = load_task_result(data, 'author')['mutation_evidence']
    assert stored['baseline'] == before
    ref = stored['authorship_decisions'][0]['evidence_refs'][0]
    assert read_actor_source_bytes(data, 'author', ref).startswith(b'Retained host')
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    assert len(load_task_result(data, 'author')['mutation_evidence']['authorship_decisions']) == 1
    assert git(ctx.repo_dir, 'diff', '--cached', '--name-only') == ''
    terminal = record_terminal_mutation_candidates(data, 'author')
    assert terminal['terminal_candidate_snapshot']['surfaces'][0]['candidates'] == ['ouroboros/mod_a.py']


def test_drift_needs_another_decision_and_changed_foreign_content_still_blocks(held):
    ctx, data, before, decision, registry = held
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    own = Path(ctx.repo_dir) / 'ouroboros/mod_a.py'
    own.write_text("GEN = 'NEW'\n", encoding='utf-8')
    assert 'preexisting_dirty_changed' in git_tools._task_attributed_commit_paths(ctx, None)[2]
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    assert len(load_task_result(data, 'author')['mutation_evidence']['authorship_decisions']) == 2
    foreign = Path(ctx.repo_dir) / 'ouroboros/mod_b.py'
    foreign.write_text("GEN = 'MIXED'\n", encoding='utf-8')
    assert 'preexisting_dirty_changed' in git_tools._task_attributed_commit_paths(ctx, None)[2]
    assert load_task_result(data, 'author')['mutation_evidence']['baseline'] == before


@pytest.mark.parametrize('bad', ['hash', 'evidence', 'missing', 'outside', 'reason', 'directory'])
def test_invalid_authorship_changes_no_baseline_or_decision(held, bad):
    ctx, data, before, decision, registry = held
    if bad == 'hash':
        decision['baseline_hash'] = '0' * 64
    elif bad == 'evidence':
        decision['evidence'][0]['sha256'] = '0' * 64
    elif bad == 'missing':
        Path(decision['evidence'][0]['path']).unlink()
    elif bad == 'outside':
        decision['paths'] = ['../owner.txt']
    elif bad == 'reason':
        decision['reason'] = ''
    else:
        own = Path(ctx.repo_dir) / 'ouroboros/mod_a.py'
        own.unlink()
        own.mkdir()
    result = registry.execute_result('prepare_self_change', {'authorship':decision})
    assert result.status == 'blocked', result.text
    stored = load_task_result(data, 'author')['mutation_evidence']
    assert stored['baseline'] == before and not stored.get('authorship_decisions')


def test_modes_and_partial_commit_epochs_do_not_reclaim_foreign_leftovers(held):
    from ouroboros.mutation_attribution import advance_mutation_baseline

    ctx, data, before, decision, registry = held
    own = Path(ctx.repo_dir) / 'ouroboros/mod_a.py'
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    own.chmod(0o755)
    if git(ctx.repo_dir, 'config', '--get', 'core.filemode') != 'true':
        pytest.skip('Git does not observe executable bits on this filesystem')
    assert 'preexisting_dirty_changed' in git_tools._task_attributed_commit_paths(ctx, None)[2]
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    # Committing a different own path leaves claimed WIP attributable in the new epoch.
    (Path(ctx.repo_dir) / 'another.txt').write_text('own\n', encoding='utf-8')
    git(ctx.repo_dir, 'add', 'another.txt')
    git(ctx.repo_dir, 'commit', '-qm', 'own partial')
    advanced = advance_mutation_baseline(data, 'author', ctx.repo_dir)
    assert advanced['baseline']['surfaces'][0]['git']['dirty_paths'] == ['ouroboros/mod_a.py', 'ouroboros/mod_b.py']
    assert git_tools._task_attributed_commit_paths(ctx, None)[0] == ['ouroboros/mod_a.py']
    assert advanced['authorship_decisions'][-1]['carried_from'] == before['baseline_hash']
    assert read_actor_source_bytes(data, 'author', advanced['authorship_decisions'][0]['baseline_source'])
    own.write_text("GEN = 'DRIFT'\n", encoding='utf-8')
    assert 'preexisting_dirty_changed' in git_tools._task_attributed_commit_paths(ctx, None)[2]


def test_commit_review_receives_the_recorded_judgment_not_a_clean_baseline(held, monkeypatch):
    ctx, data, before, decision, registry = held
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    seen = []

    def review(*args, **kwargs):
        from ouroboros.review_evidence import capture_commit_review_evidence
        packet = capture_commit_review_evidence(ctx)
        text = read_actor_source_bytes(data, 'author', packet['source_ref']).decode('utf-8')
        seen.append((kwargs['scope'], text))
        return {'status':'blocked', 'message':'inspection only'}

    monkeypatch.setattr(git_tools, '_run_reviewed_stage_cycle', review)
    registry.execute('commit_reviewed', {'commit_message':'inspect evidence', 'skip_tests':True, 'scope':'author scope'})
    assert len(seen) == 1, seen
    assert seen[0][0] == 'author scope'
    assert 'author_judgment_not_historical_cleanliness' in seen[0][1]
    assert before['baseline_hash'] in seen[0][1] and 'historical preimage unavailable' in seen[0][1]


@pytest.mark.parametrize('kind', ['missing', 'symlink'])
def test_exact_deletion_and_symlink_are_supported(held, kind):
    ctx, data, before, decision, registry = held
    own = Path(ctx.repo_dir) / 'ouroboros/mod_a.py'
    own.unlink()
    if kind == 'symlink':
        try:
            own.symlink_to('mod_b.py')
        except OSError:
            pytest.skip('symlink creation unavailable')
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    selected, _, error, _ = git_tools._task_attributed_commit_paths(ctx, None)
    assert error == '' and selected == ['ouroboros/mod_a.py']
    assert load_task_result(data, 'author')['mutation_evidence']['baseline'] == before


def test_terminal_projection_and_successor_keep_the_author_judgment(held):
    from ouroboros.mutation_attribution import mutation_evidence_projection

    ctx, data, before, decision, registry = held
    assert registry.execute_result('prepare_self_change', {'authorship':decision}).status == 'ok'
    terminal = record_terminal_mutation_candidates(data, 'author')
    assert mutation_evidence_projection(terminal)['authored_path_count'] == 1
    assert terminal['terminal_candidate_snapshot']['surfaces'][0]['authorship_decisions']
    write_task_result(data, 'author', 'completed', mutation_evidence=terminal)
    write_task_result(data, 'successor', 'running')
    capture_mutation_baseline(data, 'successor', [{'surface_type':'system_repo', 'host_root':str(ctx.repo_dir)}],
                              predecessor_source={'task_id':'author'})
    from ouroboros.mutation_attribution import attributed_git_candidates
    adopted = attributed_git_candidates(data, 'successor', ctx.repo_dir)
    assert adopted['candidates'] == ['ouroboros/mod_a.py']
    assert adopted['predecessor_adoption']['authorship_decisions'][0]['basis'] == 'author_judgment_not_historical_cleanliness'


def test_in_place_cannot_silently_drop_authorship(held):
    ctx, data, before, decision, registry = held
    result = registry.execute_result('prepare_self_change', {'in_place':True, 'authorship':decision})
    assert result.status == 'error' and result.code == 'TOOL_ARG_ERROR'
    assert load_task_result(data, 'author')['mutation_evidence']['baseline'] == before
