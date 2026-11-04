"""Shared runtime policy, metadata readiness, and exact adoption-target falsifiers.

All install subprocesses are doubles. No pip or system package mutation occurs.
"""
import builtins
from email.message import Message
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros import body_adoption, runtime_dependencies as deps, startup_migrations
from supervisor import git_ops, git_ops_reset as reset


@pytest.fixture
def base_python(tmp_path, monkeypatch):
    monkeypatch.delenv("OUROBOROS_PYTEST_ACTIVE", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "base"))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setattr("ouroboros.platform_layer.interpreter_is_embedded", lambda _: False)
    monkeypatch.setattr(deps.sysconfig, "get_path", lambda _: str(tmp_path / "stdlib"))
    marker = tmp_path / "stdlib" / "EXTERNALLY-MANAGED"
    marker.parent.mkdir()
    return marker


@pytest.mark.parametrize("pip_user", [None, "0", "1"])
def test_pep668_not_pip_user_classifies_base_and_venv(base_python, monkeypatch, pip_user):
    if pip_user is None:
        monkeypatch.delenv("PIP_USER", raising=False)
    else:
        monkeypatch.setenv("PIP_USER", pip_user)
    assert deps.install_mode() == "install"
    base_python.touch()
    assert deps.install_mode() == "externally_managed"
    monkeypatch.setattr(sys, "prefix", sys.prefix + "-venv")
    assert deps.install_mode() == "install"  # inherited base marker must not suppress a venv


def test_embedded_frozen_and_test_paths_survive(base_python, monkeypatch):
    base_python.touch()
    monkeypatch.setattr("ouroboros.platform_layer.interpreter_is_embedded", lambda _: True)
    assert deps.install_mode() == "install"
    monkeypatch.setenv("OUROBOROS_PYTEST_ACTIVE", "1")
    assert deps.install_mode() == "pytest:suppressed"
    assert reset.runtime_dependency_command(base_python.parent) is None
    assert reset.sync_runtime_dependencies("test") == (True, "pytest:suppressed")
    monkeypatch.setattr(sys, "frozen", True)
    assert deps.install_mode() == "frozen:bundled"
    assert reset.runtime_dependency_command(base_python.parent) is None
    assert reset.sync_runtime_dependencies("test") == (True, "frozen:bundled")


def _project(*requirements, python=">=3.10", version="1.0"):
    import json
    return {"pyproject.toml": "[project]\nversion=" + json.dumps(version) + "\ndependencies="
            + json.dumps(list(requirements)) + "\nrequires-python=" + json.dumps(python) + "\n"}


def _installed(monkeypatch, packages):
    def distribution(name):
        if name not in packages:
            raise deps.metadata.PackageNotFoundError(name)
        version, requires, extras = packages[name]
        message = Message()
        for extra in extras:
            message["Provides-Extra"] = extra
        return SimpleNamespace(version=version, requires=requires, metadata=message)
    monkeypatch.setattr(deps.metadata, "distribution", distribution)


def test_supported_ranges_markers_extras_and_transitive_metadata(monkeypatch, tmp_path):
    _installed(monkeypatch, {"example": ("3.4", ["leaf>=2; extra == 'fast'", "cycle"], ["fast"]),
                            "leaf": ("2.5", [], []), "cycle": ("1", ["example"], [])})
    sources = _project("example[fast]>=2,<4", "missing; python_version < '2'")
    sources["requirements-runtime.lock"] = "example==1.0\n"  # recipe is not a compatibility maximum
    assert deps.verify_external_requirements(tmp_path, sources=sources).startswith("externally_managed:verified:")
    for requirement in ("example>=4", "example<3", "missing", "example[absent]"):
        with pytest.raises(deps.DependencyReadinessError):
            deps.verify_external_requirements(tmp_path, sources=_project(requirement))
    _installed(monkeypatch, {"example": ("3.4", ["leaf>=2; extra == 'fast'"], ["fast"])})
    with pytest.raises(deps.DependencyReadinessError, match="leaf.*missing"):
        deps.verify_external_requirements(tmp_path, sources=sources)
    with pytest.raises(deps.DependencyReadinessError, match="Python"):
        deps.verify_external_requirements(tmp_path, sources=_project(python=">=99"))


@pytest.mark.parametrize("sources", [
    {"pyproject.toml": "broken ["}, {"pyproject.toml": "[project]\nname='dynamic'"},
    {"requirements.txt": "--index-url https://example.invalid\n"},
    {"requirements.txt": "-r requirements.txt\n"},
    _project("example @ https://example.invalid/file.whl"),
])
def test_unknown_contract_is_never_satisfied(tmp_path, sources):
    with pytest.raises((deps.DependencyReadinessError, ValueError)):
        deps.verify_external_requirements(tmp_path, sources=sources)


def test_legacy_include_and_python310_parser_route(tmp_path, monkeypatch):
    _installed(monkeypatch, {"example": ("2", [], [])})
    assert deps.verify_external_requirements(tmp_path, sources={
        "requirements.txt": "-r requirements-runtime.lock", "requirements-runtime.lock": "example==2\n"
    }).startswith("externally_managed:verified:")
    # Make the selected recipe follow an include, rather than selecting the included lock directly.
    assert deps.verify_external_requirements(tmp_path, sources={
        "requirements.txt": "-r extra.txt", "extra.txt": "example==2\n"
    }).startswith("externally_managed:verified:")
    real_import = builtins.__import__
    called = []
    def py310(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError("3.10-shaped")
        if name == "pip._vendor":
            called.append(name)
            return SimpleNamespace(tomli=SimpleNamespace(loads=lambda text: {"project": {"dependencies": []}}))
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", py310)
    assert deps.project_contract(_project())[1] == []
    assert called == ["pip._vendor"]


def test_unavailable_parser_or_metadata_is_unverified(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ImportError("parser unavailable")
    monkeypatch.setattr(deps, "_toml", broken)
    with pytest.raises(deps.DependencyReadinessError, match="unavailable"):
        deps.verify_external_requirements(tmp_path, sources=_project())


@pytest.mark.parametrize("incompatible", ["example", "leaf"])
def test_direct_and_transitive_python_metadata_constraints(tmp_path, monkeypatch, incompatible):
    def distribution(name):
        message = Message()
        message["Requires-Python"] = ">=99" if name == incompatible else ">=3.10"
        return SimpleNamespace(version="3", metadata=message, requires=["leaf"] if name == "example" else [])
    monkeypatch.setattr(deps.metadata, "distribution", distribution)
    with pytest.raises(deps.DependencyReadinessError, match=f"{incompatible}: Python"):
        deps.verify_external_requirements(tmp_path, sources=_project("example>=2"))


def test_sync_external_fresh_verification_no_old_stamp_or_bootstrap_cache(base_python, tmp_path, monkeypatch):
    base_python.touch()
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    (tmp_path / "pyproject.toml").write_text(_project("example>=2")["pyproject.toml"])
    startup_migrations.stamp(tmp_path / "data", dependencies="old-unchecked-success")
    monkeypatch.setattr(reset.subprocess, "Popen", lambda *a, **k: pytest.fail("must not install"))
    _installed(monkeypatch, {"example": ("3", [], [])})
    assert reset.sync_runtime_dependencies("test")[1].startswith("externally_managed:verified:")
    assert not startup_migrations.watermarks(tmp_path / "data").get("dependencies")
    assert reset.runtime_dependency_command(tmp_path) is None
    assert reset._bootstrap_snapshot() is None
    _installed(monkeypatch, {})
    ok, detail = reset.sync_runtime_dependencies("test")
    assert ok and "unverified" in detail and "example>=2: missing" in detail
    assert not startup_migrations.watermarks(tmp_path / "data").get("dependencies")
    with pytest.raises(deps.DependencyReadinessError):
        reset.runtime_dependency_command(tmp_path)
    assert "deps_sync_unverified" in (tmp_path / "data/logs/supervisor.jsonl").read_text()


def test_unknown_marker_never_installs_or_blocks_core_smoke(base_python, tmp_path, monkeypatch):
    original = Path.stat
    def denied(path, *args, **kwargs):
        if path == base_python:
            raise PermissionError("marker probe denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", denied)
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(reset.subprocess, "Popen", lambda *a, **k: pytest.fail("must not install"))
    assert deps.install_mode().startswith("unverified:marker:")
    ok, detail = reset.sync_runtime_dependencies("test")
    assert ok and "unverified:marker:PermissionError" in detail
    assert reset._bootstrap_snapshot() is None
    with pytest.raises(deps.DependencyReadinessError, match="marker"):
        reset.runtime_dependency_command(tmp_path)


def test_pip_user_zero_venv_reaches_actual_owned_install_branch(base_python, tmp_path, monkeypatch):
    base_python.touch()
    monkeypatch.setattr(sys, "prefix", sys.prefix + "-venv")
    monkeypatch.setenv("PIP_USER", "0")
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    (tmp_path / "requirements-runtime.lock").write_text("example==2\n")
    commands = []
    monkeypatch.setattr(reset.subprocess, "Popen", lambda cmd, **kw: commands.append(cmd) or Mock(wait=lambda **kw: 0))
    assert reset.sync_runtime_dependencies("test")[0]
    assert commands == [reset.runtime_dependency_command(tmp_path)]
    assert startup_migrations.watermarks(tmp_path / "data")["dependencies"]
    assert reset.sync_runtime_dependencies("test")[1].startswith("unchanged:")
    assert len(commands) == 1


def test_adoption_reads_exact_target_not_mutable_worktree(base_python, tmp_path, monkeypatch):
    base_python.touch()
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    path = tmp_path / "pyproject.toml"
    path.write_text(_project("example>=1")["pyproject.toml"])
    git("add", "."); git("commit", "-qm", "old")
    old = git("rev-parse", "HEAD")
    path.write_text(_project("example>=4")["pyproject.toml"])
    git("commit", "-qam", "new")
    cand = git("rev-parse", "HEAD")
    path.write_text(_project("example>=1")["pyproject.toml"])  # would hide the missing requirement
    _installed(monkeypatch, {"example": ("2", [], [])})
    rows = [{"path": "pyproject.toml"}]
    with pytest.raises(body_adoption.AdoptionRefused) as refused:
        body_adoption._dependency_command(tmp_path, rows, old=old, cand=cand)
    assert refused.value.code == "ADOPTION_DEPENDENCIES_UNSATISFIED"
    _installed(monkeypatch, {"example": ("5", [], [])})
    assert body_adoption._dependency_command(tmp_path, rows, old=old, cand=cand) is None
    path.write_text(_project("example>=4", version="2.0")["pyproject.toml"])
    git("commit", "-qam", "version only")
    # No new requirement prerequisite for an unchanged contract, even if drifted.
    _installed(monkeypatch, {})
    assert body_adoption._dependency_command(tmp_path, rows, old=cand, cand=git("rev-parse", "HEAD")) is None
    # A lock refresh changes a reproducible install recipe, not this external
    # environment's already-unsatisfied supported-range contract.
    version_commit = git("rev-parse", "HEAD")
    (tmp_path / "requirements-runtime.lock").write_text("example==9\n")
    git("add", "."); git("commit", "-qm", "lock refresh")
    assert body_adoption._dependency_command(tmp_path, [{"path": "requirements-runtime.lock"}],
        old=version_commit, cand=git("rev-parse", "HEAD")) is None


@pytest.mark.parametrize("old_lock,new_files,expected", [
    (True, {"requirements.txt": "example>=2"}, "requirements.txt"),
    (False, {"requirements-runtime.lock": "example==2"}, "requirements-runtime.lock"),
    (True, {}, None),
])
def test_owned_switch_chooses_recipe_from_target_presence(base_python, tmp_path, old_lock, new_files, expected):
    # Commands execute AFTER files move: choosing by old file existence is wrong.
    old = tmp_path / ("requirements-runtime.lock" if old_lock else "requirements.txt")
    old.touch()
    command = reset.runtime_dependency_command(tmp_path, sources=new_files)
    if expected:
        assert command[-2:] == ["-r", str(tmp_path / expected)]
    else:
        assert command[-2:] == ["openai>=1.0.0", "requests"]


def test_supported_source_cli_and_child_imports_precede_installed_decoys(tmp_path):
    """The supported -m/cwd consumer, NOT an arbitrary external script, owns source selection."""
    import json
    import os
    import ouroboros
    root = Path(__file__).resolve().parents[1]
    assert Path(reset.__file__).resolve() == root / "supervisor/git_ops_reset.py"
    assert Path(ouroboros.__file__).resolve() == root / "ouroboros/__init__.py"
    decoy = tmp_path / "decoy"
    for package in ("ouroboros", "supervisor"):
        directory = decoy / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("raise AssertionError('installed decoy won')\n")
    env = {**os.environ, "PYTHONPATH": str(decoy)}
    code = ("import json,ouroboros,supervisor.git_ops_reset as r; "
            "print(json.dumps([ouroboros.__file__,r.__file__]))")
    observed = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                              capture_output=True, text=True, timeout=30, check=True)
    assert [str(Path(p).resolve()) for p in json.loads(observed.stdout)] == [
        str(root / "ouroboros/__init__.py"), str(root / "supervisor/git_ops_reset.py")]
    help_result = subprocess.run([sys.executable, "-m", "ouroboros.cli", "--help"],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert help_result.returncode == 0, help_result.stderr
    assert "usage:" in help_result.stdout
