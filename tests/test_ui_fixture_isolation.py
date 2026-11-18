"""Control: inherited (dummy) provider credentials never reach a ui_browser test."""
import subprocess
import sys
from pathlib import Path

from ouroboros.test_environment import owner_configuration_keys

REPO = Path(__file__).parents[1]
DUMMY = {"OPENAI_COMPATIBLE_API_KEY": "dummy-inherited", "OPENAI_COMPATIBLE_BASE_URL": "https://dummy.invalid/v1",
         "ANTHROPIC_API_KEY": "dummy-inherited", "SOME_PROVIDER_TOKEN": "dummy-inherited"}


def test_owner_configuration_keys_name_provider_inputs_and_keep_harness_controls():
    env = {**DUMMY, "OUROBOROS_RUN_UI_SMOKE": "1", "PLAYWRIGHT_BROWSERS_PATH": "/x", "PATH": "/bin"}
    assert set(owner_configuration_keys(env)) == set(DUMMY)


def test_ui_browser_marked_test_starts_without_inherited_provider_inputs(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text("import os, pytest\n"
                     "@pytest.mark.ui_browser\n"
                     "def test_marked():\n"
                     "    assert not [k for k in os.environ if k.startswith(('OPENAI_COMPATIBLE_', 'ANTHROPIC_'))]\n"
                     "    assert os.environ['OUROBOROS_RUN_UI_SMOKE'] == '1'\n"
                     "def test_unmarked_keeps_env():\n"
                     "    assert os.environ['OPENAI_COMPATIBLE_API_KEY'] == 'dummy-inherited'\n")
    env = {**__import__("os").environ, **DUMMY, "OUROBOROS_RUN_UI_SMOKE": "1", "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.ui_fixture_isolation", "-p", "no:cacheprovider",
                          "--rootdir", str(tmp_path), "-c", "/dev/null", str(probe), "-q", "-W", "ignore"],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "2 passed" in out.stdout
