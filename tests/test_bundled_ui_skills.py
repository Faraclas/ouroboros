"""Real source-only UI seeds use existing admission; never migrate installed state."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys

import pytest

SEEDS = Path(__file__).resolve().parents[1] / "skills"
NAMES = ("copilot-credits", "project-orientation", "project-boards")


@pytest.fixture
def drive(tmp_path, monkeypatch):
    import ouroboros.config as config

    root = tmp_path / "data"
    (root / "skills" / "native").mkdir(parents=True)
    monkeypatch.setattr(config, "SETTINGS_PATH", root / "settings.json")
    monkeypatch.setattr(config, "get_auto_grant_enabled", lambda: False)
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "true")
    monkeypatch.delenv("OUROBOROS_SKILL_SOURCES", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("bundled offline admission attempted a network request")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    return root


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", NAMES)
def test_real_ui_seed_offline_admission_is_not_enablement_or_review_receipt(drive, name):
    from ouroboros.skill_builtin import install_builtin_skill
    from ouroboros.skill_loader import find_skill, skill_state_dir_path
    from ouroboros.skill_install_record import read_install_record

    result = install_builtin_skill(name, drive_root=drive, seed_dir=SEEDS)
    assert result["ok"] and result["basis"] == "trusted"
    assert result["auto_granted_keys"] == result["auto_granted_permissions"] == []
    target = drive / "skills" / "native" / name
    assert (target / ".seed-origin").is_file()
    assert not (target / ".self_authored.json").exists()
    assert (target / "plugin.py").read_bytes() == (SEEDS / name / "plugin.py").read_bytes()
    record = read_install_record(drive, name)
    assert record["source"]["id"] == "builtin" and record["official"] is True
    skill = find_skill(drive, name)
    assert skill.review.gate_for(skill.content_hash)["blocking_reason"] == "trusted_source"
    assert not skill.enabled
    assert not (skill_state_dir_path(drive, name) / "review.json").exists()
    assert not list(target.rglob("*.sqlite3"))


@pytest.mark.parametrize("name", NAMES)
def test_review_required_builtin_policy_is_not_overridden(drive, monkeypatch, name):
    from ouroboros.skill_builtin import install_builtin_skill
    from ouroboros.skill_loader import find_skill

    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "false")
    result = install_builtin_skill(name, drive_root=drive, seed_dir=SEEDS)
    assert result["ok"] and result["basis"] == "review"
    skill = find_skill(drive, name)
    assert not skill.review.gate_for(skill.content_hash)["executable_review"]
    assert not skill.enabled


@pytest.mark.parametrize("name", NAMES)
def test_same_name_external_payload_and_decisions_are_not_converted(drive, name):
    from ouroboros.skill_builtin import install_builtin_skill, _offer_default_builtins
    from ouroboros.skill_loader import discover_skill_identity, save_enabled, skill_state_dir_path
    import logging

    external = drive / "skills" / "external" / name
    shutil.copytree(SEEDS / name, external)
    list(discover_skill_identity(drive, name))  # establish any legacy identity baseline
    save_enabled(drive, name, False, actor="owner_ui")
    state = skill_state_dir_path(drive, name)
    (state / "preserved-owner-decision.json").write_text('{"fixture":"keep"}', encoding="utf-8")
    before_payload, before_state = files(external), files(state)
    result = install_builtin_skill(name, drive_root=drive, seed_dir=SEEDS)
    assert not result["ok"] and result["code"] == "name_taken"
    assert not (drive / "skills" / "native" / name).exists()
    assert files(external) == before_payload and files(state) == before_state
    # No new default-on upgrade offer repeatedly collides with these external copies.
    seed = drive.parent / "only-ui-seeds"
    seed.mkdir()
    shutil.copy(SEEDS / "builtin.json", seed / "builtin.json")
    for slug in NAMES:
        shutil.copytree(SEEDS / slug, seed / slug)
    marker = drive / "state" / "skill_starter.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"state": "done"}), encoding="utf-8")
    failures = []
    _offer_default_builtins(drive, seed, logging.getLogger("test.ui.builtin"), failures)
    assert failures == []
    assert files(external) == before_payload and files(state) == before_state

    # Negative control: exercise the real existing-install offer path, not its
    # fresh-install early return. A default-on name collision must be observed.
    catalog = json.loads((seed / "builtin.json").read_text(encoding="utf-8"))
    catalog["skills"] = [row for row in catalog["skills"] if row["name"] == name]
    catalog["skills"][0]["default_on"] = True
    (seed / "builtin.json").write_text(json.dumps(catalog), encoding="utf-8")
    _offer_default_builtins(drive, seed, logging.getLogger("test.ui.builtin"), failures)
    assert failures == [name]
    assert files(external) == before_payload and files(state) == before_state


def test_ui_catalog_is_recommended_not_an_existing_install_migration():
    rows = {r["name"]: r for r in json.loads((SEEDS / "builtin.json").read_text(encoding="utf-8"))["skills"]}
    for name in NAMES:
        assert rows[name]["recommended"] is True
        assert rows[name].get("default_on") is not True
        assert not (SEEDS / name / ".seed-origin").exists()
        assert not (SEEDS / name / ".self_authored.json").exists()


def test_external_board_database_and_exact_retry_receipts_survive_seed_collision(drive, monkeypatch):
    from ouroboros.skill_builtin import install_builtin_skill

    path = SEEDS / "project-boards" / "board_store.py"
    spec = importlib.util.spec_from_file_location("_bundled_ui_board_store", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    external = drive / "skills" / "external" / "project-boards"
    shutil.copytree(SEEDS / "project-boards", external)
    state = drive / "state" / "skills" / "project-boards"
    actor = {"surface": "interface", "label": "via Interface/API"}
    store = module.BoardStore(state, lambda pid: {"id": pid} if pid == "p" else None)
    request = {"project_id": "p", "title": "Saved idea", "request_id": "capture-original"}
    item = store.mutate("capture", request, actor)["item"]
    store.mutate("comment", {"item_id": item["id"], "text": "Keep this discussion", "request_id": "comment-original"}, actor)
    before = files(state)
    result = install_builtin_skill("project-boards", drive_root=drive, seed_dir=SEEDS)
    assert not result["ok"] and result["code"] == "name_taken"
    assert files(state) == before
    reopened = module.BoardStore(state, lambda pid: {"id": pid})
    duplicate = reopened.mutate("capture", request, actor)
    assert duplicate["duplicate"] is True and duplicate["item"]["id"] == item["id"]
    with closing(sqlite3.connect(state / "boards.sqlite3")) as conn:
        assert [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("items", "comments", "requests")] == [1, 1, 2]
