"""Saved primary model, Context, Effort and Subagents of each room.

Projects copy Main once, at creation or migration. Reads stay read-only; every
settings writer freezes legacy rooms before changing Main. Registry writers take
their Main snapshot before the registry lock, never acquiring settings under it.
Actor lifetimes and room resolution belong to the task-settings owner.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping

from ouroboros.context_mode_compat import normalize_context_mode, owner_declared_low
from ouroboros.settings_scales import EFFORT_RANGE_KEYS, effort_range, normalize_subagents_mode

COGNITION_KEYS = ("OUROBOROS_CONTEXT_MODE", *EFFORT_RANGE_KEYS, "OUROBOROS_SUBAGENTS_MODE")


class PrimaryModelConflict(ValueError):
    """The editor's expected route no longer names this room's saved route."""


def cognition_from_settings(settings: Mapping[str, Any]) -> dict:
    return {"context_mode": normalize_context_mode(settings.get("OUROBOROS_CONTEXT_MODE", "max")),
            "effort_range": effort_range(settings),
            "primary_model": primary_from_settings(settings),
            "subagents_mode": normalize_subagents_mode(settings.get("OUROBOROS_SUBAGENTS_MODE"))}


def primary_from_settings(settings: Mapping[str, Any]) -> dict:
    from ouroboros.model_slots import MODEL_ACCOUNTS_KEY, model_role_option
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS
    from ouroboros.provider_models import provider_for_model
    from ouroboros.server_runtime import apply_runtime_provider_defaults
    settings, _changed, _keys = apply_runtime_provider_defaults(dict(settings))
    model = str(settings.get("OUROBOROS_MODEL") or SETTINGS_DEFAULTS["OUROBOROS_MODEL"]).strip()
    local = str(settings.get("USE_LOCAL_MAIN", "false")).lower() in ("true", "1")
    pin = model_role_option(MODEL_ACCOUNTS_KEY, "main", settings=dict(settings))
    return {"model": model, "use_local": local,
            "credential_profile_id": str(pin or "") if not local and provider_for_model(model) == "claudexor" else ""}


def normalize_primary(value: Any) -> dict:
    """Validate intent, not availability: catalogs are suggestions, never allowlists."""
    if (not isinstance(value, Mapping) or set(value) != {"model", "use_local", "credential_profile_id"}
            or not isinstance(value.get("model"), str) or not value["model"].strip()
            or any(ord(char) < 32 for char in value["model"])
            or not isinstance(value.get("use_local"), bool)
            or not isinstance(value.get("credential_profile_id"), str)):
        raise ValueError("Choose a model, local-runtime flag and account (empty means Auto).")
    from ouroboros.model_slots import apply_model_role_override
    primary = {"model": value["model"].strip(), "use_local": value["use_local"],
               "credential_profile_id": value["credential_profile_id"].strip()}
    apply_model_role_override({}, role="main", **primary)  # reuse the route/account contract, no write
    return primary


def cognition_overrides(cognition: Mapping[str, Any]) -> dict:
    """The non-secret room values; snapshot callers retain only Context/Effort."""
    effort = cognition["effort_range"]
    return {"OUROBOROS_CONTEXT_MODE": cognition["context_mode"],
            "OUROBOROS_SUBAGENTS_MODE": normalize_subagents_mode(cognition.get("subagents_mode")),
            **{key: effort[field] for key, field in zip(EFFORT_RANGE_KEYS, ("min", "recommended", "max"))}}


def main_cognition(settings: Mapping[str, Any] | None = None) -> dict:
    from ouroboros import config
    from ouroboros.settings_integrity import SETTINGS_ENV_LOCK

    settings = config.load_settings() if settings is None else settings
    with SETTINGS_ENV_LOCK:
        projected = dict(os.environ)
        config.apply_settings_to_env(settings, environ=projected)
    cognition = cognition_from_settings(projected)
    # A retired auto-Low view is not an owner choice to copy into a new room.
    if cognition["context_mode"] == "low" and not owner_declared_low(projected.get("OUROBOROS_CONTEXT_MODE_AUTO_LOW")):
        cognition["context_mode"] = "max"
    return cognition


def project_cognition(project: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict:
    """Legacy rows use the pre-migration baseline; saved choices never follow Main."""
    saved = project.get("cognition")
    if not isinstance(saved, Mapping):
        return {**copy.deepcopy(dict(baseline)), "subagents_mode": "auto"}
    current = cognition_from_settings(cognition_overrides(saved))
    current["primary_model"] = normalize_primary(saved.get("primary_model", baseline["primary_model"]))
    return current


def read_primary(drive_root: Any, project_id: str) -> dict:
    """The hot model reader needs no global projection for an already seeded row."""
    from ouroboros.projects_registry import get_project

    project = get_project(drive_root, project_id, strict=True)
    if project is None:
        raise ValueError(f"Project {project_id!r} is unavailable")
    saved = project.get("cognition") or {}
    return normalize_primary(saved["primary_model"]) if "primary_model" in saved else main_cognition()["primary_model"]


def read_cognition(drive_root: Any, project_id: str = "", *, settings: Mapping[str, Any] | None = None) -> dict:
    baseline = main_cognition(settings)  # settings before registry: the write order, too
    if not project_id:
        return baseline
    from ouroboros.projects_registry import get_project

    project = get_project(drive_root, project_id, strict=True)
    if project is None:
        raise ValueError(f"Project {project_id!r} is unavailable")
    return project_cognition(project, baseline)


def _seed_rows(data: dict, baseline: Mapping[str, Any]) -> bool:
    changed = False
    for row in data["projects"]:
        if "cognition" not in row:
            row["cognition"] = {**copy.deepcopy(dict(baseline)), "subagents_mode": "auto"}
            changed = True
        elif isinstance(row["cognition"], dict) and "primary_model" not in row["cognition"]:
            row["cognition"]["primary_model"] = copy.deepcopy(baseline["primary_model"])
            changed = True
    return changed


def ensure_project_cognition(drive_root: Any, *, baseline: Mapping[str, Any] | None = None) -> None:
    """Freeze existing rooms before Main changes, or during ordinary boot reconcile."""
    from ouroboros import projects_registry as registry

    path = registry._registry_path(drive_root)
    if not path.exists():
        return
    baseline = main_cognition() if baseline is None else baseline
    with registry._file_write_lock(path):
        data = registry._load(drive_root, strict=True, project_id="")
        if _seed_rows(data, baseline):
            registry._save(drive_root, data)


def preserve_project_cognition(settings: Mapping[str, Any], settings_path: Path) -> None:
    """Settings-write prologue; the caller already owns the settings file lock."""
    from ouroboros import config

    current = main_cognition(config.load_settings_lock_held())
    if current != cognition_from_settings(settings):
        # The file's parent is its actual data root, including isolated readers
        # that replace SETTINGS_PATH without replacing the module's DATA_DIR.
        ensure_project_cognition(settings_path.parent, baseline=current)


def update_project_cognition(drive_root: Any, project_id: str, *, context_mode: str | None = None,
                             effort_range: Mapping[str, str] | None = None,
                             subagents_mode: str | None = None, primary_model: Mapping | None = None,
                             expected_primary_model: Mapping | None = None) -> tuple[dict, dict]:
    """Update just this room's decision under the registry's existing write lock."""
    from ouroboros import projects_registry as registry

    baseline = main_cognition()
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=project_id)
        project = next((row for row in data["projects"] if row["id"] == project_id
                        and row.get("lifecycle") == registry.PROJECT_ACTIVE), None)
        if project is None:
            raise ValueError(f"Project {project_id!r} is unavailable")
        previous = project_cognition(project, baseline)
        if expected_primary_model is not None and previous["primary_model"] != normalize_primary(expected_primary_model):
            raise PrimaryModelConflict("Primary model changed elsewhere. Refresh before applying your choice.")
        current = copy.deepcopy(previous)
        if primary_model is not None:
            current["primary_model"] = normalize_primary(primary_model)
        if context_mode is not None:
            current["context_mode"] = context_mode
        if effort_range is not None:
            current["effort_range"] = dict(effort_range)
        if subagents_mode is not None:
            current["subagents_mode"] = subagents_mode
        project["cognition"] = current
        registry._save(drive_root, data)
    return previous, current


def request_project_id(body: Mapping[str, Any]) -> str:
    """Exact optional room identity; never sanitize a different id into this one."""
    from ouroboros.project_facts import explicit_project_id_ok

    value = body.get("project_id", "")
    if not isinstance(value, str) or (value and not explicit_project_id_ok(value)):
        raise ValueError("project_id must name an existing Project, or be empty for Main")
    return value
