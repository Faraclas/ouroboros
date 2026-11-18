"""Local-only repository loss observations, never a removal verdict.

Unlike candidate retention, this does not preserve, pin, exclude scratch, or
compare with the serving body. Remote-tracking refs are dated local knowledge.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

from ouroboros.git_observation import host_git_env
from ouroboros.platform_layer import subprocess_hidden_kwargs
from ouroboros.utils import utc_now_iso


def inspect_repository(path: str) -> dict:
    """Inspect one selected directory; no fetch, content capture or sibling walk.

    Git may traverse this checkout for status. Other worktrees are listed from
    metadata only. Submodule/nested-repository contents and unreachable objects
    are not exhaustively inspected. Every failed probe remains a gap.
    """
    result = {"observed_at": utc_now_iso(), "path": path, "status": "unavailable",
              "arrangement": "unknown", "gaps": [], "facts": {},
              "remote_coverage": "Local remote-tracking refs only; no remote contacted or backup verified.",
              "coverage": "Selected checkout status and local Git references; no exhaustive nested repository, submodule or unreachable-object inspection.",
              "deletion_authorized": False}
    target = pathlib.Path(path).expanduser()
    try:
        if not target.is_dir():
            result["status"] = "not_directory" if target.exists() else "missing"
            return result
    except OSError as exc:
        result["gaps"].append(type(exc).__name__)
        return result
    # Do not let inherited GIT_DIR/INDEX/WORK_TREE redirect the selected target.
    env = host_git_env({k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")

    def git(*args, optional=False):
        try:
            proc = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                 "-C", str(target), *args], env=env, capture_output=True,
                timeout=10, **subprocess_hidden_kwargs())
            if proc.returncode == 0:
                return os.fsdecode(proc.stdout)
            if not optional:
                result["gaps"].append(f"{args[0]} failed (exit {proc.returncode})")
        except (OSError, subprocess.SubprocessError) as exc:
            result["gaps"].append(f"{args[0]}: {type(exc).__name__}")
        return None

    probe = git("rev-parse", "--is-bare-repository", optional=True)
    if probe is None:
        # Failure isn't necessarily a non-repository (permissions, corrupt Git,
        # missing executable, timeout). Only Git's explicit diagnostic says so.
        try:
            p = subprocess.run(["git", "-C", str(target), "rev-parse", "--git-dir"],
                               env=env, capture_output=True, timeout=10,
                               **subprocess_hidden_kwargs())
            if p.returncode and b"not a git repository" in p.stderr.lower():
                result["status"] = "not_repository"
            else:
                result["gaps"].append("Repository identity could not be inspected")
        except (OSError, subprocess.SubprocessError) as exc:
            result["gaps"].append(type(exc).__name__)
        return result
    bare = probe.strip() == "true"
    git_dir = git("rev-parse", "--absolute-git-dir")
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    root = git("rev-parse", "--show-toplevel") if not bare else None
    # rev-parse appends one LF; strip() would also erase valid path bytes.
    root = root.removesuffix('\n') if root is not None else str(target.resolve()) if bare else None
    inside = bool(not bare and root and pathlib.Path(root).resolve() != target.resolve())
    result.update(status="repository", arrangement="bare" if bare else "inside_repository" if inside else
                  "linked" if git_dir and common and git_dir.strip() != common.strip() else "main")
    if inside:
        result["coverage"] = "Checkout counts cover the selected subdirectory only; references, stashes and worktree metadata describe its enclosing repository. No other checkout contents inspected."
    facts = result["facts"]
    facts.update(root=root,
                 git_dir=git_dir.removesuffix('\n') if git_dir else None,
                 common_dir=common.removesuffix('\n') if common else None)
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", optional=True)
    head = git("rev-parse", "--verify", "HEAD", optional=True)
    facts.update(branch=branch.strip() if branch else None, head=head.strip() if head else None)
    if not head:
        ref = git("symbolic-ref", "--quiet", "HEAD", optional=True)
        refs_for_head = git("for-each-ref", "--format=%(refname)", ref.strip()) if ref else None
        if not ref or refs_for_head is None or refs_for_head.strip():
            result["gaps"].append("HEAD unavailable; unborn state not established")
    status = None if bare else git("-c", "status.renames=false", "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none", "--", ".")
    if bare:
        facts.update(uncommitted=None, untracked=None, ignored=None)
    elif status is not None:
        # Disabling rename detection avoids the extra pathname record in -z.
        rows = status.split("\0") if status is not None else []
        facts.update(uncommitted=sum(bool(row) and not row.startswith("??") for row in rows) if status is not None else None,
                     untracked=sum(row.startswith("??") for row in rows) if status is not None else None)
        ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--", ".")
        facts["ignored"] = len([x for x in ignored.split("\0") if x]) if ignored is not None else None
    else:
        facts.update(uncommitted=None, untracked=None, ignored=None)
    refs = git("for-each-ref", "--format=%(refname)", "refs/remotes")
    branches = git("for-each-ref", "--format=%(refname:short)%09%(upstream)", "refs/heads")
    facts["branches_without_upstream"] = (sum(not line.partition("\t")[2] for line in branches.splitlines())
                                          if branches is not None else None)
    facts["remote_tracking_refs"] = len(refs.splitlines()) if refs is not None else None
    local = git("rev-list", "--count", "--branches", *( ["HEAD"] if head else []), "--not", "--remotes") if refs is not None else None
    facts["commits_not_in_remote_tracking"] = int(local.strip()) if local is not None else None
    stash = git("for-each-ref", "--format=%(refname)", "refs/stash")
    stashes = git("reflog", "show", "--format=%H", "refs/stash") if stash else ""
    facts["stashes"] = len(stashes.splitlines()) if stashes is not None and stash is not None else None
    worktrees = git("worktree", "list", "--porcelain", "-z")
    facts["worktrees"] = [field[9:] for field in worktrees.split('\0') if field.startswith("worktree ")] if worktrees is not None else None
    facts["counts_note"] = "Untracked/ignored directories count as entries, not recursive file totals. Bare repositories have no checkout status."
    result["status"] = "partial" if result["gaps"] else "repository"
    result["warning"] = ("Before any future deletion, local changes or unknown coverage need a warning and extra confirmation. "
                         "A human or agent may decide to proceed within authorized scope or pause. This inspection grants neither choice nor deletion authority.")
    return result
