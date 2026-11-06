"""Stable system boundaries through real SDK HTTP mocks for both model families.

No network, provider acceptance, retention or performance is established here.
"""
import asyncio
import copy
import json
from pathlib import Path
import socket
import uuid
from types import SimpleNamespace

import openai
import pytest

from ouroboros import config, copilot_responses as codec, net_transport as nt, pricing
from ouroboros import request_wire_contract as wire, request_wire_recovery as recovery
from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from tests._usage_store_testing import ledger_rows

MODELS = ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6.1-terra', 'claude-opus-5.5', 'claude-sonnet-5.5']
TOOLS = [{'type': 'function', 'function': {'name': 'lookup', 'description': 'Lookup',
          'parameters': {'type': 'object', 'properties': {}}}}]


def marked(text):
    return {'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}


def native_markers(body):
    return [b for m in body.get('input', []) for b in
            (m.get('content') if isinstance(m.get('content'), list) else [])
            if 'prompt_cache_breakpoint' in b]


@pytest.fixture
def cache_wire(monkeypatch, tmp_path):
    """Keep constructors/accounting/codec real; replace final HTTP and route lookup."""
    hm = nt._openai_httpx_module()
    sent, state = [], {'model': MODELS[0], 'status': 200, 'error': None}
    target = {'provider': 'openai-compatible', 'resolved_model': MODELS[0],
              'usage_model': 'openai-compatible/' + MODELS[0], 'api_key': 'fixture',
              'base_url': 'https://api.business.githubcopilot.com',
              'supports_openrouter_extensions': False}
    def no_network(*a, **k):
        pytest.fail('real socket forbidden in cache fixture')
    def respond(self, request):
        body = json.loads(request.read())
        sent.append({'path': request.url.path, 'body': body})
        if state.get('timeout'):
            raise hm.ReadTimeout('offline interrupted response', request=request)
        if state['error'] and (not callable(state['error']) or state['error'](body)):
            return hm.Response(state['status'], json={'error': state.get('error_body', {
                'message': 'Unsupported parameter: input[0].content[0].prompt_cache_breakpoint',
                'type': 'invalid_request_error', 'param': 'input[0].content[0].prompt_cache_breakpoint'})})
        text = '{"status":"SAFE","reason":"offline fixture"}'
        if request.url.path.endswith('/responses'):
            return hm.Response(200, json={'id': 'cache-response', 'model': state['model'],
                'status': 'in_progress' if state.get('pending') else 'completed',
                'output': [{'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': text}]}],
                'usage': {'input_tokens': 20, 'output_tokens': 4}})
        reply = {'id': 'cache-chat', 'model': state['model'], 'choices': [{'index': 0,
                 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}],
                 'usage': {'prompt_tokens': 20, 'completion_tokens': 4}}
        if body.get('stream'):
            chunks = [{'id': 'cache-chat', 'model': state['model'], 'choices': [{'index': 0,
                       'delta': {'content': text}, 'finish_reason': None}]},
                      {'id': 'cache-chat', 'model': state['model'], 'choices': [{'index': 0,
                       'delta': {}, 'finish_reason': 'stop'}], 'usage': reply['usage']}]
            return hm.Response(200, content=(''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
                               + 'data: [DONE]\n\n').encode(), headers={'content-type': 'text/event-stream'})
        return hm.Response(200, json=reply)
    async def arespond(self, request):
        return respond(self, request)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_network)
    def resolve(self, model):
        from ouroboros.provider_models import parse_model_route
        actual = parse_model_route(model).model
        assert actual == state['model'], 'producer substituted another model'
        return {**target, 'resolved_model': actual, 'usage_model': 'openai-compatible/' + actual}
    monkeypatch.setattr(LLMClient, '_resolve_remote_target', resolve)
    monkeypatch.setattr(ua, '_reservation_cost', lambda request: 0.)
    monkeypatch.setattr(pricing, 'estimate_cost_optional', lambda *a, **k: None)
    monkeypatch.setattr(wire, 'canonical_wire_evidence_root', lambda: tmp_path)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', respond)
    monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', arespond)
    root = Path(config.DATA_DIR)
    task = 'cache-boundary-' + uuid.uuid4().hex
    scope = ua.UsageScope(drive_root=root, task_id=task, root_task_id='offline-cache',
                         category='verification', source='offline_mock_fixture', non_task_operation=True)
    def invoke(messages, *, model=MODELS[0], asynchronous=False, stream=False, no_proxy=False, tools=None):
        state['model'] = model
        client = LLMClient()
        kwargs = dict(messages=messages, model='openai-compatible::' + model,
                      reasoning_effort='high', max_tokens=1024, wait_for_resources=False,
                      tools=tools, stream=stream, no_proxy=no_proxy)
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            if asynchronous:
                async def run():
                    try:
                        return await client.chat_async(**kwargs)
                    finally:
                        for sdk in client._async_remote_clients.values():
                            await sdk.close()
                return asyncio.run(run())
            try:
                return client.chat(**kwargs)
            finally:
                for sdk in client._remote_clients.values():
                    sdk.close()
    return SimpleNamespace(invoke=invoke, sent=sent, state=state, root=root, task=task, scope=scope,
                           wire_root=tmp_path)


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('no_proxy', [False, True])
def test_system_boundaries_and_family_routing(cache_wire, model, asynchronous, stream, no_proxy):
    messages = [{'role': 'system', 'content': [marked('GOV\n'), marked('IDENTITY\n'),
                 {'type': 'text', 'text': 'runtime'}]}, {'role': 'user', 'content': 'Question'}]
    before = copy.deepcopy(messages)
    msg, usage = cache_wire.invoke(messages, model=model, asynchronous=asynchronous,
                                  stream=stream, no_proxy=no_proxy, tools=TOOLS)
    assert messages == before and json.loads(msg['content'])['status'] == 'SAFE'
    assert len(cache_wire.sent) == 1 and len(usage['ledger_attempt_ids']) == 1
    body = cache_wire.sent[0]['body']
    if model.startswith('gpt'):
        assert cache_wire.sent[0]['path'] == '/responses'
        assert body['input'][0]['content'] == [
            {'type': 'input_text', 'text': 'GOV\n', 'prompt_cache_breakpoint': {'mode': 'explicit'}},
            {'type': 'input_text', 'text': 'IDENTITY\n', 'prompt_cache_breakpoint': {'mode': 'explicit'}},
            {'type': 'input_text', 'text': 'runtime'}]
        assert 'prompt_cache_key' not in body and 'prompt_cache_options' not in body
        assert usage['prompt_cache_ttl_evidence']['http'] == {
            'breakpoints': 2, 'key_present': False, 'options_present': False}
        assert all('cache_control' not in tool for tool in body['tools'])
    else:
        assert cache_wire.sent[0]['path'] == '/chat/completions'
        assert [b['text'] for b in body['messages'][0]['content']] == ['GOV\n', 'IDENTITY\n', 'runtime']
        assert all('cache_control' in b for b in body['messages'][0]['content'][:2])
        assert 'cache_control' not in body['messages'][0]['content'][2]
        assert body['tools'][-1]['cache_control']['type'] == 'ephemeral'
        assert 'input' not in body and usage['prompt_cache_ttl_evidence']['http'] is None


@pytest.mark.parametrize('ttl', ['default', '5m', '1h'])
def test_native_projection_does_not_map_lifetimes(cache_wire, monkeypatch, ttl):
    monkeypatch.setattr(config, 'resolve_prompt_cache_ttl', lambda: ttl)
    _, usage = cache_wire.invoke([{'role': 'system', 'content': [marked('stable'),
                                 {'type': 'text', 'text': 'changing'}]}])
    body = cache_wire.sent[0]['body']
    assert native_markers(body) == [{'type': 'input_text', 'text': 'stable',
                                    'prompt_cache_breakpoint': {'mode': 'explicit'}}]
    assert 'ttl' not in json.dumps(body) and 'retention' not in json.dumps(body)
    assert usage['prompt_cache_ttl'] == ('1h' if ttl == 'default' else ttl)


def test_only_system_text_boundaries_and_original_semantics_are_projected():
    payload = {'model': MODELS[0], 'prompt_cache_key': 'not-part-of-this-increment',
               'messages': [{'role': 'system', 'content': [marked('a'), marked(' '),
                            {'type': 'text', 'text': 'b'}, {'type': 'image_url', 'image_url': {
                             'url': 'https://fixture.invalid/image.png', 'detail': 'high'}}]},
                            {'role': 'user', 'content': [marked('user')]},
                            {'role': 'assistant', 'content': 'a', 'tool_calls': [
                             {'id': 'call_1', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
                            {'role': 'tool', 'tool_call_id': 'call_1', 'content': [marked('x\r\ny')]}]}
    before = copy.deepcopy(payload)
    body = codec._copilot_chat_to_responses_payload(payload)
    assert payload == before
    assert native_markers(body) == [{'type': 'input_text', 'text': 'a',
                                    'prompt_cache_breakpoint': {'mode': 'explicit'}}]
    assert body['input'][0]['content'][-1] == {'type': 'input_image',
            'image_url': 'https://fixture.invalid/image.png', 'detail': 'high'}
    assert body['input'][1] == {'role': 'user', 'content': 'user'}
    assert body['input'][-1] == {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'x\r\ny'}
    assert body['input'][-2]['call_id'] == 'call_1'
    assert 'prompt_cache_key' not in body


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('status', [400, 422])
def test_native_marker_refusal_uses_bounded_marker_free_retry(cache_wire, asynchronous, status):
    cache_wire.state.update(status=status, error=lambda body: bool(native_markers(body)))
    messages = [{'role': 'system', 'content': [marked('stable'), {'type': 'text', 'text': 'tail'}]},
                {'role': 'user', 'content': 'subject'}]
    _, usage = cache_wire.invoke(messages, asynchronous=asynchronous)
    assert len(cache_wire.sent) == 2
    assert native_markers(cache_wire.sent[0]['body']) and not native_markers(cache_wire.sent[1]['body'])
    assert len(usage['ledger_attempt_ids']) == 2
    assert usage['request_wire']['compatibility_learning'] == 'not_performed_translated_surface'
    rows = {r['attempt_id']: r for r in ledger_rows(cache_wire.root)}
    attempts = [rows[a] for a in usage['ledger_attempt_ids']]
    assert attempts[-1]['state'] == 'settled'
    assert attempts[0]['state'] == 'unresolved'  # 400 has no priced/usage witness, not invented free.


@pytest.mark.parametrize('status', [401, 403, 429])
def test_noncompatibility_marker_named_errors_never_resend(cache_wire, status):
    cache_wire.state.update(status=status, error=True)
    with pytest.raises(openai.APIStatusError):
        cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
    assert len(cache_wire.sent) == 1


def test_implicit_mode_keeps_first_three_system_boundaries_without_dropping_text(cache_wire):
    messages = [{'role': 'system', 'content': [marked(str(i)) for i in range(6)]}]
    _, usage = cache_wire.invoke(messages, tools=TOOLS)
    body = cache_wire.sent[0]['body']
    assert [b['text'] for b in body['input'][0]['content']] == list(map(str, range(6)))
    assert [b['text'] for b in native_markers(body)] == ['0', '1', '2']
    assert usage['request_wire']['observed_http']['cache']['breakpoints'] == 3
    assert usage['prompt_cache_breakpoints_reduced'] == {'declared': 6, 'kept': 4, 'dropped': 2}


def test_paramless_native_hint_rejection_and_no_cross_call_learning(cache_wire):
    cache_wire.state.update(status=400, error=lambda body: bool(native_markers(body)),
                           error_body={'message': 'Unsupported parameter: prompt_cache_breakpoint',
                                       'type': 'invalid_request_error'})
    for _ in range(2):
        _, usage = cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
        assert usage['request_wire']['compatibility_learning'] == 'not_performed_translated_surface'
    assert [bool(native_markers(item['body'])) for item in cache_wire.sent] == [True, False, True, False]
    assert not (cache_wire.wire_root / 'state' / 'request_wire_compatibility.json').exists()


@pytest.mark.parametrize('error', [
    {'param': 'temperature', 'message': 'Unsupported value prompt_cache_breakpoint for temperature'},
    {'param': 'prompt_cache_breakpoint_mode', 'message': 'Unsupported parameter: prompt_cache_breakpoint_mode'},
    {'param': 'input[0].content[0].prompt_cache_breakpoint', 'message': 'Input too long', 'code': 'context_length_exceeded'},
])
def test_other_field_value_or_context_does_not_remove_markers(cache_wire, error):
    cache_wire.state.update(status=400, error=True, error_body={'type': 'invalid_request_error', **error})
    with pytest.raises(openai.BadRequestError):
        cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
    assert len(cache_wire.sent) == 1


@pytest.mark.parametrize('model', MODELS[3:])
def test_claude_native_field_error_does_not_trigger_responses_mapping(cache_wire, model):
    cache_wire.state.update(status=400, error=True)
    with pytest.raises(openai.BadRequestError):
        cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}], model=model)
    assert len(cache_wire.sent) == 1 and cache_wire.sent[0]['path'] == '/chat/completions'


def test_native_breakpoint_refusal_never_drops_unmapped_ttl_first(cache_wire):
    cache_wire.state.update(status=400, error=lambda body: bool(native_markers(body)),
        error_body={'param': 'input[0].content[0].prompt_cache_breakpoint',
                    'message': 'Unsupported parameter prompt_cache_breakpoint; ttl is not accepted here',
                    'type': 'invalid_request_error'})
    _, usage = cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
    assert len(cache_wire.sent) == 2
    assert cache_wire.sent[0]['body'] != cache_wire.sent[1]['body']
    assert len(usage['ledger_attempt_ids']) == 2


def test_persistent_native_refusal_stops_after_marker_free_retry(cache_wire):
    cache_wire.state.update(status=400, error=True)
    with pytest.raises(openai.BadRequestError):
        cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
    assert len(cache_wire.sent) == 2
    assert native_markers(cache_wire.sent[0]['body']) and not native_markers(cache_wire.sent[1]['body'])


@pytest.mark.parametrize('kind', ['timeout', 'pending', 'http500'])
def test_unknown_or_server_error_does_not_authorize_marker_omission_retry(cache_wire, kind):
    if kind == 'http500':
        cache_wire.state.update(error=True, status=500)
        expected = openai.APIStatusError
    else:
        cache_wire.state[kind] = True
        expected = openai.APITimeoutError if kind == 'timeout' else nt.CopilotPendingResponse
    with pytest.raises(expected):
        cache_wire.invoke([{'role': 'system', 'content': [marked('stable')]}])
    assert len(cache_wire.sent) == 1 and native_markers(cache_wire.sent[0]['body'])
    rows = [r for r in ledger_rows(cache_wire.root) if r.get('task_id') == cache_wire.task]
    assert len(rows) == 1 and rows[0]['state'] == 'unresolved'
