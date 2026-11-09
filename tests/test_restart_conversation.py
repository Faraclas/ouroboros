"""Ordinary room input must not reopen an accepted restart requester."""
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from tests.test_agent_restart_intent import runtime, accept  # noqa: F401
from tests.test_project_routing_v664 import _ctx

pytestmark = pytest.mark.serial


def _room(runtime, monkeypatch, direct=False):
    from ouroboros.projects_registry import create_project
    from ouroboros.server_routing_context import RootSnapshot
    from supervisor.active_activity import get_direct_activity_registry

    project = create_project(runtime.root, "Closing installation")
    task = runtime.workers.RUNNING["requester"]["task"]
    task.update(chat_id=project["chat_id"], project_id=project["id"], drive_root=str(runtime.root))
    actor = SimpleNamespace(_busy=True, _accepting_owner_messages=True,
        _current_task_id="requester", _owner_message_admission_lock=threading.RLock(),
        _owner_message_generation=0, tools=SimpleNamespace(_ctx=runtime.ctx))
    if direct:
        runtime.workers.RUNNING.clear()
        entry = get_direct_activity_registry().register("requester", project["chat_id"], actor=actor)
        entry.execution.advance("started")
        monkeypatch.setattr(runtime.workers, "get_direct_chat_agent", lambda tid: actor)
    host = _ctx(runtime.root, running=runtime.workers.RUNNING)
    host.RUNNING, host.PENDING = runtime.workers.RUNNING, runtime.workers.PENDING
    roots = RootSnapshot((({"task_id": "requester", "room_reader": True,
        "direct_chat": direct, "project_id": project["id"], "status": "running"},
        frozenset({project["chat_id"]})),))
    return project, host, roots, actor


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_exact_requester_defers_ordinary_input_only_after_restart_intent(runtime, monkeypatch, direct, restart):
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.server_owner_routing import _route_project_chat_to_running_task
    from supervisor.active_activity import get_direct_activity_registry

    room, host, roots, actor = _room(runtime, monkeypatch, direct)
    try:
        if restart:
            accept(runtime)
        routed = _route_project_chat_to_running_task(host, room["chat_id"], "later ordinary question",
                                                     "later-id", roots=roots)
        assert routed == ("" if restart else "requester")
        entries = drain_owner_entries(runtime.root, "requester")
        assert [row["text"] for row in entries] == ([] if restart else ["later ordinary question"])
        assert actor._owner_message_generation == (int(not restart) if direct else 0)
        assert not runtime.server._pending_restart, "room intake must not start a premature drain"
    finally:
        if direct:
            get_direct_activity_registry().unregister("requester")


@pytest.mark.parametrize("upload", [False, True])
def test_project_message_uses_existing_durable_direct_deferral(runtime, monkeypatch, upload):
    from ouroboros import server_owner_routing as routing
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.project_dialogue import latest_chat_annotations
    from supervisor import worker_chat_lane

    room, host, roots, _actor = _room(runtime, monkeypatch)
    accept(runtime)
    notices, saved = [], threading.Event()
    def notice(*args, **kwargs):
        notices.append((args, kwargs))
        saved.set()
    monkeypatch.setattr(runtime.workers, "_get_chat_agent", lambda: pytest.fail("no model before restart"))
    monkeypatch.setattr(runtime.workers, "send_with_budget", notice)
    monkeypatch.setattr(routing, "addressable_roots", lambda ctx: roots)
    host.handle_chat_direct = worker_chat_lane.handle_chat_direct
    bridge = SimpleNamespace(send_routing_ack=lambda *a, **k: pytest.fail("must not claim mailbox delivery"))
    source = runtime.root / "owner-input.txt"
    source.write_bytes(b"Exact owner file\r\n")
    staged = []
    stage = routing._stage_mailbox_attachments
    def capture_staging(*args, **kwargs):
        value = stage(*args, **kwargs)
        staged.extend(value[1])
        return value
    monkeypatch.setattr(routing, "_stage_mailbox_attachments", capture_staging)
    routing._route_owner_message(bridge, host, {
        "chat_id": room["chat_id"], "text": "a later question", "client_message_id": "saved-id",
        "image_data": None if upload else ("cGhvdG8=", "image/png"),
        "task_metadata": {"chat_attachment_uploads": [{"path": str(source), "label": "owner input"}]} if upload else {},
    })
    assert saved.wait(5), notices
    assert drain_owner_entries(runtime.root, "requester") == []
    queued = [row for row in runtime.queue.PENDING if str(row.get("text") or "").startswith("a later question")]
    assert len(queued) == 1
    task = queued[0]
    assert task["chat_id"] == room["chat_id"] and task["project_id"] == room["id"]
    assert task["metadata"]["client_message_id"] == "saved-id"
    if upload:
        from pathlib import Path
        from ouroboros.artifacts import task_artifact_dir_path
        row = task["attachment_manifest"][0]
        assert (task_artifact_dir_path(runtime.root, task["id"]) / row["relpath"]).read_bytes() == source.read_bytes()
        assert staged and not Path(staged[0]["abs_path"]).exists(), "refused requester copy must be removed"
        assert source.read_bytes() == b"Exact owner file\r\n"
    else:
        assert task.get("image_source_ref") and "image_base64" not in task
    assert "Saved for execution" in notices[-1][0][1]
    assert "saved-id" not in latest_chat_annotations(runtime.root)


@pytest.mark.parametrize("status,attempt,defer", [
    ("awaiting", 1, False), ("accepted", 1, True), ("ready", 1, True),
    ("released", 1, False), ("refused", 1, False), ("accepted", 2, False),
])
def test_pending_state_and_exact_attempt_do_not_become_a_permanent_reader_fence(runtime, monkeypatch, status, attempt, defer):
    from ouroboros.server_owner_routing import _route_project_chat_to_running_task
    room, host, roots, _actor = _room(runtime, monkeypatch)
    accept(runtime)
    runtime.restart._AGENT_RESTART["status"] = status
    runtime.workers.RUNNING["requester"]["attempt"] = attempt
    assert _route_project_chat_to_running_task(host, room["chat_id"], "new input", "state-id", roots=roots) == (
        "" if defer else "requester")


def test_late_answer_head_is_ordinary_input_not_the_requesters_own_required_question(runtime, monkeypatch):
    from ouroboros.server_owner_routing import _route_project_chat_to_running_task
    from ouroboros.server_routing_context import RootSnapshot
    room, host, roots, _actor = _room(runtime, monkeypatch)
    candidate = {**roots.rows(room["chat_id"])[0], "late_input_of": "finished-asker"}
    roots = RootSnapshot(((candidate, frozenset({room["chat_id"]})),))
    accept(runtime)
    assert _route_project_chat_to_running_task(host, room["chat_id"], "second late answer", "late-id",
                                               roots=roots, thread_of="finished-asker") == ""


def test_place_author_without_queue_or_direct_carrier_cannot_accept_restart(runtime):
    from ouroboros.conversation_lease import PresenceTurnGate, accept_author_mail, presence_author_agent
    calls = []
    lease = PresenceTurnGate(state_root=runtime.root / "state").acquire("restart-room")
    try:
        lease.bind_author("requester")
        actor = lease.intake_agent = SimpleNamespace(_owner_message_admission_lock=threading.RLock(),
            _busy=True, _accepting_owner_messages=True, _current_task_id="requester",
            tools=SimpleNamespace(_ctx=runtime.ctx))
        assert accept_author_mail(runtime.root, "requester", "restart-room", lambda: calls.append(1) or True)
        from tests.test_agent_restart_intent import request
        runtime.workers.RUNNING.clear()
        replies, done, thread = request(runtime)
        runtime.server._handle_restart_in_supervisor(runtime.events.get(timeout=5), runtime.host)
        assert done.wait(5)
        thread.join(5)
        assert runtime.restart._AGENT_RESTART["status"] == "refused", replies
        assert accept_author_mail(runtime.root, "requester", "restart-room", lambda: calls.append(2) or True)
        assert calls == [1, 2]
        assert presence_author_agent(runtime.root, "requester") is actor
        assert actor._accepting_owner_messages, "required answers keep their separate intake"
    finally:
        lease.release()


@pytest.mark.parametrize("direct", [False, True])
def test_acceptance_between_reader_snapshot_and_mailbox_commit_is_rechecked(runtime, monkeypatch, direct):
    from ouroboros import server_owner_routing as routing
    from ouroboros.owner_mailbox import drain_owner_entries
    from supervisor.active_activity import get_direct_activity_registry
    room, host, roots, _actor = _room(runtime, monkeypatch, direct)
    stage = routing._stage_mailbox_attachments
    def accept_after_staging(*args, **kwargs):
        value = stage(*args, **kwargs)
        accept(runtime)
        return value
    monkeypatch.setattr(routing, "_stage_mailbox_attachments", accept_after_staging)
    try:
        assert routing._route_project_chat_to_running_task(host, room["chat_id"], "racing input", "race-id", roots=roots) == ""
        assert drain_owner_entries(runtime.root, "requester") == []
    finally:
        if direct:
            get_direct_activity_registry().unregister("requester")


def test_mailbox_commit_before_acceptance_is_not_discarded(runtime, monkeypatch):
    from ouroboros import server_owner_routing as routing
    from ouroboros.owner_mailbox import drain_owner_entries
    from tests.test_agent_restart_intent import request
    room, host, roots, _actor = _room(runtime, monkeypatch)
    import ouroboros.owner_mailbox as mail
    append = mail.write_owner_message
    pending = {}
    def accepting_while_mail_commits(*args, **kwargs):
        result = append(*args, **kwargs)
        replies, done, thread = request(runtime)
        event = runtime.events.get(timeout=5)
        # A separate event consumer cannot win Q until the mail admission ends.
        excluded = []
        def observe_lock():
            acquired = runtime.queue._queue_lock.acquire(blocking=False)
            excluded.append(not acquired)
            if acquired:
                runtime.queue._queue_lock.release()
        consumer = threading.Thread(target=observe_lock)
        consumer.start()
        consumer.join(5)
        assert excluded == [True]
        pending.update(replies=replies, done=done, thread=thread, event=event)
        return result
    monkeypatch.setattr(mail, "write_owner_message", accepting_while_mail_commits)
    try:
        assert routing._route_project_chat_to_running_task(host, room["chat_id"], "before acceptance", "before-id", roots=roots) == "requester"
        runtime.server._handle_restart_in_supervisor(pending["event"], runtime.host)
        assert pending["done"].wait(5)
        assert pending["replies"][0].startswith("Restart requested.")
        assert [row["text"] for row in drain_owner_entries(runtime.root, "requester")] == ["before acceptance"]
    finally:
        if pending:
            pending["thread"].join(10)


@pytest.mark.parametrize("direct", [False, True])
def test_required_quiz_answer_still_reaches_the_accepted_requester(runtime, monkeypatch, direct):
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.task_results import write_task_result
    from supervisor import message_bus
    from supervisor.active_activity import get_direct_activity_registry
    from tests.test_quiz_history_evidence import _ask, _answer
    room, _host, _roots, _actor = _room(runtime, monkeypatch, direct)
    runtime.task = {"id": "requester", "chat_id": room["chat_id"], "drive_root": str(runtime.root),
                    "project_id": room["id"]}
    write_task_result(runtime.root, "requester", "running", chat_id=room["chat_id"], task_attempt=1)
    bridge = runtime.bridge = message_bus.LocalChatBridge()
    bridge._broadcast_fn = lambda frame: None
    monkeypatch.setattr(message_bus, "_BRIDGE", bridge)
    monkeypatch.setattr(message_bus, "DATA_DIR", runtime.root)
    try:
        _ask(runtime, "installation-answer", chat_id=room["chat_id"])
        accept(runtime)
        status, response = _answer(runtime, "installation-answer", request_id="required-answer", comment="exact answer")
        assert status == 200 and response["state"] == "answered", response
        entries = drain_owner_entries(runtime.root, "requester")
        assert len(entries) == 1 and entries[0]["kind"] == "quiz_answer"
        assert "exact answer" in entries[0]["text"]
        assert not runtime.queue.PENDING, "a required answer must not become a new deferred turn"
    finally:
        if direct:
            get_direct_activity_registry().unregister("requester")
