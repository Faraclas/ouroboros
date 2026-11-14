"""A fresh owner-door turn shows the checker the owner's recent words in its own room.

The turn itself carries only its newest message, so approval given earlier in the same
room (before a restart or stop) never reached Safety. The host supplies those words from
the retained room record, labelled, bounded and apart from the corpus that grounds the run.
"""
import json
from types import SimpleNamespace

from ouroboros import dialogue_store as ds
from ouroboros.safety import _build_check_prompt

ROOM, OTHER = 94178224, 55


def _row(text, seq, *, chat_id=ROOM, direction="in", **extra):
    return {"ts": f"2026-11-12T21:{seq:02d}:00+00:00", "direction": direction, "chat_id": chat_id,
            "text": text, "client_message_id": f"m{seq}", "task_id": "", **extra}


def _ctx(tmp_path, *, owner=True, chat_id=ROOM):
    meta = {"origin_message_ref": {"chat_id": chat_id, "client_message_id": "m40",
                                   "ts": "2026-11-13T13:44:00+00:00", "text_sha256": "x"}} if owner else {}
    ctx = SimpleNamespace(task_id="t1", task_metadata=meta, drive_root=tmp_path, budget_drive_root=tmp_path,
                          task_contract={}, current_chat_id=chat_id, messages=[])
    ctx._owner_directives = [{"source": "initial_user", "content": "ok. why are you not doing anything"}]
    return ctx


def _evidence(ctx):
    prompt = _build_check_prompt("start_task", {"objective": "finish Costs"}, ctx=ctx)
    return json.JSONDecoder().raw_decode(prompt.split("Host evidence (provenance is not consent):\n", 1)[1])[0]


def _seed(tmp_path):
    ds.append(tmp_path, "chat", _row("yes, definitely, separate extension activity from LLM calls", 26))
    ds.append(tmp_path, "chat", _row("Costs plan agreed.", 27, direction="out"))
    ds.append(tmp_path, "chat", _row("other room approval", 28, chat_id=OTHER))
    ds.append(tmp_path, "chat", _row("system notice", 29, system_type="reminder"))
    ds.append(tmp_path, "chat", _row("ok. why are you not doing anything", 40))


def test_owner_turn_sees_recent_room_owner_words(tmp_path):
    _seed(tmp_path)
    occasion = _evidence(_ctx(tmp_path))["occasion"]
    words = [r["text"] for r in occasion["earlier_room_owner_messages"]]
    assert words == ["yes, definitely, separate extension activity from LLM calls"]
    assert occasion["earlier_room_owner_messages"][0]["ts"].startswith("2026-11-12T21:26")
    # a short owner answer is read against the agent's last reply in the room, labelled as context
    assert occasion["agent_last_reply_in_room"]["text"] == "Costs plan agreed."
    # the run's own corpus is untouched; these rows are context, not retained directives
    assert [s["source"] for s in occasion["sources"]] == ["initial_user"]


def test_non_owner_turns_and_empty_rooms_get_nothing(tmp_path):
    _seed(tmp_path)
    assert "earlier_room_owner_messages" not in _evidence(_ctx(tmp_path, owner=False))["occasion"]
    assert "earlier_room_owner_messages" not in _evidence(_ctx(tmp_path, chat_id=777))["occasion"]


def test_room_words_are_bounded(tmp_path):
    for i in range(20):
        ds.append(tmp_path, "chat", _row(f"msg {i} " + "x" * 5000, i))
    rows = _evidence(_ctx(tmp_path))["occasion"]["earlier_room_owner_messages"]
    assert len(rows) == 8 and all(len(r["text"]) <= 2000 for r in rows)
    assert rows[-1]["text"].startswith("msg 19")
