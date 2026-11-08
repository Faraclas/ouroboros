from unittest.mock import Mock
from types import SimpleNamespace
import json
import pytest
from tests.test_project_attention import runtime, ordinary, lap, episode
from ouroboros import project_attention as attention
from ouroboros.project_admission import ProjectAdmissionError


def test_legacy_routing_defaults_keep_attention_available(runtime, monkeypatch):
    from ouroboros import agent
    from ouroboros.projects_registry import _registry_path
    from tests.test_consciousness_wake_lane import _wait_for
    root, project, duty, queue, registry = runtime
    path = _registry_path(root)
    data = json.loads(path.read_text(encoding='utf-8'))
    for row in data['projects']:
        if row['id'] == project['id']:
            row.pop('routing_generation', None)
            row.pop('working_dir', None)
    path.write_text(json.dumps(data), encoding='utf-8')
    seen = []
    class Actor:
        def handle_task(self, task):
            seen.append(task)
            return []
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: Actor())
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert duty.last_result[project['id']]['admitted'], duty.last_result
    assert _wait_for(lambda: len(seen) == 1 and not registry.actors())


@pytest.mark.parametrize('failure', [
    OSError('registry read unavailable'), ValueError('malformed routing'),
    ProjectAdmissionError('project_routing_fence_changed', 'changed during preparation'),
])
def test_guard_read_failure_keeps_episode_and_uses_existing_retry_floor(runtime, monkeypatch, failure):
    from ouroboros import project_admission, agent
    root, project, duty, queue, registry = runtime
    def unavailable(*a, **kw):
        raise failure
    monkeypatch.setattr(project_admission, 'project_admission_guard', unavailable)
    seen = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not seen.called and not registry.actors()
    assert episode(root, project['id'])['pending']
    assert duty.last_result[project['id']]['reason'] == 'admission_failed'
    assert duty._retry_at > 0
    lap(duty)  # absorb our own admission-fold revision before unchanged repeats
    retry_at = duty._retry_at
    for _ in range(4):
        lap(duty)
    assert duty._retry_at == retry_at
    assert not seen.called and not registry.actors()
