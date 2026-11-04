"""Project catalog, reopen and contextual resource associations.

The registry is the sole durable source. Resources describe useful paths; they
never grant access, claim ownership, change cwd or participate in admission.
Only explicit detail reads observe the filesystem/Git. No cleanup or setup.
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import uuid
from typing import Any

from ouroboros import projects_registry as registry
from ouroboros.git_observation import host_git_env
from ouroboros.platform_layer import subprocess_hidden_kwargs
from ouroboros.utils import utc_now_iso


class ProjectManagementError(ValueError):
    def __init__(self, reason: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.reason, self.status_code = reason, status_code


def _project(drive_root: Any, project_id: str) -> dict:
    row = registry.get_reserved_project(drive_root, project_id, strict=True)
    if row is None:
        raise ProjectManagementError("project_not_found", f"Unknown project: {project_id}", 404)
    return row


def resources_for_project(row: dict) -> list[dict]:
    # Absence means legacy projection; an explicit empty list means detached.
    if "resources" not in row:
        path = row.get("working_dir") or ""
        return ([{"id": "default", "path": path, "label": "Default working folder",
                  "added_at": str(row.get("created_at") or "")}] if path else [])
    resources = row["resources"]
    if (not isinstance(resources, list) or any(
            not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in
                                             ("id", "path", "label", "added_at"))
            or not item["id"] or not item["path"] for item in resources)):
        raise ValueError(f"Project {row['id']!r} has malformed contextual resources")
    if len({item["id"] for item in resources}) != len(resources):
        raise ValueError(f"Project {row['id']!r} has duplicate resource identities")
    return [dict(item) for item in resources]


def list_project_resources(drive_root: Any, project_id: str) -> list[dict]:
    """Return contextual paths, including closed rooms; never probe or grant them."""
    return resources_for_project(_project(drive_root, project_id))


def _view(row: dict) -> dict:
    return {**row, "lifecycle_label": {"active": "active", "deleting": "closing",
                                      "tombstoned": "closed"}[row["lifecycle"]],
            "resources": resources_for_project(row)}


def project_catalog(drive_root: Any, *, lifecycle: str = "all", q: str = "",
                    offset: int = 0, limit: int = 50) -> dict:
    """A paginated management view; ordinary sidebar/list semantics stay separate."""
    if lifecycle not in {"all", "active", "closed"}:
        raise ProjectManagementError("invalid_catalog", "lifecycle must be all, active or closed")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ProjectManagementError("invalid_catalog", "offset must be >=0; limit must be 1..200")
    needle = str(q).casefold().strip()
    rows = registry.list_reserved_projects(drive_root, strict=True)
    rows = [row for row in rows if (lifecycle == "all" or
            (row["lifecycle"] == registry.PROJECT_ACTIVE) == (lifecycle == "active"))
            and (not needle or needle in " ".join(str(row.get(key) or "") for key in
                                                 ("id", "name", "working_dir")).casefold())]
    total = len(rows)
    return {"projects": [_view(row) for row in rows[offset:offset + limit]],
            "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}


def reopen_project(drive_root: Any, project_id: str) -> dict:
    """Reactivate a fully closed room with new routing identity, never old work."""
    from supervisor.queue_schedules import schedule_transaction
    from supervisor.schedule_lifecycle import pause_project_schedules

    pid = registry.sanitize_project_id(project_id)
    with schedule_transaction(pathlib.Path(drive_root)), registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=pid)
        row = next((item for item in data["projects"] if item["id"] == pid), None)
        if row is None:
            raise ProjectManagementError("project_not_found", f"Unknown project: {project_id}", 404)
        if row["lifecycle"] == registry.PROJECT_DELETING:
            raise ProjectManagementError("project_closing", "The Project is still closing; wait for quiescence", 409)
        if row["lifecycle"] == registry.PROJECT_ACTIVE:
            return _view(row)
        resources_for_project(row)  # malformed context must not produce a half-reported transition
        # Pause before activation, including legacy tombstones; Close needs no schedule reads.
        paused = pause_project_schedules(pathlib.Path(drive_root), pid, row["chat_id"])
        if not paused["ok"]:
            raise ProjectManagementError("schedule_audit_incomplete", "Schedules paused; their audit publication is incomplete", 503)
        row.update(lifecycle=registry.PROJECT_ACTIVE, routing_incarnation=uuid.uuid4().hex,
                   routing_generation=row["routing_generation"] + 1, reopened_at=utc_now_iso(), delete_error="")
        registry._save(drive_root, data)
        return _view(row)


def _resource_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ProjectManagementError("invalid_resource", "A nonempty absolute path is required")
    text = path.strip()
    try:
        expanded = pathlib.Path(text).expanduser()
    except RuntimeError as exc:
        raise ProjectManagementError("invalid_resource", str(exc)) from exc
    if not expanded.is_absolute():
        raise ProjectManagementError("invalid_resource", "A resource path must be absolute (or start with ~)")
    # Store the authored spelling faithfully. No existence/association whitelist.
    return text


def _path_identity(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def _edit_resource(drive_root: Any, project_id: str, *, path: str = "", label: str = "",
                   resource_id: str = "", detach: bool = False) -> dict:
    pid = registry.sanitize_project_id(project_id)
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=pid)
        row = next((item for item in data["projects"] if item["id"] == pid), None)
        if row is None:
            raise ProjectManagementError("project_not_found", f"Unknown project: {project_id}", 404)
        if row["lifecycle"] == registry.PROJECT_DELETING:
            raise ProjectManagementError("project_closing", "The Project is still closing", 409)
        resources = resources_for_project(row)
        if detach:
            if not any(item["id"] == resource_id for item in resources):
                raise ProjectManagementError("resource_not_found", f"Unknown resource: {resource_id}", 404)
            resources = [item for item in resources if item["id"] != resource_id]
        else:
            if any(_path_identity(item["path"]) == _path_identity(path) for item in resources):
                return _view(row)  # same path in another room is independently valid
            resources.append({"id": uuid.uuid4().hex, "path": path, "label": label, "added_at": utc_now_iso()})
        row["resources"] = resources
        registry._save(drive_root, data)
        return _view(row)


def add_project_resource(drive_root: Any, project_id: str, path: str, label: str = "") -> dict:
    if not isinstance(label, str):
        raise ProjectManagementError("invalid_resource", "label must be a string")
    return _edit_resource(drive_root, project_id, path=_resource_path(path), label=label.strip())


def detach_project_resource(drive_root: Any, project_id: str, resource_id: str) -> dict:
    return _edit_resource(drive_root, project_id, resource_id=resource_id, detach=True)


def _git_observation(path: pathlib.Path) -> dict:
    result = {"status": "unavailable", "branch": "", "head": "", "root": ""}
    options = dict(capture_output=True, text=True, timeout=2,
                   env={**host_git_env(), "GIT_TERMINAL_PROMPT": "0"}, **subprocess_hidden_kwargs())
    try:
        # Bounded read-only Git probes handle linked worktrees and bare repos.
        probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree",
                                "--is-bare-repository", "--absolute-git-dir"],
                               **options)
        if probe.returncode:
            error = (probe.stderr or "Git probe failed").strip()
            if "not a git repository" in error.lower():
                return {**result, "status": "not_repository"}
            return {**result, "error": error}
        root = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                              **options)
        branch = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                **options)
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                              **options)
        return {"status": "repository", "root": root.stdout.strip() if root.returncode == 0 else str(path),
                "branch": branch.stdout.strip() if branch.returncode == 0 else "",
                "head": head.stdout.strip() if head.returncode == 0 else ""}
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "error": f"{type(exc).__name__}: {exc}"}


def observe_resource(path: str) -> dict:
    """Fresh read-only facts; missing and failed observation are not the same."""
    empty_git = {"status": "not_repository", "branch": "", "head": "", "root": ""}
    try:
        target = pathlib.Path(path).expanduser()
        mode = target.stat().st_mode
    except FileNotFoundError:
        return {"status": "missing", "kind": "unknown", "git": empty_git}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "kind": "unknown",
                "git": {**empty_git, "status": "unavailable"}, "error": f"{type(exc).__name__}: {exc}"}
    kind = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "unknown"
    return {"status": "available", "kind": kind,
            "git": _git_observation(target) if kind == "directory" else empty_git}


def project_detail(drive_root: Any, project_id: str) -> dict:
    row = _view(_project(drive_root, project_id))
    row["resources"] = [{**item, "observation": observe_resource(item["path"])} for item in row["resources"]]
    return row
