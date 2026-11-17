"""Review admits executable bytes, not a new package installation."""
from types import SimpleNamespace

from ouroboros.skill_dependencies import ensure_skill_dependencies
from ouroboros.marketplace.install_specs import install_specs_hash
from tests.test_skill_enable_dependencies import make_skill, reviewed, drive, installs


def test_review_alone_cannot_install(drive, installs):
    make_skill(drive)
    status, detail = ensure_skill_dependencies(drive, reviewed(drive))
    assert status == 'authorization_required' and detail
    assert installs == []


def test_direct_owner_enable_covers_declared_install(drive, installs):
    from tests.test_skill_enable_dependencies import enable
    make_skill(drive)
    assert enable(drive, reviewed(drive))['ok']
    assert installs == [['wheel']]


def test_agent_grant_is_checked_with_actual_specs_before_install(drive, installs, monkeypatch):
    from ouroboros import safety
    make_skill(drive); skill = reviewed(drive)
    seen=[]
    def deny(name, args, **kw):
        seen.append((name,args,kw)); return False, 'covering authorization missing'
    monkeypatch.setattr(safety,'check_safety',deny)
    status,_ = ensure_skill_dependencies(drive, skill, ctx=SimpleNamespace(task_id='t'))
    assert status == 'authorization_required' and not installs
    assert seen[0][1]['install_specs'][0]['package']=='wheel'
    monkeypatch.setattr(safety,'check_safety',lambda *a,**kw:(True,''))
    assert ensure_skill_dependencies(drive,skill,ctx=SimpleNamespace(task_id='t'))[0]=='installed'
    assert installs==[['wheel']]


def test_installed_use_and_unchanged_upkeep_need_no_new_question(drive, installs, monkeypatch):
    from tests.test_skill_enable_dependencies import enable
    from ouroboros.marketplace import isolated_deps
    make_skill(drive); skill=reviewed(drive)
    assert enable(drive,skill)['ok']
    assert ensure_skill_dependencies(drive,skill)[0]=='installed'
    assert installs==[['wheel']]
    original=isolated_deps.read_deps_state
    monkeypatch.setattr(isolated_deps,'read_deps_state',lambda root,name,skill_dir=None:
        ({**original(root,name),'status':'missing'} if skill_dir else original(root,name)))
    assert ensure_skill_dependencies(drive,skill)[0]=='installed'
    assert installs==[['wheel'],['wheel']]


def test_changed_dependencies_are_not_old_upkeep(drive, installs):
    from tests.test_skill_enable_dependencies import enable
    path=make_skill(drive); skill=reviewed(drive)
    assert enable(drive,skill)['ok']
    (path/'SKILL.md').write_text((path/'SKILL.md').read_text().replace('package: wheel','package: packaging'))
    skill=reviewed(drive)
    assert ensure_skill_dependencies(drive,skill)[0]=='authorization_required'
    assert installs==[['wheel']]


def test_exact_owner_release_is_covered_but_agent_or_changed_release_is_not(drive, installs):
    from ouroboros.skill_install_record import write_install_record
    make_skill(drive)
    skill = reviewed(drive)
    for actor, digest in [('ouroboros', skill.content_hash), ('owner', 'old-release')]:
        write_install_record(drive, skill.name, {'basis': 'review', 'source': {'id': 'hub'}, 'installed_by': actor, 'release': {'digest': digest}})
        assert ensure_skill_dependencies(drive, skill)[0] == 'authorization_required'
        assert not installs
    write_install_record(drive, skill.name, {'basis': 'review', 'source': {'id': 'hub'}, 'installed_by': 'owner', 'release': {'digest': skill.content_hash}})
    assert ensure_skill_dependencies(drive, skill)[0] == 'installed'
    assert installs == [['wheel']]


def test_real_review_tail_keeps_verdict_without_install_authority(drive, installs, monkeypatch):
    from ouroboros.skill_review import SkillReviewOutcome
    from ouroboros.skill_review_runner import run_skill_review_lifecycle_blocking
    from ouroboros.skill_loader import SkillReviewState, save_review_state
    make_skill(drive)
    skill = reviewed(drive)
    def review(ctx, name):
        save_review_state(drive, name, SkillReviewState(status='clean', content_hash=skill.content_hash))
        return SkillReviewOutcome(skill_name=name, status='pass', content_hash=skill.content_hash,
                                  reviewer_models=['fake/reviewer'], findings=[], error='')
    monkeypatch.setattr('supervisor.message_bus.send_with_budget', lambda *a, **k: None)
    monkeypatch.setattr('ouroboros.skill_review_runner._reconcile_extension_payload', lambda *a, **k: {})
    ctx = SimpleNamespace(drive_root=drive, repo_dir=drive, messages=[])
    result = run_skill_review_lifecycle_blocking(ctx, 'demo', source='test', review_impl=review,
                                               repo_path=str(drive / 'skills' / 'external'))
    assert result['status'] == 'clean'
    assert result['deps_status'] == 'authorization_required'
    assert not installs


def test_failed_authorized_upkeep_retains_authority(drive, installs, monkeypatch):
    from tests.test_skill_enable_dependencies import enable
    from ouroboros.marketplace import isolated_deps
    make_skill(drive)
    skill = reviewed(drive)
    assert enable(drive, skill)['ok']
    original = isolated_deps.read_deps_state
    monkeypatch.setattr(isolated_deps, 'read_deps_state', lambda root, name, skill_dir=None:
                        ({**original(root, name), 'status': 'missing'} if skill_dir else original(root, name)))
    def fail(*a, **k):
        raise RuntimeError('transient fixture outage')
    monkeypatch.setattr(isolated_deps, '_install_python_packages', fail)
    assert ensure_skill_dependencies(drive, skill)[0] == 'failed'
    failed = original(drive, skill.name)
    assert failed['installation_authority']['basis'] == 'owner_request'
    monkeypatch.setattr(isolated_deps, '_install_python_packages', lambda packages, *a, **k: [])
    assert ensure_skill_dependencies(drive, skill)[0] == 'installed'


def test_unstamped_history_does_not_authorize_reinstallation(drive, installs, monkeypatch):
    from ouroboros.marketplace import isolated_deps
    from ouroboros.skill_dependencies import auto_install_specs_for_skill
    make_skill(drive)
    skill = reviewed(drive)
    specs_hash = install_specs_hash(auto_install_specs_for_skill(drive, skill))
    monkeypatch.setattr(isolated_deps, 'read_deps_state', lambda root, name, skill_dir=None:
                        {'status': 'missing' if skill_dir else 'installed', 'specs_hash': specs_hash})
    assert ensure_skill_dependencies(drive, skill)[0] == 'authorization_required'
    assert not installs


def test_contextual_grant_does_not_become_passive_upkeep(drive, installs, monkeypatch):
    from ouroboros.marketplace import isolated_deps
    make_skill(drive)
    skill = reviewed(drive)
    monkeypatch.setattr('ouroboros.safety.check_safety', lambda *a, **k: (True, 'advice retained'))
    monkeypatch.setattr('ouroboros.safety.get_safety_mode', lambda *a: 'light')
    ctx = SimpleNamespace(task_id='one-time', task_metadata={})
    assert ensure_skill_dependencies(drive, skill, ctx=ctx)[0] == 'installed'
    original = isolated_deps.read_deps_state
    assert original(drive, skill.name)['installation_authority']['advised']
    monkeypatch.setattr(isolated_deps, 'read_deps_state', lambda root, name, skill_dir=None:
                        ({**original(root, name), 'status': 'missing'} if skill_dir else original(root, name)))
    assert ensure_skill_dependencies(drive, skill)[0] == 'authorization_required'
    monkeypatch.setattr('ouroboros.safety.check_safety', lambda *a, **k: (False, 'owner withdrew permission'))
    assert ensure_skill_dependencies(drive, skill, ctx=ctx)[0] == 'authorization_required'
    assert installs == [['wheel']]


def test_payload_fingerprint_cannot_publish_install_authority(drive, installs, monkeypatch):
    from ouroboros.marketplace import isolated_deps
    from ouroboros.skill_dependencies import auto_install_specs_for_skill
    make_skill(drive)
    skill = reviewed(drive)
    fingerprint = {'status': 'installed', 'specs_hash': install_specs_hash(auto_install_specs_for_skill(drive, skill)),
                   'installation_authority': {'basis': 'owner_request'}}
    stored = {'status': 'failed', 'specs_hash': fingerprint['specs_hash']}
    def read(root, name, skill_dir=None):
        if skill_dir is not None:
            return fingerprint
        # Publication uses the existing atomic writer; read its actual result thereafter.
        from ouroboros.skill_loader import skill_state_dir
        import json
        p = skill_state_dir(root, name) / isolated_deps.DEPS_STATE_FILENAME
        return json.loads(p.read_text()) if p.exists() else stored
    monkeypatch.setattr(isolated_deps, 'read_deps_state', read)
    assert ensure_skill_dependencies(drive, skill)[0] == 'installed'
    assert read(drive, skill.name)['installation_authority'] == {}
    fingerprint['status'] = 'missing'
    assert ensure_skill_dependencies(drive, skill)[0] == 'authorization_required'
    assert not installs
