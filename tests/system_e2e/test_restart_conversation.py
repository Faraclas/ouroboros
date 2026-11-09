"""S43: ordinary Project input survives the requester's real POSIX re-exec.

Two carriers drive the same public ingress and durable queue. Provider responses
are scripted/keyless; this tests host custody, not model judgment or Windows.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from websockets.sync.client import connect

from devtools.benchmarks.common.server_runner import _api
from ouroboros.owner_mailbox import _mailbox_path
from tests.candidate_checkout import SENTINEL_ROUTE, candidate_checkout
from tests.system_e2e.harness import (
    REPO_ROOT, LANE_MOCK, ArtifactOracle, ModelGate, ScriptedStubModel,
    classify_call, keyless_settings, message_text, require_lane,
    start_server, wait_durable_result, wait_until, ws_url,
)
from tests.system_e2e.test_ordinary_conversation_capabilities import _received_turn
from tests.system_e2e.test_system_scenarios_w6 import _WsFrames
from tests.system_e2e.test_system_scenarios_w9 import _committed_install, _fetch, _git


@pytest.mark.integration
@pytest.mark.serial
@pytest.mark.skipif(os.name == "nt", reason="POSIX in-place re-exec; Windows replacement is separate")
@pytest.mark.parametrize("pooled", [False, True])
def test_s43_project_input_after_accepted_restart_is_answered_once_after_reexec(tmp_path, pooled):
    require_lane(LANE_MOCK)
    root = tmp_path / ("pooled" if pooled else "direct")
    closing, later, answer = [prefix + uuid.uuid4().hex for prefix in ("CLOSING_", "LATER_", "ANSWER_")]
    first_id, later_id = [uuid.uuid4().hex for _ in range(2)]
    gate = ModelGate(lambda body: classify_call(body) == "agent" and bool(body.get("tools")) and any(
        row.get("role") == "tool" and "Restart requested." in message_text(row)
        for row in body.get("messages", []) if isinstance(row, dict)), timeout=240)
    state = {}
    def postboot_answer(_body):
        # Boot workers can start before another thread publishes verification.
        verified = wait_until(lambda: state["oracle"].events("restart_verify"), 10)
        assert len(verified) == 1 and verified[0]["ok"], verified
        own_input = any(row.get("role") == "user" and message_text(row) == later
                        for row in _body.get("messages", []) if isinstance(row, dict))
        return {"final": answer if own_input else "Main restart recovery complete."}
    class RestartStub(ScriptedStubModel):
        def _next_step(self, body):
            # Restart also launches Main's recovery turn. It must not consume
            # a single global reply reserved for the saved Project input.
            if self._script_index >= len(self.script):
                return postboot_answer(body)
            return super()._next_step(body)

    script = [{"tool": "request_restart", "arguments": {"reason": "Project conversation regression"}},
              {"final": closing}]
    with candidate_checkout(REPO_ROOT, root / "source" / "repo", origin_proof=True) as source, \
            RestartStub(script, gate=gate, final_answer="UNEXPECTED_EXTRA_TURN") as stub:
        install = _committed_install(source.path, root / "install" / "repo")
        head = _git(install, "rev-parse", "HEAD")
        server = start_server(install, root, keyless_settings(stub, OUROBOROS_RUNTIME_MODE="advanced"))
        try:
            oracle = state["oracle"] = ArtifactOracle(server.data_root)
            pid = server.proc.pid
            assert _fetch(server.base_url + SENTINEL_ROUTE) == source.sentinel_bytes
            project = _api(server.base_url, "POST", "/api/projects", {"name": "Closing installation"}, timeout=60)["project"]
            chat, project_id = project["chat_id"], project["id"]
            with connect(ws_url(server), open_timeout=30, proxy=None) as ws, _WsFrames(ws) as frames:
                if pooled:
                    requester = _api(server.base_url, "POST", "/api/tasks", {
                        "description": "Request an ordinary restart, then answer.",
                        "project_id": project_id, "chat_id": chat, "memory_mode": "forked",
                        "actor_id": "e2e-driver", "source": "e2e-driver",
                    }, timeout=60)["task_id"]
                else:
                    ws.send(json.dumps({"type": "chat", "content": "Request an ordinary restart, then answer.",
                        "chat_id": chat, "project_id": project_id, "client_message_id": first_id}))
                assert gate.arrived.wait(180), "No acknowledged restart round"
                intent = oracle.queue_snapshot()["restart_intent"]
                if not pooled:
                    requester = intent["task_id"]
                    received = _received_turn(oracle, first_id)
                    assert received["id"] == requester and received["_is_direct_chat"]
                assert intent["task_id"] == requester and intent["status"] == "accepted"
                frame = {"type": "chat", "content": later, "chat_id": chat,
                         "project_id": project_id, "client_message_id": later_id}
                ws.send(json.dumps(frame))
                ws.send(json.dumps(frame))  # same ingress identity must not create another saved turn
                def saved_tasks():
                    tasks = [row.get("task") or {} for row in oracle.queue_snapshot().get("pending", [])]
                    return [task for task in tasks if (task.get("metadata") or {}).get("client_message_id") == later_id]
                saved = wait_until(saved_tasks, 30)
                assert len(saved) == 1, oracle.queue_snapshot()
                task = saved[0]
                assert task["id"] != requester and task["text"] == later
                assert task["chat_id"] == chat and task["project_id"] == project_id
                assert wait_until(lambda: [row for row in frames.find(type="chat")
                    if "Saved for execution after Restart" in str(row.get("content") or "")
                    and row.get("chat_id") == chat], 30)
                path = _mailbox_path(server.data_root, requester)
                mail = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
                assert not [row for row in mail if row.get("text") == later]
                assert not _received_turn(oracle, later_id), "saved conversation cannot start before natural completion"
                assert oracle.queue_snapshot()["restart_intent"]["status"] == "accepted"
                gate.release.set()
                result = wait_durable_result(oracle, task["id"], timeout=240)
            assert result["status"] == "completed" and answer in str(result.get("result") or ""), result
            assert result["chat_id"] == chat and result["project_id"] == project_id
            original = wait_durable_result(oracle, requester, timeout=60)
            assert closing in str(original.get("result") or "") and answer not in str(original.get("result") or "")
            verified = oracle.events("restart_verify")
            assert len(verified) == 1 and verified[0]["ok"]
            assert verified[0]["expected_sha"] == verified[0]["observed_sha"] == head
            assert server.proc.pid == pid and server.proc.poll() is None
            assert _fetch(server.base_url + SENTINEL_ROUTE) == source.sentinel_bytes
            def own_replies():
                return [row for row in oracle._jsonl("logs/chat.jsonl")
                        if row.get("direction") == "out" and answer in str(row.get("text") or "")]
            replies = wait_until(own_replies, 30)  # result storage can precede chat publication
            assert len(replies) == 1 and replies[0]["task_id"] == task["id"] and replies[0]["chat_id"] == chat, replies
            assert stub.script_consumed() and gate.held == 1 and not gate.timed_out
        finally:
            print("S43_CALL_KINDS", stub.kinds())
            print("S43_RESTART_VERIFY", oracle.events("restart_verify"))
            print("S43_TASK_RESULTS", [(row.get("task_id"), row.get("status"), str(row.get("result") or "")[:300])
                                        for row in [oracle.task_result(requester), oracle.task_result(task["id"])]]
                  if "task" in locals() else "not saved")
            gate.release.set()
            server.stop()
