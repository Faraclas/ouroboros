"""Installation authorization reaches the real checker path.

Fake family-labelled responses test evidence and dispatch, NOT model judgment.
The companion bounded live no-handler exercise judges policy interpretation.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import safety
from ouroboros.llm import LLMClient


@pytest.mark.parametrize('model', ['openai/gpt-4.1-mini', 'anthropic/claude-sonnet-4'])
@pytest.mark.parametrize('covered', [False, True])
def test_install_evidence_and_verdict_reach_real_consumer(tmp_path, monkeypatch, model, covered):
    owner = ('I authorize requests from PyPI in this project venv.' if covered
             else 'Build a website.')
    ctx = SimpleNamespace(task_id='fixture', drive_root=tmp_path, budget_drive_root=tmp_path,
        repo_dir=Path(__file__).resolve().parents[1], task_metadata={},
        task_contract={'objective': 'Build a website'}, current_chat_id=1,
        _owner_directives=[{'source': 'initial_user', 'content': owner}],
        _owner_evidence_loaded=True, _owner_evidence_gaps=[])
    calls=[]
    class Sender:
        supports_response_format=staticmethod(LLMClient.supports_response_format)
        def chat(self, **kw):
            calls.append(kw)
            prompt=kw['messages'][-1]['content']
            bundle=json.JSONDecoder().raw_decode(prompt.split('Host evidence (provenance is not consent):\n',1)[1])[0]
            assert bundle['occasion']['sources']==ctx._owner_directives
            # No assertion that task objective equals permission.
            assert bundle['assignment']['authority']=='intent_only'
            return {'content':json.dumps({'status':'SAFE' if covered else 'DANGEROUS','reason':'scripted fixture'})},None
    monkeypatch.setattr(safety,'LLMClient',Sender)
    monkeypatch.setattr(safety,'_resolve_safety_routing',lambda:(False,False,None))
    monkeypatch.setattr(safety,'get_safety_model',lambda:model)
    allowed,message=safety.check_safety('run_command',{'cmd':['uv','pip','install','requests']},ctx=ctx)
    assert allowed is covered
    assert len(calls)==1 and calls[0]['model']==model
    assert ('SAFETY_VIOLATION' in message) is (not covered)

