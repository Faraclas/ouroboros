"""Local disposable Git consumers for read-only loss observations."""
import os
import subprocess

from ouroboros.repository_risk import inspect_repository


def git(path, *args):
    p = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@local",
                            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@local"})
    return p.stdout.decode().strip()


def repo(tmp_path):
    path = tmp_path / "repo"; path.mkdir(); git(path, "init", "-q")
    (path / "file").write_text("one")
    git(path, "add", "."); git(path, "commit", "-qm", "initial")
    return path


def test_local_unique_work_and_no_network(tmp_path):
    path = repo(tmp_path)
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    (path / "file").write_text("two"); git(path, "commit", "-qam", "local")
    (path / "file").write_text("three"); git(path, "stash", "push", "-m", "saved")
    (path / "file").write_text("four"); (path / "new").write_text("unique")
    (path / ".gitignore").write_text("ignored/\n"); (path / "ignored").mkdir()
    (path / "ignored" / "saved").write_text("important")
    git(path, "remote", "add", "origin", "https://invalid.invalid/no-network")
    before = (path / ".git" / "index").read_bytes()
    observed = inspect_repository(str(path))
    assert observed["status"] == "repository", observed
    f = observed["facts"]
    assert f["uncommitted"] == 1 and f["untracked"] == 2 and f["ignored"] == 1
    assert f["stashes"] == 1 and f["commits_not_in_remote_tracking"] == 1
    assert f["branches_without_upstream"] == 1
    assert before == (path / ".git" / "index").read_bytes()
    assert not observed["deletion_authorized"] and "no remote contacted" in observed["remote_coverage"]


def test_arrangements_unborn_detached_and_missing(tmp_path):
    path = repo(tmp_path)
    linked = tmp_path / "linked"; git(path, "worktree", "add", "--detach", str(linked))
    observed = inspect_repository(str(linked))
    assert observed["arrangement"] == "linked" and observed["facts"]["branch"] is None
    assert len(observed["facts"]["worktrees"]) == 2
    bare = tmp_path / "bare"; git(path, "clone", "--bare", str(path), str(bare))
    observed = inspect_repository(str(bare))
    assert observed["arrangement"] == "bare" and observed["facts"]["uncommitted"] is None
    empty = tmp_path / "empty"; empty.mkdir(); git(empty, "init", "-q")
    observed = inspect_repository(str(empty))
    assert observed["facts"]["head"] is None and observed["facts"]["commits_not_in_remote_tracking"] == 0
    assert inspect_repository(str(tmp_path / "absent"))["status"] == "missing"
    plain = tmp_path / "plain"; plain.mkdir()
    assert inspect_repository(str(plain))["status"] == "not_repository"


def test_probe_failure_is_not_clean(tmp_path, monkeypatch):
    path = repo(tmp_path)
    original = subprocess.run
    def fail_status(cmd, **kwargs):
        if "status" in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        return original(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", fail_status)
    observed = inspect_repository(str(path))
    assert observed["status"] == "partial" and observed["facts"]["uncommitted"] is None
    assert observed["gaps"]


def test_no_remote_count_and_inside_repository_scope(tmp_path):
    path = repo(tmp_path)
    observed = inspect_repository(str(path))
    assert observed['facts']['remote_tracking_refs'] == 0
    assert observed['facts']['commits_not_in_remote_tracking'] == 1
    subdir = path / 'build'; subdir.mkdir()
    (path / 'file').write_text('outside selected subtree')
    (subdir / 'output').write_text('selected')
    inside = inspect_repository(str(subdir))
    assert inside['arrangement'] == 'inside_repository'
    assert inside['facts']['uncommitted'] == 0 and inside['facts']['untracked'] == 1
    assert 'selected subdirectory only' in inside['coverage']
