"""Disposable Work Items verification. Never uses the installation's live board DB."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import plugin
from board_store import BoardError, BoardStore

ACTOR = {"surface": "interface", "label": "via Interface/API"}
ROOT = Path(__file__).resolve().parent


def known(project_id):
    return {"id": project_id, "lifecycle": "active"} if project_id in ("p", "q", "closed") else None


def process_comment(state_dir, item_id, number, event, queue):
    event.wait()
    try:
        response = BoardStore(state_dir, known).mutate("comment", {"item_id": item_id, "text": f"message {number}",
            "request_id": f"process-comment-{number}"}, ACTOR)
        queue.put(response)
    except Exception as exc:
        queue.put({"failure": repr(exc)})


def process_update(state_dir, item_id, title, event, queue):
    event.wait()
    try:
        result = BoardStore(state_dir, known).mutate("update", {"item_id": item_id, "expected_revision": 1,
            "request_id": title, "changes": {"title": title}}, ACTOR)
        queue.put(result)
    except BoardError as exc:
        queue.put(exc.payload())


def process_capture(state_dir, event, queue):
    event.wait()
    try:
        queue.put(BoardStore(state_dir, known).mutate("capture", {"project_id": "p", "title": "same", "request_id": "same"}, ACTOR))
    except Exception as exc:
        queue.put({"failure": repr(exc)})


class FakeAPI:
    def __init__(self, path):
        self.path, self.tools, self.routes = path, {}, []

    def get_state_dir(self):
        return str(self.path / "canonical" / "state" / "skills" / "project-boards")

    def get_runtime_info(self):
        return {"data_dir": str(self.path / "canonical")}

    def register_tool(self, name, handler, **kwargs):
        self.tools[name] = (handler, kwargs)

    def register_route(self, name, handler, methods):
        self.routes.append(Route("/api/extensions/project-boards/" + name, handler, methods=methods))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = BoardStore(self.root, known)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def key(self):
        self.counter += 1
        return "request-" + str(self.counter)

    def capture(self, project_id="p", title="Title", description=""):
        return self.store.mutate("capture", {"project_id": project_id, "title": title, "description": description,
            "request_id": self.key()}, ACTOR)["item"]

    def update(self, item, changes):
        return self.store.mutate("update", {"item_id": item["id"], "expected_revision": item["revision"],
            "request_id": self.key(), "changes": changes}, ACTOR)["item"]

    def comment(self, item, text="Comment", quote=None):
        return self.store.mutate("comment", {"item_id": item["id"], "text": text, "request_id": self.key(),
            "quote_comment_id": quote}, ACTOR)

    def assert_error(self, code, call):
        with self.assertRaises(BoardError) as found:
            call()
        self.assertEqual(found.exception.code, code)
        return found.exception

    def test_title_only_capture_and_no_execution_fields(self):
        item = self.capture()
        self.assertEqual((item["disposition"], item["revision"], item["archived"]), ("idea", 1, False))
        self.assertEqual(item["created_by"], ACTOR)
        self.assertNotIn("task_status", item)
        self.assertNotIn("run", item)
        self.assertTrue(item["created_at"].endswith("+00:00"))

    def test_exact_unicode_newlines_and_explicit_clear(self):
        content = "  Åß👩🏽‍💻\r\nline\rthird\n零  "
        item = self.capture(description=content)
        item = self.update(item, {"title": content, "outcome": content})
        comment = self.comment(item, content)["comment"]
        found = self.store.get(item["id"])
        self.assertEqual(found["item"]["description"], content)
        self.assertEqual(found["item"]["title"], content)
        self.assertEqual(found["comments"][0]["text"], content)
        self.assertEqual(comment["text"], content)
        item = self.update(found["item"], {"description": "", "outcome": "", "references": [], "prerequisites": []})
        self.assertEqual((item["description"], item["outcome"]), ("", ""))

    def test_cas_comment_revision_and_current_conflict(self):
        item = self.capture()
        self.comment(item)
        exc = self.assert_error("revision_conflict", lambda: self.update(item, {"title": "stale"}))
        self.assertEqual(exc.status, 409)
        self.assertEqual(exc.current["revision"], 2)
        self.assertEqual(exc.current["title"], item["title"])

    def test_dispositions_are_unrestricted_authored_values(self):
        item = self.capture()
        for value in ("done", "idea", "set_aside", "planned", "done"):
            item = self.update(item, {"disposition": value})
            self.assertEqual(item["disposition"], value)
        self.assertEqual(self.store.find("p")["counts"]["done"], 1)

    def test_loss_delivery_receipt_original_response_survives_later_edit(self):
        payload = {"project_id": "p", "title": "Original", "request_id": "lost"}
        original = self.store.mutate("capture", payload, ACTOR)
        self.update(original["item"], {"title": "Later"})
        repeated = self.store.mutate("capture", payload, ACTOR)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["item"], original["item"])
        self.assertEqual(self.store.find("p")["total"], 1)
        self.assert_error("request_conflict", lambda: self.store.mutate("capture", {**payload, "title": "Changed"}, ACTOR))
        self.assert_error("request_conflict", lambda: self.store.mutate("capture", payload, {**ACTOR, "label": "different"}))

    def test_comment_idempotency_and_quote_snapshot(self):
        item = self.capture()
        first = self.comment(item, "quoted\r\nβ")["comment"]
        payload = {"item_id": item["id"], "text": "reply", "request_id": "reply", "quote_comment_id": first["id"]}
        result = self.store.mutate("comment", payload, ACTOR)
        repeated = self.store.mutate("comment", payload, ACTOR)
        self.assertEqual(result["comment"], repeated["comment"])
        self.assertEqual(result["comment"]["quote"]["text"], first["text"])
        self.assertEqual(len(self.store.get(item["id"])["comments"]), 2)
        other = self.capture()
        self.assert_error("invalid", lambda: self.comment(other, quote=first["id"]))

    def test_links_across_projects_reverse_current_fields_and_cycles_allowed(self):
        one, two = self.capture(), self.capture("q", "Second")
        one = self.update(one, {"prerequisites": [{"item_id": two["id"], "needs": "Ship API"}]})
        two = self.update(two, {"title": "Renamed", "disposition": "done", "prerequisites": [{"item_id": one["id"]}]})
        result = self.store.get(one["id"])
        self.assertEqual(result["item"]["prerequisites"][0], {"item_id": two["id"], "needs": "Ship API",
            "title": "Renamed", "disposition": "done", "project_id": "q", "missing": False})
        self.assertEqual(result["needed_by"][0]["item_id"], two["id"])
        self.assertEqual(self.store.get(two["id"])["needed_by"][0]["needs"], "Ship API")
        self.assert_error("invalid", lambda: self.update(one, {"prerequisites": [{"item_id": one["id"]}]}))
        self.assert_error("invalid", lambda: self.update(one, {"prerequisites": [{"item_id": two["id"]}, {"item_id": two["id"]}]}))

    def test_invalid_multifield_write_rolls_back_links_and_receipt(self):
        item, target = self.capture(), self.capture("q")
        original = self.store.get(item["id"])
        self.assert_error("invalid", lambda: self.update(item, {"title": "do not save", "prerequisites": [{"item_id": target["id"]}, {"item_id": "missing"}]}))
        self.assertEqual(self.store.get(item["id"]), original)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_search_all_fields_unicode_literal_wildcards_pagination_counts(self):
        a, b, c = self.capture(title="Straße"), self.capture(description="Describe ☃"), self.capture(title="% literal")
        b = self.update(b, {"outcome": "OUTCOME"})
        self.comment(c, "Thread Needle")
        self.assertEqual(self.store.find("p", q="STRASSE")["total"], 1)
        self.assertEqual(self.store.find("p", q="☃")["total"], 1)
        self.assertEqual(self.store.find("p", q="outcome")["total"], 1)
        self.assertEqual(self.store.find("p", q="needle")["total"], 1)
        self.assertEqual(self.store.find("p", q="%")["total"], 1)
        first = self.store.find("p", limit=2)
        second = self.store.find("p", limit=2, offset=2)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({r["id"] for r in first["items"] + second["items"]}), 3)
        self.assertEqual(self.store.find("p", q="absent")["counts"]["idea"], 3)
        self.assertEqual(self.store.find("p", q="needle")["items"][0]["preview"], "Thread Needle")

    def test_archived_flag_export_and_closed_project_retention(self):
        item = self.capture("closed")
        item = self.update(item, {"archived": True, "references": [{"kind": "task", "value": "opaque-task", "label": "Work"},
            {"kind": "row", "value": "row:1@example#hash"}, {"kind": "url", "value": "https://example.test/x?a=b"}]})
        self.comment(item, "Retain")
        self.assertEqual(self.store.find("closed")["total"], 0)
        self.assertEqual(self.store.find("closed", archived=True)["total"], 1)
        exported = self.store.export("closed")
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["items"][0]["comments"][0]["text"], "Retain")
        self.assertEqual(exported["items"][0]["references"], item["references"])
        self.assertTrue(exported["items"][0]["archived"])

    def test_missing_and_unavailable_project_storage_not_deleted(self):
        item = self.capture()
        store = BoardStore(self.root, lambda _: None)
        self.assertEqual(self.assert_error("missing", lambda: store.get(item["id"])).status, 404)
        def unavailable(_):
            raise OSError("private details")
        store = BoardStore(self.root, unavailable)
        error = self.assert_error("unavailable", lambda: store.find("p"))
        self.assertEqual(error.status, 503)
        self.assertNotIn("private details", str(error))
        self.assertEqual(self.store.get(item["id"])["item"], item)

    def test_validation_no_partial_writes(self):
        invalids = [None, [], {}, {"project_id": "p", "title": " ", "request_id": "a"},
            {"project_id": "p", "title": None, "request_id": "b"},
            {"project_id": "p", "title": "valid", "request_id": "c", "author": ACTOR},
            {"project_id": "p", "title": "\udcff", "request_id": "d"}]
        for payload in invalids:
            with self.subTest(payload=repr(payload)):
                self.assert_error("invalid", lambda: self.store.mutate("capture", payload, ACTOR))
        self.assertEqual(self.store.find("p")["total"], 0)
        item = self.capture()
        for changes in ({"archived": 1}, {"disposition": "doing"}, {"project_id": "q"}, {"title": ""},
                        {"references": [{"kind": "url", "value": "javascript:alert(1)"}]},
                        {"references": [{"kind": "url", "value": "https://user:pass@example.test"}]},
                        {"references": [{"kind": "task", "value": "x", "status": "running"}]}):
            with self.subTest(changes=changes):
                self.assert_error("invalid", lambda: self.update(item, changes))
        for kwargs in ({"offset": -1}, {"limit": 0}, {"limit": 201}, {"offset": True}, {"archived": "false"}):
            self.assert_error("invalid", lambda: self.store.find("p", **kwargs))

    def processes(self, target, args):
        ctx = multiprocessing.get_context("spawn")
        event, queue = ctx.Event(), ctx.Queue()
        children = [ctx.Process(target=target, args=(*values, event, queue)) for values in args]
        for child in children:
            child.start()
        event.set()
        results = [queue.get(timeout=30) for _ in children]
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        queue.close()
        queue.join_thread()
        return results

    def test_real_crossprocess_concurrent_appends(self):
        item = self.capture()
        results = self.processes(process_comment, [(str(self.root), item["id"], n) for n in range(8)])
        self.assertTrue(all(r.get("ok") for r in results), results)
        found = self.store.get(item["id"])
        self.assertEqual(len(found["comments"]), 8)
        self.assertEqual(found["item"]["revision"], 9)
        self.assertEqual(len({r["id"] for r in found["comments"]}), 8)
        self.assertEqual([r["seq"] for r in found["comments"]], sorted(r["seq"] for r in found["comments"]))

    def test_real_crossprocess_cas_one_winner(self):
        item = self.capture()
        results = self.processes(process_update, [(str(self.root), item["id"], name) for name in ("first", "second")])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual([r["error"]["code"] for r in results if not r["ok"]], ["revision_conflict"])
        self.assertEqual(self.store.get(item["id"])["item"]["revision"], 2)

    def test_real_crossprocess_first_init_duplicate_exactly_once(self):
        results = self.processes(process_capture, [(str(self.root),)] * 4)
        self.assertTrue(all(r.get("ok") for r in results), results)
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        self.assertEqual(len({r["item"]["id"] for r in results}), 1)
        self.assertEqual(self.store.find("p")["total"], 1)

    def test_fresh_process_persistence(self):
        item = self.capture(description="durable\r\nβ")
        script = """import json,sys
from board_store import BoardStore
s=BoardStore(sys.argv[1],lambda p: {'id':p})
print(json.dumps(s.get(sys.argv[2]),ensure_ascii=False))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), item["id"]], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["item"], item)

    def test_future_or_corrupt_schema_is_unavailable_not_replaced(self):
        self.capture()
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("PRAGMA user_version=9")
        self.assert_error("unavailable", lambda: self.store.find("p"))
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_invalid_fingerprint_values_and_large_offset_are_typed(self):
        for value in (float("inf"), b"not JSON", "\udcff"):
            self.assert_error("invalid", lambda: self.store.mutate("capture", {
                "project_id": "p", "title": value, "request_id": "invalid"}, ACTOR))
        self.assert_error("invalid", lambda: self.store.find("p", offset=2**63))

    def test_wal_snapshot_reader_does_not_block_independent_writer(self):
        item = self.capture()
        with closing(sqlite3.connect(self.store.path, timeout=1, isolation_level=None)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            conn.execute("BEGIN")
            self.assertEqual(conn.execute("SELECT title FROM items").fetchone()[0], item["title"])
            self.update(item, {"title": "new"})
            self.assertEqual(conn.execute("SELECT title FROM items").fetchone()[0], item["title"])
            conn.rollback()
        self.assertEqual(self.store.get(item["id"])["item"]["title"], "new")

    def test_setup_busy_reopens_before_exactly_one_mutation(self):
        connect = sqlite3.connect
        opened = []
        class BusyOnce:
            def __init__(self, conn):
                object.__setattr__(self, "conn", conn)
                object.__setattr__(self, "closed", False)
            def __setattr__(self, name, value):
                setattr(self.conn, name, value)
            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode=WAL":
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = 261  # SQLITE_BUSY_RECOVERY, primary5
                    raise error
                return self.conn.execute(sql, *args)
            def close(self):
                object.__setattr__(self, "closed", True)
                self.conn.close()
        def guarded_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            if not opened:
                conn = BusyOnce(conn)
            opened.append(conn)
            return conn
        with patch("board_store.sqlite3.connect", side_effect=guarded_connect):
            item = self.capture()
        self.assertEqual(len(opened), 2)
        self.assertTrue(opened[0].closed)
        self.assertEqual(self.store.find("p")["total"], 1)
        self.assertEqual(self.store.get(item["id"])["item"]["revision"], 1)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = FakeAPI(self.root)
        self.patch = patch.object(plugin, "project_reader", return_value=known)
        self.patch.start()
        plugin.register(self.api)
        self.app = Starlette(routes=self.api.routes)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.ctx = SimpleNamespace(task_id="real-task", project_id="q", current_chat_id=77,
            drive_root=self.root / "PRIVATE_NOT_CANONICAL")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.temp.cleanup()

    def url(self, route):
        return "/api/extensions/project-boards/" + route

    def call(self, name, **args):
        return json.loads(self.api.tools[name][0](self.ctx, **args))

    def test_registration_has_exact_passive_surface_no_background(self):
        self.assertEqual(set(self.api.tools), {"board_find", "board_get", "board_capture", "board_update", "board_comment"})
        self.assertEqual(len(self.api.routes), 6)
        for _name, (_handler, config) in self.api.tools.items():
            self.assertFalse(config["schema"]["additionalProperties"])
            self.assertNotIn("author", config["schema"]["properties"])

    def test_agent_foreign_project_real_actor_and_canonical_shared_http_state(self):
        item = self.call("board_capture", project_id="p", title="foreign", request_id="agent")["item"]
        self.assertEqual(item["created_by"], {"surface": "agent", "label": "Ouroboros", "task_id": "real-task", "source_project_id": "q", "chat_id": 77})
        result = self.client.get(self.url("item"), params={"item_id": item["id"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["item"], item)
        self.assertFalse(self.ctx.drive_root.exists())
        self.assertTrue((Path(self.api.get_state_dir()) / "boards.sqlite3").is_file())
        self.assertEqual(self.call("board_find")["total"], 0)
        self.assertEqual(self.call("board_find", project_id="p")["total"], 1)

    def test_interface_actor_cannot_be_spoofed(self):
        data = {"project_id": "p", "title": "title", "request_id": "http"}
        for extra in ({"author": ACTOR}, {"source_task_id": "fake"}, {"source_project_id": "q"}):
            self.assertEqual(self.client.post(self.url("capture"), json={**data, **extra}).status_code, 400)
        result = self.client.post(self.url("capture"), json=data)
        self.assertEqual(result.json()["item"]["created_by"], ACTOR)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertTrue(self.client.post(self.url("capture"), json=data).json()["duplicate"])
        self.assertEqual(self.client.post(self.url("capture"), json={**data, "title": "changed"}).status_code, 409)

    def test_http_full_mutation_get_list_export_and_conflict(self):
        item = self.client.post(self.url("capture"), json={"project_id": "p", "title": "One", "request_id": "c"}).json()["item"]
        comment = self.client.post(self.url("comment"), json={"item_id": item["id"], "text": "β\r\nquote", "request_id": "comment"})
        self.assertEqual(comment.status_code, 200)
        changes = {"item_id": item["id"], "expected_revision": 1, "request_id": "stale", "changes": {"title": "No"}}
        result = self.client.post(self.url("update"), json=changes)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["current"]["revision"], 2)
        changes.update(expected_revision=2, request_id="fresh")
        self.assertEqual(self.client.post(self.url("update"), json=changes).status_code, 200)
        result = self.client.get(self.url("list"), params={"project_id": "p", "q": "β", "archived": "false", "limit": 1})
        self.assertEqual(result.json()["total"], 1)
        self.assertEqual(self.client.get(self.url("export"), params={"project_id": "p"}).json()["items"][0]["comments"][0]["text"], "β\r\nquote")

    def test_http_invalid_json_duplicate_unknown_repeated_query_and_method(self):
        for body in (b"{", b"[]", b'{"project_id":"p","title":"x","request_id":"a","title":"y"}', b'{"x":NaN}'):
            self.assertEqual(self.client.post(self.url("capture"), content=body).status_code, 400)
        for query in ("project_id=p&project_id=q", "project_id=p&extra=1", "project_id=p&offset=-1", "project_id=p&archived=1", "project_id=p&limit=0"):
            self.assertEqual(self.client.get(self.url("list") + "?" + query).status_code, 400)
        self.assertEqual(self.client.get(self.url("capture")).status_code, 405)
        self.assertEqual(self.client.post(self.url("capture"), json={"project_id": "absent", "title": "x", "request_id": "unknown"}).status_code, 404)

    def test_agent_actor_requires_real_host_identity(self):
        self.ctx.task_id = None
        self.assertEqual(self.call("board_capture", project_id="p", title="x", request_id="no-context")["error"]["code"], "unavailable")

    def test_actual_host_profile_readonly_metadata_and_role_fallback(self):
        for metadata, contract in (({"tool_profile": "local_readonly_subagent"}, {}),
                                   ({"delegation_role": "subagent"}, {}),
                                   ({}, {"delegation_role": "subagent"})):
            self.ctx.task_metadata, self.ctx.task_contract = metadata, contract
            for name, args in (("board_capture", {"project_id": "p", "title": "x", "request_id": "deny"}),
                               ("board_update", {"item_id": "x", "expected_revision": 1, "request_id": "deny", "changes": {}}),
                               ("board_comment", {"item_id": "x", "text": "x", "request_id": "deny"})):
                with self.subTest(metadata=metadata, contract=contract, tool=name):
                    result = self.call(name, **args)
                    self.assertEqual(result["error"]["code"], "readonly")
            self.assertTrue(self.call("board_find", project_id="p")["ok"])
        self.assertEqual(self.call("board_find", project_id="p")["total"], 0)

    def test_actual_host_profile_root_and_explicit_acting_allow_crossproject(self):
        from ouroboros.contracts.task_constraint import TaskConstraint
        self.ctx.task_metadata = {"delegation_role": "subagent", "tool_profile": "acting_subagent"}
        self.ctx.task_contract = {}
        # The canonical mode spelling is supplied by the actual host, not guessed.
        from ouroboros.tool_access import ACTING_SUBAGENT_MODE
        self.ctx.task_constraint = TaskConstraint(mode=ACTING_SUBAGENT_MODE, surface="external_workspace")
        self.assertTrue(self.call("board_capture", project_id="p", title="acting", request_id="acting")["ok"])
        self.ctx.task_metadata, self.ctx.task_contract, self.ctx.task_constraint = {}, {}, None
        self.assertTrue(self.call("board_capture", project_id="p", title="root", request_id="root")["ok"])

    def test_http_surrogates_infinite_exponents_deep_json_and_giant_integer(self):
        for content in ('{"project_id":"p","title":"\\ud800","request_id":"a"}',
                        '{"project_id":"p","title":1e999,"request_id":"a"}',
                        '[' * 2000 + '0' + ']' * 2000):
            self.assertEqual(self.client.post(self.url("capture"), content=content.encode()).status_code, 400)
        for query in ("limit=" + "9" * 5000, "offset=" + str(2**63)):
            self.assertEqual(self.client.get(self.url("list") + "?project_id=p&" + query).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
