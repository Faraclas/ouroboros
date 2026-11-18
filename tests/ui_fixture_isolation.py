"""Disposable browser fixtures own their provider, account and settings inputs.

A UI fixture writes the settings it needs into its own data directory. Without this fence the
owner's real OPENAI_COMPATIBLE_*, other provider keys and every settings-projected variable that
the test process inherited are read by the disposable backend too, so provider listings and
catalog routes depend on whoever launched pytest. Every ``ui_browser`` test starts with those
variables absent; a fixture that wants a value sets it explicitly (dummy values only). Harness
controls such as OUROBOROS_RUN_UI_SMOKE and browser paths are not owner configuration and stay.
"""
import os

import pytest

from ouroboros.test_environment import owner_configuration_keys


@pytest.fixture(autouse=True)
def _ui_browser_owns_provider_inputs(request, monkeypatch):
    if request.node.get_closest_marker("ui_browser"):
        for key in owner_configuration_keys(os.environ):
            monkeypatch.delenv(key, raising=False)
