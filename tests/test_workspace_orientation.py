"""Workspace orientation describes defaults without authoring task authority."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ouroboros.gateway.tasks import _compose_task_text
from ouroboros.workspace_admission import compose_workspace_block


@pytest.mark.parametrize("surface", ["gateway", "schedule", "promotion"])
@pytest.mark.parametrize("objective", [
    "Approved core delivery: use an isolated body candidate, qualify and install.",
    "External-only delivery: do not read or modify the Ouroboros system repository.",
])
def test_workspace_briefing_preserves_scope(surface, objective, tmp_path, monkeypatch):
    from supervisor import queue, workers, schedule_occurrence, worker_promotion
    from ouroboros import workspace_admission, headless

    folder = tmp_path / "workspace"
    folder.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    preflight = {"workspace_root": str(folder), "error": "snapshot unavailable"}
    monkeypatch.setattr(workspace_admission, "bounded_workspace_preflight", lambda _: deepcopy(preflight))
    monkeypatch.setattr(headless, "prepare_task_drive", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.projects_registry.update_project", lambda *_a, **_k: None)
    monkeypatch.setattr(workers, "DRIVE_ROOT", data)
    monkeypatch.setattr(workers, "REPO_DIR", tmp_path / "body")
    monkeypatch.setattr(queue, "DRIVE_ROOT", data)
    contract = {"objective": objective, "constraints": "No network; preserve owner data.",
                "resource_policy": {"allowed_origins": []}, "disabled_tools": ["web_search"]}
    task = {"id": "orientation", "text": objective, "project_id": "room",
            "metadata": {}, "task_contract": deepcopy(contract),
            "_project_admission": {"project": {"working_dir": str(folder)}}}
    if surface == "gateway":
        task["text"] = _compose_task_text(objective, workspace_root=folder,
            workspace_mode="external", memory_mode="forked", workspace_preflight=preflight,
            attachments=[])
    elif surface == "schedule":
        assert schedule_occurrence._bind(task, str(folder)) is None
        assert task["workspace_root"] == str(folder)
    else:
        monkeypatch.setattr(workspace_admission, "resolve_room_workspace", lambda **_: (str(folder), ""))
        evt = {"workspace_root": str(folder)}
        assert worker_promotion._admit_promoted_workspace(evt, SimpleNamespace(), task,
            pid="room", tid=task["id"]) is None
        assert task["workspace_root"] == str(folder)
    assert task["task_contract"] == contract
    text = task["text"]
    assert text.startswith(objective)
    block = text.split("[HEADLESS_WORKSPACE]", 1)[1]
    assert "default target" in block
    assert "not the Ouroboros system repo" not in block
    assert "does not grant access or override" in block
    assert "explicit root/cwd" in block
    assert f"workspace_root: {folder}" in block
    assert "snapshot unavailable" in block
    assert text.endswith("[END_HEADLESS_WORKSPACE]")


def test_orientation_has_no_objective_classifier(tmp_path):
    block = compose_workspace_block(workspace_root=tmp_path, workspace_mode="external",
        memory_mode="forked", workspace_preflight={})
    assert "default target" in block
    assert "does not grant access or override" in block
