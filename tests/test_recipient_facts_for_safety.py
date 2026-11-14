"""A task-to-task message reaches the checker with host facts about its recipient.

The call carries only a task id; without recipient facts the checker cannot tell the room's
own engineer from a stranger. The facts are host-sourced, bounded, and grant nothing.
"""
import json
from types import SimpleNamespace

from ouroboros import peer_roster
from ouroboros.safety import _build_check_prompt


def _subject(tool, args, tmp_path, monkeypatch, row):
    monkeypatch.setattr(peer_roster, "host_listed_independent_root", lambda drive, tid: row)
    ctx = SimpleNamespace(task_id="t1", root_task_id="root1", task_metadata={}, drive_root=tmp_path,
                          budget_drive_root=tmp_path, task_contract={}, messages=[])
    prompt = _build_check_prompt(tool, args, ctx=ctx)
    return json.JSONDecoder().raw_decode(prompt.split("Host evidence (provenance is not consent):\n", 1)[1])[0]["subject"]


def test_listed_recipient_facts_reach_checker(tmp_path, monkeypatch):
    row = {"task_id": "abc", "title": "Costs", "project_id": "viper-interface", "status": "running",
           "origin": {"owner_ingress": True}, "drive_root": "/secret/path", "tails": ["x"]}
    recipient = _subject("tell_task", {"task_id": "abc", "note": "hi"}, tmp_path, monkeypatch, row)["recipient"]
    assert recipient["host_listed"] is True and recipient["project_id"] == "viper-interface"
    assert recipient["same_task_tree"] is False
    assert "drive_root" not in recipient and "tails" not in recipient


def test_unlisted_recipient_is_stated_unknown_not_guessed(tmp_path, monkeypatch):
    recipient = _subject("forward_to_worker", {"task_id": "zzz", "message": "m"}, tmp_path, monkeypatch, None)["recipient"]
    assert recipient["host_listed"] is False and "not established" in recipient["note"]


def test_other_tools_get_no_recipient(tmp_path, monkeypatch):
    assert "recipient" not in _subject("start_task", {"objective": "x"}, tmp_path, monkeypatch, {"task_id": "abc"})
