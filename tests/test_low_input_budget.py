"""Owner Nov15: Low targets input; Nano and physical totals retain their meaning."""
import dataclasses

import pytest
from jsonschema import validate

from ouroboros import context_budget as cb, context_fit as cf
from ouroboros.physical_attempt_context import PhysicalAttemptContext
from ouroboros.tool_result_delivery import frame_bounds
from tests.test_context_fit_v664 import _plan


@pytest.mark.parametrize('reply', [2048, 4096, 65536])
@pytest.mark.parametrize('levels', [0, 1, 2])
def test_low_input_allowance_is_independent_of_reply_ceiling(reply, levels):
    total, reserve = cb.context_mode_limits('low', 'low', reply)
    assert (total, reserve) == (95000 + reply, reply)
    frame = cb.request_context_budget(window_tokens=None, output_reserve_tokens=reserve,
        non_memory_tokens=0, target_tokens=total, target_input_tokens=95000, margin_count=levels)
    assert frame['without_margin_tokens'] == 95000
    assert frame['with_margin_tokens'] == 95000 - levels * 11875
    assert cb.context_mode_limits('nano', 'nano', reply) == (35000, min(reply, 8192))
    assert cb.context_input_target('low', 'max') is None


@pytest.mark.parametrize('size,deficit', [(95000, 0), (95001, 1), (100000, 5000)])
def test_main_measurement_and_physical_record_are_explicit(monkeypatch, size, deficit):
    from ouroboros import capability_evidence as ce
    from ouroboros.tools.vision_process import _CONTEXT
    monkeypatch.setattr(cf, 'estimate_context_prompt_tokens', lambda *a, **k: size)
    monkeypatch.setattr(ce, 'resolve_main_token_density', lambda *a, **k: (1.0, 'cold_estimate'))
    result = cf.measure_main_fit(_plan(preferred='low'), [], [], profile='owner_low',
                                 rendered_mode='low', round_id='one')
    m = result.measurement
    assert (m.target_input_tokens, m.target_total_tokens) == (95000, 160536)
    assert m.response_reserve_tokens == m.reply_allowance_tokens == 65536
    assert m.target_deficit_tokens == deficit
    assert result.action == ('send_target_miss' if deficit else 'send')
    assert m.low_water_margin_tokens == (11875 if deficit else 0)
    physical = dataclasses.asdict(cf._physical_context_for_fit(result))
    assert physical['target_input_tokens'] == 95000
    validate(physical, _CONTEXT)
    legacy = PhysicalAttemptContext(profile='owner_low', rendered_mode='low',
        measurement_basis='cold_estimate', route_fp='old', round_id='old',
        target_total_tokens=95000, capacity_total_tokens=None, context_target_miss=True,
        automatic_pass_used=False)
    assert legacy.target_input_tokens is None and legacy.target_total_tokens == 95000


@pytest.mark.parametrize('capacity,boundary,eighth', [(None, None, 11875), (500000, 500000, 11875),
                                                     (128000, 128000, 16000), (145536, 80000, 10000)])
def test_binding_bound_selected_in_compatible_units(capacity, boundary, eighth):
    args = dict(target_total_tokens=160536, capacity_total_tokens=capacity,
                target_input_tokens=95000, boundary_tokens=boundary)
    assert cf.reclaim_low_water_margin(**args) == eighth
    measured = dict(target_total_tokens=160536, target_input_tokens=95000,
                    capacity_total_tokens=capacity, response_reserve_tokens=65536,
                    window={'tokens': boundary})
    assert frame_bounds(measured)[2] == eighth
    frame = cb.request_context_budget(window_tokens=capacity, output_reserve_tokens=65536,
        non_memory_tokens=0, target_tokens=160536, target_input_tokens=95000, boundary_tokens=boundary)
    assert frame['working_margin_tokens'] == eighth


def test_existing_total_target_keeps_input_only_window_margin():
    # Nano's existing min(total-target, actual input boundary) margin is unchanged.
    assert cb.context_working_margin(35000, 38192, boundary_tokens=30000) == 3750
    # Existing reclaim/tool-share callers used total-equivalent bounds, not the
    # memory floor's input boundary; the Low change does not revise Nano's policy.
    assert frame_bounds(dict(target_total_tokens=35000, capacity_total_tokens=38192,
                             response_reserve_tokens=8192, window={'tokens': 30000}))[2] == 4375


def test_dual_window_bound_is_shared_by_measurement_and_tool_batch(monkeypatch):
    from ouroboros import capability_evidence as ce
    from ouroboros.context_window import WindowLimit, WindowRecord
    monkeypatch.setattr(cf, 'estimate_context_prompt_tokens', lambda *a, **k: 95001)
    monkeypatch.setattr(ce, 'resolve_main_token_density', lambda *a, **k: (1.0, 'cold_estimate'))
    limits = (WindowLimit(100000, 'input', 'metadata'), WindowLimit(128000, 'total', 'metadata'))
    record = WindowRecord('route-a', input=limits[0], total=limits[1], reserve=65536, declared=limits)
    plan = dataclasses.replace(_plan(preferred='low', window=128000, known=True), window_record=record)
    m = cf.measure_main_fit(plan, [], [], profile='owner_low', rendered_mode='low', round_id='dual').measurement
    assert m.window['tokens'] == 128000 and m.window['boundary_tokens'] == 100000
    assert m.low_water_margin_tokens == frame_bounds(dataclasses.asdict(m))[2] == 12500
