"""Dispatch deadlines narrow phase limits across supported HTTP backends."""
import httpx
import pytest

from ouroboros.deadline_utils import physical_dispatch_timeout
from ouroboros.net_transport import _openai_httpx_module


TIMEOUT_TYPES = list(dict.fromkeys([httpx.Timeout, _openai_httpx_module().Timeout]))


@pytest.mark.parametrize("timeout_type", TIMEOUT_TYPES)
def test_phase_limits_never_widen(monkeypatch, timeout_type):
    monkeypatch.setattr("ouroboros.llm_attempt.require_physical_dispatch_window", lambda: 5)
    original = timeout_type(connect=1, read=None, write=2, pool=30)
    result = physical_dispatch_timeout(original)
    assert type(result) is timeout_type
    assert result.as_dict() == {"connect": 1, "read": 5, "write": 2, "pool": 5}
    assert original.as_dict() == {"connect": 1, "read": None, "write": 2, "pool": 30}


@pytest.mark.parametrize("timeout_type", TIMEOUT_TYPES)
def test_unbounded_dispatch_keeps_original_object(monkeypatch, timeout_type):
    monkeypatch.setattr("ouroboros.llm_attempt.require_physical_dispatch_window", lambda: None)
    original = timeout_type(connect=1, read=None, write=2, pool=30)
    assert physical_dispatch_timeout(original) is original


def test_production_no_proxy_caps_survive_large_window(monkeypatch):
    from ouroboros.llm import LLMClient

    monkeypatch.setattr("ouroboros.llm_attempt.require_physical_dispatch_window", lambda: 600)
    original = LLMClient._no_proxy_timeout(120)
    result = physical_dispatch_timeout(original)
    assert type(result) is type(original)
    assert result.as_dict() == {"connect": 30, "read": 120, "write": 120, "pool": 30}


@pytest.mark.parametrize("explicit,expected", [(2, 2), (30, 5), (None, 5)])
def test_scalar_and_default_limits(monkeypatch, explicit, expected):
    monkeypatch.setattr("ouroboros.llm_attempt.require_physical_dispatch_window", lambda: 5)
    assert physical_dispatch_timeout(explicit) == expected
