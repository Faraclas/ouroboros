"""Actual prompt producers through public SDK HTTP mocks, not reconstructed messages."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import config, context, safety, usage_accounting as ua
from ouroboros.context_fit import seal_task_transcript
from ouroboros.llm import LLMClient
from ouroboros.memory import Memory
from ouroboros.review_execution import ApiChatReviewExecutor, ReviewAssignment
from ouroboros.review_substrate import ReviewRequest, ReviewSlot
from tests.test_copilot_cache_boundaries import MODELS, TOOLS, cache_wire, native_markers  # noqa: F401


@pytest.fixture
def main_messages(tmp_path, monkeypatch):
    root = tmp_path / 'producer'
    repo = root / 'repo'
    files = {'prompts/SYSTEM.md': 'SYSTEM governance\n', 'BIBLE.md': 'Constitution\n',
             'VERSION': '1.2.3', 'README.md': 'README', 'pyproject.toml': 'version = "1.2.3"',
             'docs/ARCHITECTURE.md': '# Ouroboros v1.2.3',
             'docs/DEVELOPMENT.md': '# Engineering\n', 'docs/CHECKLISTS.md': 'Checklist'}
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    for name, text in {'memory/identity.md': 'Identity\n', 'memory/scratchpad.md': 'Scratch\n',
                       'state/state.json': '{"spent_usd":0}'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    (root / 'logs').mkdir()
    env = SimpleNamespace(repo_dir=repo, drive_root=root, repo_path=lambda p: repo / p,
                          drive_path=lambda p: root / p)
    monkeypatch.setenv('OUROBOROS_CANONICAL_DATA_DIR', str(root))
    monkeypatch.setattr(context, 'get_context_mode', lambda: 'max')
    resolve_route = context._context_fit_route
    monkeypatch.setattr(context, '_context_fit_route',
                        lambda task, **kw: resolve_route(task, allow_fetch=False))
    messages, _ = context.build_llm_messages(env, Memory(drive_root=root, repo_dir=repo),
                                            {'id': 'cache-producer', 'type': 'task', 'text': 'Actual task'})
    assert messages[0]['role'] == 'system' and isinstance(messages[0]['content'], list)
    assert 'SYSTEM governance' in ''.join(b.get('text', '') for b in messages[0]['content'])
    seal_task_transcript(messages, min_prefix_tokens=0)
    return messages


def assert_producer(cache_wire, original, model):
    body = cache_wire.sent[-1]['body']
    assert body['model'] == model
    blocks = original[0]['content']
    texts = [b['text'] for b in blocks]
    if model.startswith('gpt'):
        sent = body['input'][0]['content']
        assert [b['text'] for b in sent] == texts
        expected = [b['text'] for b in blocks if isinstance(b.get('cache_control'), dict) and b['text'].strip()][:3]
        assert [b['text'] for b in native_markers(body)] == expected
        assert 0 < len(expected) <= 3
        assert 'prompt_cache_options' not in body and 'prompt_cache_key' not in body
    else:
        assert body['messages'][0] == {'role': 'system', 'content': ''.join(texts)}
        assert cache_wire.sent[-1]['path'] == '/chat/completions'


@pytest.mark.parametrize('model', MODELS)
def test_actual_main_context_and_reseal(cache_wire, main_messages, model):
    before = copy.deepcopy(main_messages)
    cache_wire.invoke(main_messages, model=model, tools=TOOLS)
    assert main_messages == before
    assert_producer(cache_wire, main_messages, model)
    first = copy.deepcopy(cache_wire.sent[-1]['body'])
    main_messages.extend([{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'native_call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'native_call', 'content': 'Exact\r\nresult'}])
    seal_task_transcript(main_messages, min_prefix_tokens=0)
    sealed = copy.deepcopy(main_messages)
    cache_wire.invoke(main_messages, model=model, tools=TOOLS)
    assert main_messages == sealed
    assert_producer(cache_wire, main_messages, model)
    second = cache_wire.sent[-1]['body']
    if model.startswith('gpt'):
        assert second['input'][0] == first['input'][0]
        assert second['input'][-1] == {'type': 'function_call_output',
                                      'call_id': 'native_call', 'output': 'Exact\r\nresult'}
    else:
        assert second['messages'][0] == first['messages'][0]
        assert second['messages'][-1]['tool_call_id'] == 'native_call'


@pytest.mark.parametrize('model', MODELS)
def test_actual_api_review_executor(cache_wire, model):
    cache_wire.state['model'] = model
    slot = ReviewSlot(slot_id='fixture', model='openai-compatible::' + model, effort='high')
    request = ReviewRequest(surface='task_acceptance', goal='Offline review', task_id=cache_wire.task,
                            evidence={'fixture': True}, max_tokens=1024)
    client = LLMClient()
    executor = ApiChatReviewExecutor(ReviewAssignment(request, slot), llm=client)
    original = copy.deepcopy(executor.messages)
    try:
        with ua.usage_scope(cache_wire.scope):
            answer = executor.execute()
        assert json.loads(answer.raw_text)['status'] == 'SAFE'
    finally:
        for sdk in client._remote_clients.values():
            sdk.close()
    assert len(cache_wire.sent) == 1 and executor.messages == original
    assert_producer(cache_wire, original, model)


@pytest.mark.parametrize('model', MODELS)
def test_actual_safety_producer(cache_wire, model):
    cache_wire.state['model'] = model
    client = LLMClient()
    ctx = SimpleNamespace(drive_root=cache_wire.root, task_id=cache_wire.task,
                          runtime_mode='pro')
    try:
        with ua.usage_scope(cache_wire.scope):
            message, usage = safety._safety_model_call(client=client, ctx=ctx, tool_name='read_file',
                light_model='openai-compatible::' + model, use_local=False, call_type='offline_safety_fixture',
                user_prompt='Read a fixture', on_usage=lambda usage: None, attempts_limit=1,
                account='', processing='')
    finally:
        for sdk in client._remote_clients.values():
            sdk.close()
    assert json.loads(message['content'])['status'] == 'SAFE'
    assert len(cache_wire.sent) == 1 and len(usage['ledger_attempt_ids']) == 1
    body = cache_wire.sent[0]['body']
    assert body['model'] == model  # Luna is not substituted with Sol.
    if model.startswith('gpt'):
        assert native_markers(body) == [{'type': 'input_text', 'text': safety._get_safety_prompt(),
                                        'prompt_cache_breakpoint': {'mode': 'explicit'}}]
        assert body['input'][1] == {'role': 'user', 'content': 'Read a fixture'}
    else:
        assert body['messages'][0] == {'role': 'system', 'content': safety._get_safety_prompt()}
        assert usage['wire_layout']['copilot_system_string']['cache_markers_omitted'] == 1


def test_native_review_user_assignment_remains_unrepresented(cache_wire):
    from ouroboros.review_native_episode import native_first_send_messages
    messages = native_first_send_messages('Actual native assignment')
    before = copy.deepcopy(messages)
    cache_wire.invoke(messages)
    assert messages == before and not native_markers(cache_wire.sent[0]['body'])
    assert cache_wire.sent[0]['body']['input'][1] == {'role': 'user', 'content': 'Actual native assignment'}


def test_fixture_rejects_producer_model_substitution(cache_wire):
    cache_wire.state['model'] = 'gpt-6-luna'
    with pytest.raises(AssertionError, match='producer substituted another model'):
        LLMClient()._resolve_remote_target('openai-compatible::gpt-6.1-sol')
    assert cache_wire.sent == []
