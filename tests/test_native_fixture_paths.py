"""Synthetic display roots are stable; real registry and physical refusal are unchanged."""
import dataclasses
import json
import re

import pytest

from tests._memory_view_context import native_fixture_core
from tests.test_context_economy_window import _native, main_call, setup, live_wait  # noqa: F401


@dataclasses.dataclass(frozen=True)
class Captured:
    dynamic_head_text: str
    dynamic_text: str
    semi_stable_text: str
    user_content_json: str
    supplementary_messages_json: str
    memory_view_json: str


def test_only_world_metadata_is_length_invariant_not_foreign_or_owner_text():
    foreign = '/parent/foreign-session'
    normalized = []
    for root in ('/parent/w', '/parent/' + 'long-' * 100 + '/w'):
        core = Captured(f'Source: `{root}/repo/docs/DESIGN.md`', json.dumps({'drive': root + '/drive'}),
                        f'{root}/drive/memory/identity.md\n{foreign}\n{root}-other',
                        json.dumps(root), json.dumps(root), json.dumps(root))
        after = native_fixture_core(core, root)
        assert (after.user_content_json, after.supplementary_messages_json, after.memory_view_json) == (
            core.user_content_json, core.supplementary_messages_json, core.memory_view_json)
        assert foreign in after.semi_stable_text and root + '-other' in after.semi_stable_text
        normalized.append((after.dynamic_head_text, after.dynamic_text, after.semi_stable_text.splitlines()[0]))
    assert normalized[0] == normalized[1]


def test_unrewritten_long_host_metadata_is_still_measured_and_refused(main_call, monkeypatch, tmp_path):
    from ouroboros.context_fit import MinimumViewUnfit
    from tests import _memory_view_context as fixture
    # Exaggerate only captured host text, not actual disk roots or tool authority.
    def long_metadata(core, root):
        value = str(root)
        return dataclasses.replace(core, **{name: getattr(core, name).replace(value, '/synthetic/' + 'part/' * 200)
            for name in ('dynamic_head_text', 'dynamic_text', 'semi_stable_text')})
    monkeypatch.setattr(fixture, 'native_fixture_core', long_metadata)
    _ctx, _gateway, _tools, _events, plan, _windows = _native(main_call, monkeypatch, tmp_path)
    with pytest.raises(MinimumViewUnfit) as failure:
        plan(150_000)
    # This new test uses the ordinary root view (two work levels), unlike the
    # original victim cases' explicitly supplied Main view (one level).
    from ouroboros.context_budget import OWNER_LOW_TARGET_TOKENS, RECLAIM_LOW_WATER_DIVISOR
    work = 2 * ((min(150_000, OWNER_LOW_TARGET_TOKENS) + RECLAIM_LOW_WATER_DIVISOR - 1)
                // RECLAIM_LOW_WATER_DIVISOR)
    assert OWNER_LOW_TARGET_TOKENS == 95_000 and work == 23_750
    values = re.search(rf'(\d+) input \+ 65536 reply \+ {work} work > 150000', str(failure.value))
    assert values and int(values[1]) > 150_000 - 65_536 - work, str(failure.value)


def test_normalized_host_metadata_never_shrinks_protected_oversize_assignment(main_call, monkeypatch, tmp_path):
    from ouroboros.context_fit import MinimumViewUnfit
    _ctx, _gateway, _tools, _events, plan, _windows = _native(main_call, monkeypatch, tmp_path,
        task_text='protected assignment ' * 20_000)
    with pytest.raises(MinimumViewUnfit):
        plan(150_000)
