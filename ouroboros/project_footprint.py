"""Project-scoped first observations and explicit footprint declarations.

The Project registry owns this history; resource associations remain mutable
context. No tree snapshot, copy, background tracking, ownership or undo policy.
"""
from __future__ import annotations

import pathlib
import uuid

from ouroboros.utils import utc_now_iso

COVERAGE = ("Recorded targets only, not a complete filesystem inventory. First observation is not a project-start baseline. "
            "Opaque shell effects, shared caches/packages and later foreign changes are not automatically attributed. "
            "Records grant no ownership, access, backup or deletion authority.")


def first_observation(path: str, *, source: str, origin: str = "unknown", task_id: str = "", note: str = "", inspect: bool = True) -> dict:
    from ouroboros.project_management import observe_resource
    return {"id": uuid.uuid4().hex, "path": path, "recorded_at": utc_now_iso(),
            "source": source, "origin": origin, "task_id": task_id, "note": note,
            "observation": observe_resource(path) if inspect else None}


def entries(row: dict) -> list[dict]:
    value = row.get("footprint", [])
    if not isinstance(value, list) or any(not isinstance(item, dict) or
            not isinstance(item.get("path"), str) or not item.get("id") for item in value):
        raise ValueError("Project footprint history is malformed")
    return [dict(item) for item in value]


def append_observation(row: dict, record: dict) -> None:
    """Keep first involvement per spelling/source; never overwrite older facts."""
    history = entries(row)
    if not any(item["path"] == record["path"] and item.get("source") == record["source"] for item in history):
        row["footprint"] = [*history, record]


def record_target(drive_root, project_id: str, path: str, *, task_id: str, origin: str = "unknown", note: str = "", inspect: bool = True, observation_error: str = "") -> dict:
    """An attributed declaration, not host proof that this project created a path.

    Call before touching an existing particular target for its initial Git/path
    observation, or declare an explicitly generated output/environment afterwards.
    No content is copied; a missing before-observation cannot be reconstructed.
    """
    from ouroboros import projects_registry as registry
    from ouroboros.project_management import _resource_path, ProjectManagementError
    path = _resource_path(path)
    if origin not in {"unknown", "pre_existing", "project_created"} or not isinstance(note, str) or len(note) > 2000:
        raise ProjectManagementError("invalid_resource", "origin must be unknown, pre_existing or project_created; note <=2000 characters")
    record = first_observation(path, source="agent_declaration", origin=origin, task_id=task_id, note=note, inspect=inspect)
    if observation_error:
        record['observation_error'] = observation_error
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=project_id)
        row = next((p for p in data["projects"] if p["id"] == project_id), None)
        if row is None:
            raise ProjectManagementError("project_not_found", "Unknown Project", 404)
        if row["lifecycle"] == registry.PROJECT_DELETING:
            raise ProjectManagementError("project_closing", "Project is still closing", 409)
        # Declarations are history, including corrections; duplicates of the same
        # exact declaration are harmless, and do not acquire a new baseline.
        history = entries(row)
        same = next((item for item in history if all(item.get(k) == record.get(k) for k in
                    ("path", "source", "origin", "task_id", "note"))), None)
        if same:
            return same
        row["footprint"] = [*history, record]
        registry._save(drive_root, data)
    return record


def record_workspace_creation(drive_root, project_id: str, path: str, base_sha: str) -> None:
    """Existing genesis producer records its new directory, without a new copy kind."""
    record = first_observation(path, source="genesis_producer", origin="project_created")
    record["base_sha"] = base_sha
    store_observation(drive_root, project_id, record)


def record_clone_creation(drive_root, project_id: str, path: str) -> None:
    """Called only by successful clone producers, never by authored provenance."""
    store_observation(drive_root, project_id, first_observation(path, source="clone_producer", origin="cloned"))


def store_observation(drive_root, project_id: str, record: dict) -> None:
    """Commit an existing producer's observation; never probe under the writer lock."""
    from ouroboros import projects_registry as registry
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=project_id)
        row = next((p for p in data["projects"] if p["id"] == project_id), None)
        if row is not None:
            append_observation(row, record)
            registry._save(drive_root, data)


def view(row: dict) -> dict:
    """Historical records only. Legacy locations get explicit unknown provenance."""
    from ouroboros.project_management import resources_for_project
    history = entries(row)
    paths = [item["path"] for item in resources_for_project(row)]
    if row.get("working_dir"):
        paths.append(row["working_dir"])
    unknown = list(dict.fromkeys(path for path in paths if not any(item["path"] == path for item in history)))
    return {"entries": history, "legacy_unknown_paths": unknown, "coverage": COVERAGE}


def target_path(drive_root, project_id: str, resource_id: str) -> str:
    """Resolve one stored target without probing it."""
    from ouroboros.project_management import _project, resources_for_project, ProjectManagementError
    row = _project(drive_root, project_id)
    targets = [*resources_for_project(row), *entries(row)]
    if resource_id.startswith('generated:'):
        targets += generated_targets(drive_root, project_id)['targets']
    if row.get("working_dir"):
        targets.append({"id": "starting_folder", "path": row["working_dir"]})
    target = next((item for item in targets if item["id"] == resource_id), None)
    if target is None:
        raise ProjectManagementError("resource_not_found", "Unknown recorded resource", 404)
    return target["path"]


def inspect_target(drive_root, project_id: str, resource_id: str) -> dict:
    """Select a recorded target by id, never an arbitrary browser-supplied path."""
    from ouroboros.repository_risk import inspect_repository
    return {"resource_id": resource_id, **inspect_repository(target_path(drive_root, project_id, resource_id))}


def generated_targets(drive_root, project_id: str) -> dict:
    """Join existing copy/task records, not the contents of their directories.

    This is an explicit inspection read, not a hot poll. It reads only project
    task bodies plus the copy-record directory. No legacy import/retirer runs.
    Stored task facts survive scratch-drive retirement; absent facts stay gaps.
    """
    import json
    from ouroboros import projects_registry as registry, task_store
    targets, gaps = [], []
    bound = {tid for tid, binding in registry._load_bindings(drive_root)["bindings"].items()
             if isinstance(binding, dict) and binding.get("project_id") == project_id}
    tids = set(task_store.newest_ids(drive_root, where="project_id=?", params=(project_id,))) | bound
    documents = task_store.documents_by_ids(drive_root, tids)
    for tid in sorted(tids):
        task = documents.get(tid)
        if not task:
            gaps.append(f"Task {tid}: stored facts unavailable")
            continue
        for key in ("child_drive_root", "headless_child_drive_root", "workspace_root"):
            if task.get(key):
                targets.append({"path": task[key], "source": f"task.{key}", "task_id": tid,
                                "origin": "task_associated", "recorded_at": task.get("ts", "")})
        for artifact in task.get("artifacts") or []:
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                targets.append({"path": artifact["path"], "source": "task.artifacts", "task_id": tid,
                                "origin": "recorded_output", "recorded_at": task.get("ts", "")})
    records = pathlib.Path(drive_root) / "state" / "private_copies"
    try:
        for file in records.glob("*.json"):
            try:
                row = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("not an object")
                if row.get("task_id") in tids or row.get("parent_task_id") in tids or row.get("task_id") == f"project_{project_id}":
                    targets.append({"path": row.get("path", ""), "source": "private_copy_record",
                                    "task_id": row.get("task_id", ""), "origin": row.get("kind", "recorded_copy"),
                                    "recorded_at": row.get("created_at", ""), "base_sha": row.get("base_sha", "")})
            except (OSError, ValueError, UnicodeError) as exc:
                gaps.append(f"Copy record {file.name}: {type(exc).__name__}; project association unknown")
    except OSError as exc:
        gaps.append(f"Copy records unavailable: {type(exc).__name__}")
    import hashlib
    for target in targets:
        identity = '\0'.join(str(target.get(key, '')) for key in ('path', 'source', 'task_id'))
        target['id'] = 'generated:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
    return {"targets": targets, "gaps": gaps,
            "coverage": "Existing project task/artifact and private-copy records only; no target contents read. Retired copy records and undeclared shell-created environments may be absent. Task-associated paths are not creation or disposability proof."}
