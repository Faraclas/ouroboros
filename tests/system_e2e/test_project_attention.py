"""S42: real host idle admission and received project context, keyless model.

The stub chooses no work; it proves host/context delivery, not model judgment.
The source copy includes dirty candidate bytes and leaves the original unchanged.
"""
from pathlib import Path
import json
import re
import time
import uuid

import pytest
from devtools.benchmarks.common.server_runner import _api
from tests.candidate_checkout import candidate_checkout
from tests.system_e2e.harness import (
    LANE_MOCK, ArtifactOracle, ModelGate, ScriptedStubModel, body_text,
    keyless_settings, require_lane, start_server, wait_durable_result, wait_until, ws_url,
)
from tests.system_e2e.test_ordinary_conversation_capabilities import _received_turn
from tests.system_e2e.test_system_scenarios_w6 import _WsFrames


class AttentionModel(ScriptedStubModel):
    def __init__(self, gate):
        super().__init__(gate=gate, model_ids=['mock-attention', 'mock-scout'])
        self.attention_round = 0
        self.scout_id = ''

    def _answer(self, body, seq):
        if body.get('model') == 'mock-scout':
            return 'scout', {'role': 'assistant', 'content': 'SCOUT_DONE'}
        if body.get('model') != 'mock-attention':
            return super()._answer(body, seq)
        self.attention_round += 1
        text = body_text(body)
        if self.attention_round == 1:
            name, args = 'schedule_subagent', {
                'subagent_id': 'openai-compatible::mock-scout',
                'objective': 'Read-only consideration; no work is needed. Return SCOUT_DONE.',
                'expected_output': 'SCOUT_DONE', 'may_fan_out': False,
            }
        elif self.attention_round == 2:
            matches = re.findall(r'Subagent request queued ([a-zA-Z0-9_-]+)', text)
            assert matches, 'actual child admission receipt missing'
            self.scout_id = matches[-1]
            name, args = 'wait_task', {'task_id': self.scout_id, 'timeout_sec': 180}
        elif self.attention_round == 3:
            name, args = 'get_task_result', {'task_id': self.scout_id, 'view': 'answer'}
        elif self.attention_round == 4:
            hashes = re.findall(r'"child_result_sha256"\s*:\s*"([0-9a-f]{64})"', text)
            assert hashes, 'actual child result identity missing'
            name, args = 'tree_note', {'kind': 'decision', 'text': 'Read-only scout incorporated; no work selected.',
                'payload': {'type': 'child_result_disposition', 'child_task_id': self.scout_id,
                            'child_result_sha256': hashes[-1], 'disposition': 'integrated'}}
        else:
            name, args = 'finish_task', {'action': 'finish', 'silent': True, 'summary': 'No work selected.'}
        return 'attention', {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': f'call-{seq}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)},
        }]}


@pytest.mark.integration
@pytest.mark.serial
@pytest.mark.timeout(600)
def test_s42_project_idle_context_no_work_scout_and_later_opportunity(tmp_path):
    require_lane(LANE_MOCK)
    source = Path(__file__).resolve().parents[2]
    gate = ModelGate(lambda body: body.get('model') == 'mock-model' and 'ATTENTION_HELD' in body_text(body), timeout=180)
    with candidate_checkout(source, tmp_path / 'copy') as copied, AttentionModel(gate) as stub:
        settings = keyless_settings(stub, OUROBOROS_MODEL_CONSCIOUSNESS='openai-compatible::mock-attention',
            OUROBOROS_CONSCIOUSNESS_AUTONOMY='observe', OUROBOROS_MAX_ROUNDS=30,
            OUROBOROS_SUBAGENTS=json.dumps({'enabled': True, 'items': [{
                'subagent_id': 'scout', 'route': {'kind': 'api_model', 'target_id': 'openai-compatible::mock-scout'},
                'recommended_use': 'Read-only fixture scout.',
            }]}))
        server = start_server(copied.path, tmp_path / 'server', settings)
        try:
            oracle = ArtifactOracle(server.data_root)
            project = _api(server.base_url, 'POST', '/api/projects', {'name': 'Unique Project Attention Context'}, timeout=60)['project']
            from websockets.sync.client import connect
            with connect(ws_url(server), open_timeout=30, proxy=None) as ws, _WsFrames(ws):
                ws.send(json.dumps({'type': 'command', 'cmd': '/bg start'}))
                assert wait_until(lambda: _api(server.base_url, 'GET', '/api/state').get('bg_consciousness_enabled'), 30)
                def send(text):
                    mid = 'attention-' + uuid.uuid4().hex
                    ws.send(json.dumps({'type': 'chat', 'content': text, 'client_message_id': mid,
                                        'chat_id': project['chat_id'], 'project_id': project['id']}))
                    return mid
                def attention_tasks():
                    return [row['task'] for row in oracle.events('task_received')
                            if (row.get('task', {}).get('metadata') or {}).get('wake_cause') == 'project_idle']
                held = send('ATTENTION_HELD: answer when released.')
                assert gate.arrived.wait(120), 'ordinary direct turn never reached the real model receiver'
                assert not attention_tasks(), 'busy project was declared idle'
                gate.release.set()
                ordinary = wait_until(lambda: _received_turn(oracle, held), 30)
                assert ordinary and ordinary['project_id'] == project['id']
                wait_durable_result(oracle, ordinary['id'], timeout=120)
                first = wait_until(lambda: attention_tasks(), 120)
                assert first and len(first) == 1, first
                first = first[0]
                assert first['project_id'] == project['id'] and first['chat_id'] == project['chat_id']
                assert first['_is_direct_chat'] and first['metadata']['consciousness_cause'] == 'project_idle'
                final = wait_durable_result(oracle, first['id'], timeout=180)
                assert final.get('status') == 'completed', final
                recent = _api(server.base_url, 'GET', '/api/consciousness', timeout=60)['recent_wakes']
                assert next(row for row in recent if row['task_id'] == first['id'])['cause'] == 'project_idle'
                assert stub.scout_id and oracle.task_result(stub.scout_id)['status'] == 'completed'
                assert oracle.task_result(stub.scout_id)['metadata']['consciousness_cause'] == 'project_idle'
                received = [body for kind, body in stub.calls if kind == 'attention']
                assert received and 'Unique Project Attention Context' in body_text(received[0])
                assert '[Project attention · idle opportunity]' in body_text(received[0])
                # The bounded negative is paired with a later positive, not a dead observer.
                time.sleep(3)
                import sqlite3
                with sqlite3.connect(f"file:{server.data_root / 'state/current.sqlite'}?mode=ro", uri=True) as conn:
                    episodes = conn.execute("SELECT facts FROM obligations WHERE set_name='project_attention'").fetchall()
                assert len(attention_tasks()) == 1, json.dumps({'episodes': episodes,
                    'scout_received': [row['task'].get('metadata') for row in oracle.events('task_received')
                                       if row.get('task', {}).get('id') == stub.scout_id]}, ensure_ascii=False)
                later = send('LATER_ORDINARY: answer here.')
                task = wait_until(lambda: _received_turn(oracle, later), 30)
                wait_durable_result(oracle, task['id'], timeout=120)
                assert wait_until(lambda: len(attention_tasks()) == 2, 120), attention_tasks()
                second = attention_tasks()[1]
                wait_durable_result(oracle, second['id'], timeout=120)
                assert second['id'] != first['id']
        finally:
            gate.release.set()
            server.stop()
