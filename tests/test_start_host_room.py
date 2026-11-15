"""Actual first-send host growth fits the start; arriving owner/history is not a new start."""
import dataclasses

import pytest
from ouroboros import capability_evidence as ce, context_fit as cf, memory_view as mv
from ouroboros.context_budget import bound_host_row
from tests import _memory_view_synthetic as syn
from tests.test_main_authored_context import main_loop  # noqa: F401


def _start(f, monkeypatch):
    monkeypatch.setattr(cf, '_route_calibration_ratio', lambda *a, **k: 1.0)
    snap = syn.snapshot(role='integrator', story=syn.pages(2, 200),
        room_facts=syn.room(lane1=[syn.spoken(i, 'ouroboros', 400) for i in range(2400)], lane2=2))
    snap = dataclasses.replace(snap, spec=dataclasses.replace(snap.spec, work_room_levels=2))
    core = cf.ContextCore('Required core', 'Constitution', 'b' * 600000, '', 'Identity', 'f' * 1000,
                         '"Assignment"', False, memory_view_json=mv.snapshot_json(snap))
    plan = cf.build_context_fit_plan(None, core, {'id': 'authored-main', 'type': 'task'},
        preferred_mode='max', tool_schemas=f.registry.schemas(), route_resolver=lambda *a, **k: (
            {'provider': 'openai', 'model': 'openai/test-model'},
            ce.CapabilityEvidence(400000, 'confirmed', 'metadata', 'start-hosts')))
    assert plan.initial_mode == 'low'
    f.ctx.context_fit_plan, f.messages = plan, plan.messages_for('low')
    f.messages.append(bound_host_row('independent_roots', 'Synthetic host roster: ' + 'x' * 6000))
    return plan


@pytest.mark.parametrize('owner_arrives', [False, True])
def test_first_send_host_room_and_owner_tail_guard(main_loop, monkeypatch, owner_arrives):
    f = main_loop
    plan = _start(f, monkeypatch)
    if owner_arrives:
        f.messages.append({'role': 'user', 'content': 'Owner correction: preserve this exact request.'})
    original = plan.low_projection.system_message()
    assert f.run([{'content': 'done'}])[0] == 'done'
    sent = f.inputs[0]
    events = []
    while not f.events.empty():
        events.append(f.events.get_nowait().get('data') or {})
    narrowed = [e for e in events if e.get('checkpoint_kind') == 'context_memory_coarsened'
                and e.get('cause') == 'start_host_rows']
    if owner_arrives:
        assert sent['messages'][0] == original
        assert not narrowed and f.ctx.context_fit_plan is plan
        assert any(m.get('content') == 'Owner correction: preserve this exact request.' for m in sent['messages'])
    else:
        assert narrowed and sent['messages'][0] != original
        assert cf.estimate_context_prompt_tokens(sent['messages'], sent['tools'], provider='openai') + 65536 + 100000 <= 400000
        assert all(m.get('role') == 'user' for m in sent['messages'][1:])
