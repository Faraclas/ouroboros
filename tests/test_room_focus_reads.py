"""Room focus reads body focus only; validation and latest selection stay in Python."""
import contextlib
import json
from types import SimpleNamespace

import pytest

from ouroboros import memory_view as mv, task_store, store
from ouroboros.focus import normalize_focus
from tests._store_shared import seed_result, unreadable_result


def _focus(tid, text="Ready café", ts="2026-10-01T10:00:00+00:00"):
    return normalize_focus(text, {"reader": "get_task_result", "task_id": tid},
                           task_id=tid, authored_at=ts)


def test_directory_avoids_general_display_materialization(tmp_path, monkeypatch):
    seed_result(tmp_path, "only", {"focus": _focus("only"), "unrelated": "x" * 100_000})
    monkeypatch.setattr(mv.memory_inventory, "membership_facts", lambda _root: SimpleNamespace(bindings={}))
    def forbidden(*_args, **_kwargs):
        pytest.fail("focus selection must not assemble general display/card/handoff data")
    monkeypatch.setattr(task_store, "display_documents", forbidden)
    assert "Ready café" in mv._room_focus(tmp_path, {"room": [(None, {"task_id": "only"}, None)]}, {})["room"]


@pytest.mark.parametrize("value", [None, 1.25, 1, True, False, "text", [], [1.25], {}])
def test_focus_type_transport_and_full_objects(tmp_path, value):
    seed_result(tmp_path, "odd", {"focus": value})
    seed_result(tmp_path, "missing", {})
    full = {**_focus("good"), "source_handle": {"kind": "task_source", "root": "artifact_store",
            "path": "source_handles/context_checkpoints/retained.txt", "size": 123,
            "sha256": "a" * 64}, "extra": {"float": 1.25, "unicode": "café\u0000"}}
    seed_result(tmp_path, "good", {"task_id": "body-impostor", "focus": full})
    got = task_store.focus_objects(tmp_path, ["odd", "good", "missing", "absent", "good", ""])
    assert got == {"odd": value if isinstance(value, dict) else None, "missing": None, "good": full}


def test_focus_capture_releases_before_decode_and_ignores_cards_handoffs(tmp_path, monkeypatch):
    seed_result(tmp_path, "task", {"focus": _focus("task")})
    real_open = task_store.open_store
    real_capture = task_store.capture
    active = []
    @contextlib.contextmanager
    def opened(*args, **kwargs):
        with real_open(*args, **kwargs) as txn:
            active.append(True)
            try:
                yield txn
            finally:
                active.pop()
    def captured(*args, **kwargs):
        value = real_capture(*args, **kwargs)
        class Answer:
            def tuples(self):
                assert not active
                return value.tuples()
        return Answer()
    def forbidden(*_args, **_kwargs):
        pytest.fail("unrelated display projection")
    monkeypatch.setattr(task_store, "open_store", opened)
    monkeypatch.setattr(task_store, "capture", captured)
    monkeypatch.setattr(task_store, "_handoff_capture", forbidden)
    monkeypatch.setattr(task_store.store_task_cards, "select", forbidden)
    assert task_store.focus_objects(tmp_path, ["task"]) == {"task": _focus("task")}


def test_directory_union_offsets_ties_invalid_newer_and_actual_identity(tmp_path, monkeypatch):
    values = {"open": _focus("open", "older", "2026-10-01T09:00:00+00:00"),
              "note": _focus("note", "notes only"),
              "bound": _focus("bound", "offset latest", "2026-10-01T07:00:00-04:00"),
              "tie-a": _focus("tie-a", "tie a"), "tie-z": _focus("tie-z", "tie z"),
              "invalid": _focus("invalid", "invalid date", "bad date"),
              "foreign": _focus("someone-else", "foreign", "2099-01-01T00:00:00Z"),
              "bad-handle": {**_focus("bad-handle", "bad handle", "2099-01-01T00:00:00Z"), "source_handle": {}},
              "real": 1.25}
    for tid, focus in values.items():
        seed_result(tmp_path, tid, {"task_id": "impostor", "status": "completed", "focus": focus})
    monkeypatch.setattr(mv.memory_inventory, "membership_facts", lambda _root: SimpleNamespace(bindings={
        "bound": "room", "invalid": "room", "foreign": "room", "bad-handle": "room", "real": "room",
        "tie-a": "ties", "tie-z": "ties", "unrelated": "not-shown"}))
    rows = {"room": [(None, {"task_id": "open"}, None)], "ties": []}
    notes = {"notes": [{"task_id": "note"}]}
    result = mv._room_focus(tmp_path, rows, notes)
    assert set(result) == {"room", "notes", "ties"}
    assert "offset latest" in result["room"] and "notes only" in result["notes"] and "tie z" in result["ties"]
    # Compare against the complete previous consumer, not merely a hand-built expected line.
    objects = task_store.focus_objects
    monkeypatch.setattr(task_store, "focus_objects", lambda root, ids: {
        tid: json.loads(text).get("focus") for tid, text in task_store.display_documents(root, ids).items()})
    assert mv._room_focus(tmp_path, rows, notes) == result
    monkeypatch.setattr(task_store, "focus_objects", objects)


def test_roots_empty_freshness_and_schema_six(tmp_path, monkeypatch):
    from tests.test_store_task_cards import _schema_six
    legacy = {"task_id": "task", "status": "running", "_schema_version": 1, "focus": _focus("task")}
    _schema_six(tmp_path, monkeypatch, legacy)
    first = task_store.focus_objects(tmp_path, ["task"])
    assert first == {"task": legacy["focus"]}
    seed_result(tmp_path, "task", {"focus": _focus("task", "new")})
    assert first["task"]["text"] == "Ready café"
    assert task_store.focus_objects(tmp_path, ["task"])["task"]["text"] == "new"
    other = tmp_path / "fork"
    assert task_store.focus_objects(other, ["task"]) == {} and not (other / "state").exists()
    assert task_store.focus_objects(other, []) == {} and not (other / "state").exists()
    seed_result(other, "task", {"focus": _focus("task", "fork")})
    assert task_store.focus_objects(other, ["task"])["task"]["text"] == "fork"
    assert task_store.focus_objects(tmp_path, ["task"])["task"]["text"] == "new"


def test_unreadable_focus_boundary_still_fails_capture_explicitly(tmp_path, monkeypatch):
    seed_result(tmp_path, "task", {"focus": _focus("task")})
    unreadable_result(monkeypatch, "task", root=tmp_path)
    with pytest.raises(store.StoreError, match="simulated unreadable row"):
        task_store.focus_objects(tmp_path, ["task"])


@pytest.mark.parametrize("error_type", [store.StoreMigrating, store.StoreRetired, store.StoreLost, store.StoreBusy])
def test_store_failure_is_not_an_empty_focus_directory(tmp_path, monkeypatch, error_type):
    from tests.test_memory_view_room import _install, _view
    _install(tmp_path)
    original = task_store.focus_objects
    def broken(root, task_ids):
        def unavailable(*_args, **_kwargs):
            raise error_type("focus unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(task_store, "open_store", unavailable)
            return original(root, task_ids)
    monkeypatch.setattr(task_store, "focus_objects", broken)
    snap, _text = _view(tmp_path, {"id": "wake", "usage_category": "consciousness"})
    assert snap.store_status["state"] == "journal_unreadable"
    assert error_type.__name__ in snap.store_status["reason"]


def test_unrelated_malformed_handoff_is_not_a_focus_dependency(tmp_path):
    seed_result(tmp_path, "task", {"focus": _focus("task"), "launch_handoffs": {"call": {"state": "held"}}})
    with store.open_store(tmp_path, write=True) as txn:
        txn.conn.execute("UPDATE task_handoffs SET facts='broken' WHERE task_id='task'")
    with pytest.raises(ValueError):
        task_store.display_documents(tmp_path, ["task"])
    assert task_store.focus_objects(tmp_path, ["task"]) == {"task": _focus("task")}
