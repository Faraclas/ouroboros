"""Real Git interoperability and historical/current/generation consumer boundaries."""
from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ouroboros import config, server_process, update_letter, usage_store
from ouroboros.gateway import state as gateway
from supervisor import message_bus, queue, state, workers

OLD = "a" * 40


def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "current")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--allow-empty", "-m", "initial")
    return repo


@pytest.fixture
def api_scene(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    (data / "logs").mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(data / "settings.json"))
    usage_store.migrate_from_journal(data)
    monkeypatch.setattr(state, "TOTAL_BUDGET_LIMIT", 0.0)
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(queue, "get_evolution_status_snapshot", lambda **kw: {})
    request = Request({
        "type": "http", "method": "GET", "path": "/api/state", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "client": ("test", 1),
        "app": SimpleNamespace(state=SimpleNamespace(drive_root=data, app_start=0.0)),
    })
    saved_path = data / "state" / "saved-checkout.json"

    def read(repo, saved):
        # A real persisted old record is read-only throughout the consumer call.
        saved_path.write_text(json.dumps(saved), encoding="utf-8")
        before = saved_path.read_bytes()
        monkeypatch.setattr(config, "REPO_DIR", repo)
        monkeypatch.setattr(state, "load_state", lambda: json.loads(saved_path.read_bytes()))
        response = asyncio.run(gateway.api_state(request))
        assert response.status_code == 200, response.body
        assert response.headers["cache-control"] == "no-store"
        assert saved_path.read_bytes() == before
        return json.loads(response.body)

    return read


@pytest.mark.parametrize("kind", ["named", "packed", "linked", "detached", "unborn", "missing"])
@pytest.mark.parametrize("saved", [
    {"current_branch": "historical", "current_sha": OLD},
    {"current_branch": "historical"},
    {"current_sha": OLD},
])
def test_real_git_api_ignores_saved_stamp(real_repo, tmp_path, monkeypatch, api_scene, kind, saved):
    repo = real_repo
    sha = _git(repo, "rev-parse", "HEAD")
    branch = "current"
    if kind == "packed":
        _git(repo, "pack-refs", "--all")
        assert not (repo / ".git/refs/heads/current").exists()
    elif kind == "linked":
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", "-b", "feature/lane", str(linked))
        _git(repo, "pack-refs", "--all")
        repo, branch = linked, "feature/lane"
        assert (repo / ".git").is_file()
    elif kind == "detached":
        _git(repo, "checkout", "--detach")
        branch = None
    elif kind == "unborn":
        _git(repo, "checkout", "--orphan", "unborn")
        branch, sha = "unborn", ""
    elif kind == "missing":
        repo, branch, sha = tmp_path / "missing", None, ""
    before = copy.deepcopy(saved)
    body = api_scene(repo, saved)
    assert (body["branch"], body["sha"]) == (branch, sha[:8])
    assert saved == before
    assert gateway._runtime_repo_identity(saved) == (branch, sha)


def test_disk_advance_and_same_commit_branch_change_do_not_rebind_generation(real_repo, monkeypatch, api_scene):
    monkeypatch.setattr(server_process, "_source_baseline", None)
    initial = _git(real_repo, "rev-parse", "HEAD")
    assert server_process.capture_server_source_baseline(real_repo) == initial
    saved = {"current_branch": "current", "current_sha": initial}
    _git(real_repo, "checkout", "-b", "other")
    body = api_scene(real_repo, saved)
    assert (body["branch"], body["sha"]) == ("other", initial[:8])
    _git(real_repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--allow-empty", "-m", "advance")
    advanced = _git(real_repo, "rev-parse", "HEAD")
    body = api_scene(real_repo, saved)
    assert advanced != initial
    assert (body["branch"], body["sha"]) == ("other", advanced[:8])
    assert server_process.capture_server_source_baseline(real_repo) == initial
    bound = update_letter.runtime_status({"current_sha": advanced})
    assert (bound["current_sha"], bound["running_sha"]) == (advanced, initial)
    assert saved == {"current_branch": "current", "current_sha": initial}


@pytest.mark.parametrize("available", [True, False])
def test_budget_footer_explicitly_labels_saved_identity(monkeypatch, available):
    saved = {"current_branch": "historical", "current_sha": OLD,
             "_budget_accounting_available": available}
    before = copy.deepcopy(saved)
    text = message_bus._format_budget_line(saved)
    assert "saved checkout historical@aaaaaaaa" in text
    assert saved == before


def test_status_does_not_call_saved_stamp_running_version(monkeypatch):
    saved = {"current_branch": "historical", "current_sha": OLD}
    monkeypatch.setattr(state, "load_state", lambda: saved)
    text = state.status_text({}, [], {})
    assert "saved checkout: historical@aaaaaaaa" in text
    assert "version: historical@" not in text
    assert saved == {"current_branch": "historical", "current_sha": OLD}
