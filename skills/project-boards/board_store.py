"""Passive, transaction-owned Work Items storage. No execution-state readers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

DISPOSITIONS = ("idea", "planned", "done", "set_aside")
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, outcome TEXT NOT NULL, disposition TEXT NOT NULL,
 archived INTEGER NOT NULL, revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 created_by TEXT NOT NULL, updated_by TEXT NOT NULL, refs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_project ON items(project_id, created_at, id);
CREATE TABLE IF NOT EXISTS comments (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 item_id TEXT NOT NULL REFERENCES items(id), text TEXT NOT NULL,
 created_at TEXT NOT NULL, author TEXT NOT NULL, quote TEXT
);
CREATE INDEX IF NOT EXISTS comments_item ON comments(item_id, seq);
CREATE TABLE IF NOT EXISTS prerequisites (
 item_id TEXT NOT NULL REFERENCES items(id), target_id TEXT NOT NULL REFERENCES items(id),
 needs TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(item_id,target_id)
);
CREATE INDEX IF NOT EXISTS prerequisites_target ON prerequisites(target_id);
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class BoardError(Exception):
    def __init__(self, code, message, status=400, current=None):
        super().__init__(message)
        self.code, self.message, self.status, self.current = code, message, status, current

    def payload(self):
        result = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.current is not None:
            result["current"] = self.current
        return result


def text(value, field, *, nonblank=False):
    if not isinstance(value, str) or (nonblank and not value.strip()):
        raise BoardError("invalid", f"{field} must be {'nonblank ' if nonblank else ''}text.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise BoardError("invalid", f"{field} must be valid Unicode.") from exc
    return value


def identifier(value, field):
    value = text(value, field, nonblank=True)
    if len(value) > 200 or value != value.strip():
        raise BoardError("invalid", f"{field} must be an identifier of at most 200 characters.")
    return value


def exact_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise BoardError("invalid", "Unknown or missing request fields.")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_references(values):
    if not isinstance(values, list):
        raise BoardError("invalid", "references must be an array.")
    result = []
    for value in values:
        exact_keys(value, ("kind", "value", "label"), ("kind", "value"))
        if value["kind"] not in ("task", "row", "url"):
            raise BoardError("invalid", "Unknown reference kind.")
        entry = {"kind": value["kind"], "value": text(value["value"], "reference.value", nonblank=True),
                 "label": text(value.get("label", ""), "reference.label")}
        if entry["kind"] == "url":
            from urllib.parse import urlsplit
            try:
                url = urlsplit(entry["value"])
                valid = url.scheme in ("http", "https") and bool(url.hostname) and not url.username and not url.password
            except ValueError:
                valid = False
            if not valid:
                raise BoardError("invalid", "URL references require an HTTP(S) URL without credentials.")
        result.append(entry)
    return result


class BoardStore:
    """One SQLite database beneath PluginAPI.get_state_dir(), never ctx.drive_root.

    Each call opens its own connection. BEGIN IMMEDIATE serializes mutation +
    idempotency receipt; reads use one snapshot. No open connection survives a call.
    project_reader is the installed registry owner, injected only for test roots.
    """
    def __init__(self, state_dir, project_reader):
        self.path = Path(state_dir) / "boards.sqlite3"
        self.project_reader = project_reader

    def project(self, project_id):
        project_id = identifier(project_id, "project_id")
        try:
            project = self.project_reader(project_id)
        except BoardError:
            raise
        except Exception as exc:
            raise BoardError("unavailable", "Project registry is unavailable.", 503) from exc
        if not isinstance(project, dict) or project.get("id") != project_id:
            raise BoardError("missing", "Registered project not found; stored items are not deleted.", 404)
        return project_id

    @contextmanager
    def transaction(self, *, write=False):
        conn = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            conn.create_function("casefold", 1, lambda s: s.casefold())
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise BoardError("unavailable", "Work Items storage is unavailable; retry the same request ID after checking.", 503) from exc
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _connect(self):
        # SQLite's journal-mode upgrade can return BUSY immediately when fresh
        # processes initialize together (busy_timeout does not cover that upgrade).
        # Retry ONLY pre-transaction setup on a new connection, never a mutation.
        deadline = time.monotonic() + 15
        while True:
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=15000")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
                elif version != 1:
                    raise BoardError("unavailable", "Unsupported Work Items database version.", 503)
                return conn
            except BaseException as exc:
                conn.close()
                # Primary codes5/6 are SQLite's stable BUSY/LOCKED codes. Mask
                # extended codes; Python3.10 has neither attrs nor named constants.
                code = getattr(exc, "sqlite_errorcode", None)
                if isinstance(exc, sqlite3.OperationalError) and isinstance(code, int) and code & 0xFF in (5, 6) and time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise

    def _row(self, conn, item_id):
        item_id = identifier(item_id, "item_id")
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise BoardError("missing", "Work item not found.", 404)
        self.project(row["project_id"])
        return row

    def _item(self, conn, row, *, resolve=False):
        item = dict(row)
        item["archived"] = bool(item["archived"])
        item["created_by"] = json.loads(item["created_by"])
        item["updated_by"] = json.loads(item["updated_by"])
        item["references"] = json.loads(item.pop("refs"))
        links = conn.execute("""SELECT p.target_id, p.needs, t.project_id, t.title, t.disposition
            FROM prerequisites p LEFT JOIN items t ON t.id=p.target_id
            WHERE p.item_id=? ORDER BY p.position""", (item["id"],)).fetchall()
        item["prerequisites"] = []
        for link in links:
            entry = {"item_id": link["target_id"], "needs": link["needs"]}
            if resolve:
                entry.update(project_id=link["project_id"], title=link["title"], disposition=link["disposition"],
                             missing=link["project_id"] is None)
            item["prerequisites"].append(entry)
        return item

    def _comments(self, conn, item_id):
        return [self._comment_value(r) for r in conn.execute("SELECT * FROM comments WHERE item_id=? ORDER BY seq", (item_id,))]

    def get(self, item_id):
        with self.transaction() as conn:
            row = self._row(conn, item_id)
            needed = conn.execute("""SELECT i.id item_id,i.project_id,i.title,i.disposition,p.needs
                FROM prerequisites p JOIN items i ON i.id=p.item_id
                WHERE p.target_id=? ORDER BY i.created_at,i.id""", (item_id,)).fetchall()
            return {"ok": True, "item": self._item(conn, row, resolve=True),
                    "comments": self._comments(conn, item_id), "needed_by": [dict(r) for r in needed]}

    def find(self, project_id, q="", disposition=None, archived=False, offset=0, limit=100):
        project_id = self.project(project_id)
        q = text(q, "q")
        if disposition is not None and disposition not in DISPOSITIONS:
            raise BoardError("invalid", "Unknown disposition.")
        if type(archived) is not bool or type(offset) is not int or type(limit) is not int or not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise BoardError("invalid", "archived must be boolean; offset 0..2^63-1 and limit 1..200 must be integers.")
        with self.transaction() as conn:
            counts = dict.fromkeys(DISPOSITIONS, 0)
            for row in conn.execute("SELECT disposition,COUNT(*) n FROM items WHERE project_id=? AND archived=0 GROUP BY disposition", (project_id,)):
                counts[row["disposition"]] = row["n"]
            where, args = ["project_id=?", "archived=?"], [project_id, int(archived)]
            if disposition is not None:
                where.append("disposition=?")
                args.append(disposition)
            if q:
                where.append("""(instr(casefold(title),casefold(?)) OR instr(casefold(description),casefold(?))
                    OR instr(casefold(outcome),casefold(?)) OR EXISTS (SELECT 1 FROM comments c
                    WHERE c.item_id=items.id AND instr(casefold(c.text),casefold(?))))""")
                args.extend([q] * 4)
            clause = " AND ".join(where)
            total = conn.execute("SELECT COUNT(*) FROM items WHERE " + clause, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM items WHERE " + clause + " ORDER BY created_at,id LIMIT ? OFFSET ?", args + [limit, offset])
            items = []
            for row in rows:
                item = self._item(conn, row)
                latest = conn.execute("SELECT text FROM comments WHERE item_id=? ORDER BY seq DESC LIMIT 1", (item["id"],)).fetchone()
                item["comment_count"] = conn.execute("SELECT COUNT(*) FROM comments WHERE item_id=?", (item["id"],)).fetchone()[0]
                item["preview"] = latest[0] if latest else item["outcome"] or item["description"]
                items.append(item)
            return {"ok": True, "items": items, "counts": counts, "total": total,
                    "offset": offset, "limit": limit, "has_more": offset + len(items) < total}

    def export(self, project_id):
        project_id = self.project(project_id)
        with self.transaction() as conn:
            items = []
            for row in conn.execute("SELECT * FROM items WHERE project_id=? ORDER BY created_at,id", (project_id,)):
                item = self._item(conn, row)
                item["comments"] = self._comments(conn, item["id"])
                items.append(item)
            return {"ok": True, "schema_version": 1, "project_id": project_id, "exported_at": now(), "items": items}

    def mutate(self, operation, payload, actor):
        payload = dict(payload) if isinstance(payload, dict) else payload
        fields = {
            "capture": (("project_id", "title", "description", "request_id"), ("project_id", "title", "request_id")),
            "update": (("item_id", "expected_revision", "request_id", "changes"), ("item_id", "expected_revision", "request_id", "changes")),
            "comment": (("item_id", "text", "request_id", "quote_comment_id"), ("item_id", "text", "request_id")),
        }
        if operation not in fields:
            raise BoardError("invalid", "Unknown operation.")
        exact_keys(payload, *fields[operation])
        request_id = identifier(payload["request_id"], "request_id")
        try:
            fingerprint = hashlib.sha256(encode({"operation": operation, "payload": payload, "actor": actor}).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise BoardError("invalid", "Request must contain finite, valid Unicode JSON values.") from exc
        with self.transaction(write=True) as conn:
            prior = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise BoardError("request_conflict", "Request ID was already used with different arguments or actor.", 409)
                result = json.loads(prior["response"])
                result["duplicate"] = True
                return result
            result = getattr(self, "_" + operation)(conn, payload, actor)
            result.update(ok=True, duplicate=False)
            conn.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, fingerprint, encode(result)))
            return result

    def _capture(self, conn, payload, actor):
        project_id = self.project(payload["project_id"])
        title = text(payload["title"], "title", nonblank=True)
        description = text(payload.get("description", ""), "description")
        item_id, stamp, who = str(uuid.uuid4()), now(), encode(actor)
        conn.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (item_id, project_id, title, description, "", "idea", 0, 1, stamp, stamp, who, who, "[]"))
        return {"item": self._item(conn, self._row(conn, item_id))}

    def _update(self, conn, payload, actor):
        row = self._row(conn, payload["item_id"])
        expected = payload["expected_revision"]
        if type(expected) is not int or expected < 1:
            raise BoardError("invalid", "expected_revision must be a positive integer.")
        if row["revision"] != expected:
            raise BoardError("revision_conflict", "Item changed; preserve your draft and compare current content.", 409, self._item(conn, row, resolve=True))
        changes = exact_keys(payload["changes"], ("title", "description", "outcome", "disposition", "archived", "references", "prerequisites"))
        item = self._item(conn, row)
        for field in ("title", "description", "outcome"):
            if field in changes:
                item[field] = text(changes[field], field, nonblank=field == "title")
        if "disposition" in changes:
            if changes["disposition"] not in DISPOSITIONS:
                raise BoardError("invalid", "Unknown disposition.")
            item["disposition"] = changes["disposition"]
        if "archived" in changes:
            if type(changes["archived"]) is not bool:
                raise BoardError("invalid", "archived must be boolean.")
            item["archived"] = changes["archived"]
        if "references" in changes:
            item["references"] = validate_references(changes["references"])
        if "prerequisites" in changes:
            links = changes["prerequisites"]
            if not isinstance(links, list):
                raise BoardError("invalid", "prerequisites must be an array.")
            validated, seen = [], set()
            for link in links:
                exact_keys(link, ("item_id", "needs"), ("item_id",))
                target = identifier(link["item_id"], "prerequisite.item_id")
                if target == item["id"] or target in seen:
                    raise BoardError("invalid", "Self and duplicate prerequisites are not allowed.")
                try:
                    self._row(conn, target)
                except BoardError as exc:
                    if exc.code == "missing":
                        raise BoardError("invalid", "Prerequisite target must be an existing registered board item.") from exc
                    raise
                seen.add(target)
                validated.append((item["id"], target, text(link.get("needs", ""), "prerequisite.needs"), len(validated)))
            conn.execute("DELETE FROM prerequisites WHERE item_id=?", (item["id"],))
            conn.executemany("INSERT INTO prerequisites VALUES(?,?,?,?)", validated)
        conn.execute("""UPDATE items SET title=?,description=?,outcome=?,disposition=?,archived=?,revision=revision+1,
            updated_at=?,updated_by=?,refs=? WHERE id=?""", (item["title"], item["description"], item["outcome"],
            item["disposition"], int(item["archived"]), now(), encode(actor), encode(item["references"]), item["id"]))
        return {"item": self._item(conn, self._row(conn, item["id"]))}

    def _comment_value(self, value):
        row = dict(value)
        row["author"] = json.loads(row["author"])
        row["quote"] = json.loads(row["quote"]) if row["quote"] else None
        return row

    def _comment(self, conn, payload, actor):
        item = self._row(conn, payload["item_id"])
        content = text(payload["text"], "text", nonblank=True)
        quote_id = payload.get("quote_comment_id")
        quote = None
        if quote_id is not None:
            quote_id = identifier(quote_id, "quote_comment_id")
            original = conn.execute("SELECT * FROM comments WHERE id=? AND item_id=?", (quote_id, item["id"])).fetchone()
            if original is None:
                raise BoardError("invalid", "Quoted comment must belong to this item.")
            original = self._comment_value(original)
            quote = {k: original[k] for k in ("id", "text", "author", "created_at")}
        comment_id, stamp = str(uuid.uuid4()), now()
        conn.execute("INSERT INTO comments(id,item_id,text,created_at,author,quote) VALUES(?,?,?,?,?,?)",
                     (comment_id, item["id"], content, stamp, encode(actor), encode(quote) if quote else None))
        conn.execute("UPDATE items SET revision=revision+1,updated_at=?,updated_by=? WHERE id=?", (stamp, encode(actor), item["id"]))
        comment = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        return {"comment": self._comment_value(comment), "item": self._item(conn, self._row(conn, item["id"]))}
