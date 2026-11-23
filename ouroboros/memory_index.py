"""Derived, rebuildable SQLite FTS5 index over durable memory (`state/memory_index.sqlite`).

Indexes four authoritative memory families without altering them:
- Global and per-project knowledge notes (`memory/knowledge/**/*.md`, `projects/<id>/knowledge/**/*.md`)
- Chronicle records (`memory/chronicle/records.jsonl` via `ChronicleStore`)
- Project workpads (`projects/<id>/workpad.md`) and journals (`projects/<id>/journal.jsonl`)
- Project closure briefs (`projects/<id>/closure_brief.md`)

Purely derived and rebuildable on any missing file, schema mismatch, integrity failure, or
`sqlite3.DatabaseError`. Short transactions (`PRAGMA journal_mode=DELETE`) and a sidecar lock
(`state/memory_index.lock`) serialize concurrent writers and maintenance syncs.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml

from ouroboros.chronicle_store import ChronicleStore, capture
from ouroboros.knowledge import INDEX_FILE, KnowledgeAddress, read_knowledge_note, sanitize_topic
from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.project_facts import explicit_project_id_ok, project_closure_brief_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_INDEX_FILENAME = "memory_index.sqlite"
_LOCK_FILENAME = "memory_index.lock"

INDEXABLE_CHRONICLE_KINDS = frozenset({"page", "part", "note", "account", "legacy", "gap", "mark"})
EXCLUDED_DEFAULT_STATUSES = frozenset({"ARCHIVED", "SUPERSEDED", "REJECTED", "RELEASED", "CANDIDATE"})
VALID_EXACT_SCOPES = frozenset({"global", "story"})
_PROJECT_PRIVATE_FAMILIES = ("workpad", "journal", "closure_brief")

# Lifecycle ranking weights: active/closure > folded > archived/superseded/released/rejected.
_LIFECYCLE_WEIGHTS: Dict[str, float] = {
    "ACTIVE": 1.20, "CLOSED": 1.15, "FOLDED": 0.85,
    "ARCHIVED": 0.55, "SUPERSEDED": 0.45, "RELEASED": 0.45, "CANDIDATE": 0.35, "REJECTED": 0.30,
}

_PROCESS_LOCK = threading.RLock()
_SNIPPET_MAX_CHARS = 280
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

_SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sync_files (
    source_key TEXT PRIMARY KEY, path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size_bytes INTEGER NOT NULL, extra_offset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_key TEXT UNIQUE NOT NULL, source_key TEXT NOT NULL,
    family TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL,
    date_ymd TEXT NOT NULL, canonical_address TEXT NOT NULL,
    title TEXT NOT NULL, keywords TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX idx_docs_source_key ON docs(source_key);
CREATE INDEX idx_docs_scope_status ON docs(scope, status);
CREATE INDEX idx_docs_family ON docs(family);
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title, keywords, body, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, keywords, body) VALUES (new.id, new.title, new.keywords, new.body);
END;
CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, keywords, body)
    VALUES ('delete', old.id, old.title, old.keywords, old.body);
END;
CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, keywords, body)
    VALUES ('delete', old.id, old.title, old.keywords, old.body);
    INSERT INTO docs_fts(rowid, title, keywords, body) VALUES (new.id, new.title, new.keywords, new.body);
END;
"""
_REQUIRED_TABLES = frozenset({"meta", "sync_files", "docs", "docs_fts"})


@dataclass(frozen=True)
class SyncReport:
    synced: int = 0
    pruned: int = 0
    rebuilt: bool = False


@dataclass(frozen=True)
class SearchHit:
    doc_key: str; family: str; kind: str; status: str; scope: str; date_ymd: str
    canonical_address: str; title: str; snippet: str; score: float; bm25_raw: float


@dataclass(frozen=True)
class SearchResult:
    query: str; scope: str; include_archived: bool; limit: int
    hits: Tuple[SearchHit, ...]; total_candidates: int; elapsed_ms: int; rebuilt: bool

    @property
    def hit_count(self) -> int:
        return len(self.hits)


def index_path(drive_root: Any) -> pathlib.Path:
    """Path to the derived SQLite FTS5 index under `<drive_root>/state/`."""
    return pathlib.Path(drive_root) / "state" / _INDEX_FILENAME


def lock_path(drive_root: Any) -> pathlib.Path:
    """Path to the sidecar lock file serializing index writes and rebuilds."""
    return pathlib.Path(drive_root) / "state" / _LOCK_FILENAME


def closure_brief_path(drive_root: Any, project_id: str) -> pathlib.Path:
    """Path to a project's optional `closure_brief.md` under `<drive_root>/projects/<id>/`."""
    return project_closure_brief_path(project_id, drive_root=drive_root)


@contextlib.contextmanager
def _index_lock(drive_root: Any, *, timeout_sec: float = 10.0):
    lp = lock_path(drive_root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    if not _PROCESS_LOCK.acquire(timeout=timeout_sec):
        raise TimeoutError("memory_index: thread lock busy")
    try:
        fd = acquire_exclusive_file_lock(lp, timeout_sec=timeout_sec, stale_sec=90.0, owner_aware_stale=True)
        if fd is None:
            raise TimeoutError(f"memory_index: could not acquire lock {lp}")
        try:
            yield
        finally:
            release_exclusive_file_lock(lp, fd)
    finally:
        _PROCESS_LOCK.release()


def _connect(db_path: pathlib.Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    if not mode or str(mode[0]).lower() != "delete":
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def _validate_existing(conn: sqlite3.Connection) -> bool:
    try:
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            return False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _REQUIRED_TABLES <= tables:
            return False
        check = conn.execute("PRAGMA quick_check(1)").fetchone()
        if not check or str(check[0]).lower() != "ok":
            return False
        conn.execute("SELECT rowid FROM docs_fts LIMIT 1").fetchall()
        return True
    except sqlite3.DatabaseError:
        return False


def _meta_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def _meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def _stat_sig(path: pathlib.Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _mtime_ymd(mtime_ns: int) -> str:
    if not mtime_ns:
        return ""
    try:
        return datetime.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _extract_ymd(*candidates: Any, fallback_mtime_ns: int = 0) -> str:
    for value in candidates:
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, str) and _DATE_RE.match(value.strip()):
            return value.strip()[:10]
    return _mtime_ymd(fallback_mtime_ns)


def _flatten_strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [s for item in value for s in _flatten_strings(item)]
    if isinstance(value, Mapping):
        return [s for k, v in value.items() for s in (*_flatten_strings(k), *_flatten_strings(v))]
    text = str(value).strip()
    return [text] if text else []


def _first_heading_or_line(text: str, fallback: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _HEADING_RE.match(line)
        return (m.group(1).strip() if m else line)[:120]
    return fallback


def _parse_frontmatter(raw_text: str) -> Tuple[Dict[str, Any], str]:
    """Parse optional YAML frontmatter (`---` ... `---` / `...`) and return `(meta, body)`."""
    lines = raw_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, raw_text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            body = "".join(lines[idx + 1:])
            try:
                parsed = yaml.safe_load("".join(lines[1:idx]))
                return (parsed, body) if isinstance(parsed, dict) else ({}, body)
            except yaml.YAMLError:
                return {}, raw_text
    return {}, raw_text


def _load_projects_meta(root: pathlib.Path) -> Tuple[Dict[str, Dict[str, Any]], Optional[Tuple[int, int]]]:
    from ouroboros import projects_registry  # D15->D17 is lazy-only

    sig = _stat_sig(root / "state" / "projects.json")
    try:
        rows = projects_registry.list_reserved_projects(root, strict=False)
    except Exception:
        return {}, sig
    return {
        str(row["id"]).strip(): {
            "lifecycle": str(row.get("lifecycle") or projects_registry.PROJECT_ACTIVE),
            "name": str(row.get("name") or str(row["id"]).strip()),
        }
        for row in rows if isinstance(row, dict) and str(row.get("id") or "").strip()
    }, sig


def _upsert_doc(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
        """INSERT INTO docs (doc_key, source_key, family, kind, status, scope, date_ymd,
                             canonical_address, title, keywords, body)
           VALUES (:doc_key, :source_key, :family, :kind, :status, :scope, :date_ymd,
                   :canonical_address, :title, :keywords, :body)
           ON CONFLICT(doc_key) DO UPDATE SET
               source_key=excluded.source_key, family=excluded.family, kind=excluded.kind,
               status=excluded.status, scope=excluded.scope, date_ymd=excluded.date_ymd,
               canonical_address=excluded.canonical_address, title=excluded.title,
               keywords=excluded.keywords, body=excluded.body""",
        fields,
    )


def _delete_source(conn: sqlite3.Connection, source_key: str) -> int:
    cur = conn.execute("DELETE FROM docs WHERE source_key=?", (source_key,))
    conn.execute("DELETE FROM sync_files WHERE source_key=?", (source_key,))
    return int(cur.rowcount or 0)


def _record_sync_file(conn: sqlite3.Connection, source_key: str, rel_path: str,
                      mtime_ns: int, size_bytes: int, extra_offset: int = 0) -> None:
    conn.execute(
        """INSERT INTO sync_files(source_key, path, mtime_ns, size_bytes, extra_offset)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_key) DO UPDATE SET
               path=excluded.path, mtime_ns=excluded.mtime_ns,
               size_bytes=excluded.size_bytes, extra_offset=excluded.extra_offset""",
        (source_key, rel_path, mtime_ns, size_bytes, extra_offset),
    )


def _discover_project_ids(root: pathlib.Path, registry: Mapping[str, Any]) -> List[str]:
    found: Set[str] = set()
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        with contextlib.suppress(OSError):
            for entry in projects_dir.iterdir():
                if entry.is_dir() and explicit_project_id_ok(entry.name):
                    found.add(entry.name)
    for pid in registry:
        if explicit_project_id_ok(pid) and (projects_dir / pid).is_dir():
            found.add(pid)
    return sorted(found)


# --- Family (a): Knowledge notes -----------------------------------------------------------------


def _sync_knowledge_shelf(conn: sqlite3.Connection, root: pathlib.Path, shelf: pathlib.Path, project_id: str,
                          existing_files: Mapping[str, Tuple[int, int, int]], seen_keys: Set[str]) -> int:
    if not shelf.is_dir():
        return 0
    synced, scope = 0, (f"project:{project_id}" if project_id else "global")
    try:
        candidates = sorted(shelf.rglob("*.md"))
    except OSError:
        return 0
    shelf_resolved = shelf.resolve()
    for path in candidates:
        if path == shelf / INDEX_FILE or not path.is_file():
            continue
        try:
            topic = sanitize_topic(path.relative_to(shelf).as_posix()[:-3])
            path.resolve().relative_to(shelf_resolved)
        except ValueError:
            continue
        source_key = f"kn:{scope}:{topic}"
        seen_keys.add(source_key)
        sig = _stat_sig(path)
        if sig is None:
            continue
        mtime_ns, size_bytes = sig
        prev = existing_files.get(source_key)
        if prev is not None and prev[0] == mtime_ns and prev[1] == size_bytes:
            continue
        try:
            note = read_knowledge_note(KnowledgeAddress(root.resolve(), shelf_resolved, topic, project_id))
        except (OSError, UnicodeDecodeError):
            continue
        meta = note.metadata
        archive_meta = meta.get("archive") if isinstance(meta.get("archive"), Mapping) else {}
        date_ymd = _extract_ymd(meta.get("updated"), meta.get("updated_at"), meta.get("date"),
                                meta.get("created"), meta.get("created_at"), archive_meta.get("at"),
                                fallback_mtime_ns=mtime_ns)
        kw_parts = [topic, *(s for k in ("summary", "type", "tags", "keywords") for s in _flatten_strings(meta.get(k))),
                    *_flatten_strings(archive_meta.get("reason"))]
        body_text = (note.source.text_at(note.source.body_span).strip() or note.summary or note.text
                     if note.source is not None else note.text)
        _upsert_doc(
            conn, doc_key=f"knowledge:{scope}:{topic}", source_key=source_key, family="knowledge",
            kind="knowledge_note", status=("ARCHIVED" if note.state == "archived" else "ACTIVE"),
            scope=scope, date_ymd=date_ymd, canonical_address=f"knowledge_read(topic='{topic}', scope='{scope}')",
            title=note.title or topic, keywords=" ".join(kw_parts), body=body_text,
        )
        _record_sync_file(conn, source_key, path.relative_to(root).as_posix(), mtime_ns, size_bytes)
        synced += 1
    return synced


# --- Family (b): Chronicle records ---------------------------------------------------------------


def _chronicle_superseded_ids(store: ChronicleStore) -> Set[str]:
    superseded: Set[str] = set()
    for account in store.accounts():
        sel = account.get("selection")
        if not isinstance(sel, Mapping):
            continue
        if sel.get("shown") is False:
            superseded.add(str(account["id"]))
        elif sel.get("shown") is True:
            superseded.update(str(rid) for rid in (sel.get("replaces") or ()) if rid)
    return superseded


def _chronicle_record_status(record: Mapping[str, Any], *, active_mark_ids: Set[str], superseded_ids: Set[str]) -> str:
    kind, rid = str(record.get("kind") or ""), str(record.get("id") or "")
    if record.get("status") == "rejected":
        return "REJECTED"
    if record.get("status") == "candidate":
        return "CANDIDATE"
    if rid in superseded_ids:
        return "SUPERSEDED"
    if kind == "mark":
        return "ACTIVE" if rid in active_mark_ids else "RELEASED"
    if record.get("folded_into"):
        return "FOLDED"
    return "ACTIVE"


def _chronicle_record_scope(record: Mapping[str, Any]) -> str:
    kind = str(record.get("kind") or "")
    if kind == "mark" and str(record.get("scope") or "") == "global":
        return "global"
    if kind == "account":
        return "story"
    room = str(record.get("room_id") or "")
    return f"room:{room}" if room else "global"


def _chronicle_record_date(record: Mapping[str, Any], fallback_mtime_ns: int) -> str:
    covers = record.get("covers") if isinstance(record.get("covers"), Mapping) else {}
    ts_span = covers.get("ts_span") if isinstance(covers.get("ts_span"), Mapping) else {}
    raw_range = covers.get("raw_range") if isinstance(covers.get("raw_range"), Mapping) else {}
    raw_span = raw_range.get("ts_span") if isinstance(raw_range.get("ts_span"), Mapping) else {}
    return _extract_ymd(record.get("ts"), ts_span.get("end"), ts_span.get("start"),
                        raw_span.get("end"), raw_span.get("start"), fallback_mtime_ns=fallback_mtime_ns)


def _index_chronicle_record(conn: sqlite3.Connection, store: ChronicleStore, raw_record: Mapping[str, Any], *,
                            active_marks_by_id: Mapping[str, Mapping[str, Any]],
                            superseded_ids: Set[str], log_mtime_ns: int) -> bool:
    kind, rid = str(raw_record.get("kind") or ""), str(raw_record.get("id") or "")
    if kind not in INDEXABLE_CHRONICLE_KINDS or not rid:
        return False
    interpreted = store.interpreted_record(raw_record)
    status = _chronicle_record_status(interpreted, active_mark_ids=set(active_marks_by_id), superseded_ids=superseded_ids)
    scope = _chronicle_record_scope(interpreted)
    date_ymd = _chronicle_record_date(interpreted, log_mtime_ns)
    meta = interpreted.get("metadata") if isinstance(interpreted.get("metadata"), Mapping) else {}
    kw_parts: List[str] = [kind, rid]
    if interpreted.get("room_id"):
        kw_parts.append(f"room:{interpreted['room_id']}")
    if interpreted.get("task_id"):
        kw_parts.append(str(interpreted["task_id"]))
    if kind == "mark":
        shown_mark = active_marks_by_id.get(rid) or interpreted
        body_parts = [str(shown_mark.get("text") or "")]
        if shown_mark.get("quote"):
            body_parts.append(f"quote: {shown_mark['quote']}")
            kw_parts.append(str(shown_mark["quote"]))
        if isinstance(shown_mark.get("anchor"), Mapping):
            kw_parts.extend(_flatten_strings(shown_mark["anchor"]))
        body = "\n".join(p for p in body_parts if p)
        title = _first_heading_or_line(str(shown_mark.get("text") or ""), f"mark {rid}")
    else:
        body = str(interpreted.get("current_text") or interpreted.get("text") or "")
        kw_parts.extend(str(q["text"]) for q in (interpreted.get("quotes") or ()) if isinstance(q, Mapping) and q.get("text"))
        title = (str(meta["label"])[:120] if kind == "legacy" and meta.get("label")
                 else _first_heading_or_line(str(interpreted.get("text") or ""), f"{kind} {rid}"))
    _upsert_doc(
        conn, doc_key=f"chronicle:{rid}", source_key="chronicle", family="chronicle", kind=kind,
        status=status, scope=scope, date_ymd=date_ymd, canonical_address=f"memory_read(node_id='{rid}')",
        title=title, keywords=" ".join(kw_parts), body=body,
    )
    return True


def _sync_chronicle(conn: sqlite3.Connection, root: pathlib.Path) -> Tuple[int, int]:
    store = ChronicleStore(root)
    sig = _stat_sig(store.log_path)
    if sig is None:
        pruned = _delete_source(conn, "chronicle")
        if _meta_get(conn, "chronicle_log_sig") is not None or _meta_get(conn, "chronicle_seq", 0) != 0:
            _meta_set(conn, "chronicle_seq", 0)
            _meta_set(conn, "chronicle_log_sig", None)
            _meta_set(conn, "chronicle_superseded_ids", [])
        return 0, pruned
    log_mtime_ns, log_size = sig
    stored_sig = _meta_get(conn, "chronicle_log_sig")
    watermark = int(_meta_get(conn, "chronicle_seq", 0) or 0)
    if stored_sig == [log_mtime_ns, log_size] and watermark >= 0:
        return 0, 0
    full_reindex = isinstance(stored_sig, list) and len(stored_sig) == 2 and log_size < int(stored_sig[1])
    with capture(root, tail="none"):
        try:
            pruned = _delete_source(conn, "chronicle") if full_reindex else 0
            if full_reindex:
                watermark = 0
            new_records = store.records(after_seq=watermark)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            log.debug("memory_index: chronicle read skipped: %s", exc)
            return 0, 0
        if not new_records and not full_reindex:
            _meta_set(conn, "chronicle_log_sig", [log_mtime_ns, log_size])
            return 0, 0

        max_seq, affected_ids = watermark, set()
        selection_changed = full_reindex or watermark == 0
        for rec in new_records:
            max_seq = max(max_seq, int(rec.get("sequence") or 0))
            kind, rid = str(rec.get("kind") or ""), str(rec.get("id") or "")
            if kind in INDEXABLE_CHRONICLE_KINDS and rid:
                affected_ids.add(rid)
            if kind in ("correction", "decision", "mark_release", "mark_view") and rec.get("target_id"):
                affected_ids.add(str(rec["target_id"]))
            if kind == "part" and isinstance(rec.get("covers"), Mapping):
                affected_ids.update(str(mid) for mid in (rec["covers"].get("member_ids") or ()) if mid)
            if kind == "selection":
                selection_changed = True
                if rec.get("target_id"):
                    affected_ids.add(str(rec["target_id"]))

        prev_superseded = set(_meta_get(conn, "chronicle_superseded_ids", []) or [])
        superseded_ids = _chronicle_superseded_ids(store) if selection_changed else prev_superseded
        if selection_changed:
            affected_ids |= (superseded_ids ^ prev_superseded)

        active_marks_by_id = {str(m["id"]): m for m in store.active_marks() if m.get("id")}
        synced = 0
        for rid in sorted(affected_ids):
            rec = store.get(rid)
            if rec is not None and _index_chronicle_record(
                conn, store, rec, active_marks_by_id=active_marks_by_id,
                superseded_ids=superseded_ids, log_mtime_ns=log_mtime_ns,
            ):
                synced += 1

    _meta_set(conn, "chronicle_seq", max_seq)
    _meta_set(conn, "chronicle_log_sig", [log_mtime_ns, log_size])
    _meta_set(conn, "chronicle_superseded_ids", sorted(superseded_ids))
    return synced, pruned


# --- Family (c): Project workpads & journals -----------------------------------------------------


def _sync_workpad(conn: sqlite3.Connection, root: pathlib.Path, project_id: str, project_name: str, status: str,
                  existing_files: Mapping[str, Tuple[int, int, int]], seen_keys: Set[str],
                  status_changed: bool) -> int:
    path, source_key = root / "projects" / project_id / "workpad.md", f"wp:{project_id}"
    sig = _stat_sig(path)
    if sig is None: return 0
    seen_keys.add(source_key)
    mtime_ns, size_bytes = sig
    prev = existing_files.get(source_key)
    if prev is not None and prev[0] == mtime_ns and prev[1] == size_bytes and not status_changed:
        return 0
    try: text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError: return 0
    if not text:
        _delete_source(conn, source_key)
        _record_sync_file(conn, source_key, path.relative_to(root).as_posix(), mtime_ns, size_bytes)
        return 0
    _upsert_doc(
        conn, doc_key=f"workpad:{project_id}", source_key=source_key, family="workpad", kind="workpad",
        status=status, scope=f"project:{project_id}", date_ymd=_mtime_ymd(mtime_ns),
        canonical_address=f"workpad_read(project_id='{project_id}')",
        title=_first_heading_or_line(text, f"{project_name} workpad"),
        keywords=f"{project_id} {project_name} workpad", body=text,
    )
    _record_sync_file(conn, source_key, path.relative_to(root).as_posix(), mtime_ns, size_bytes)
    return 1


def _sync_journal(conn: sqlite3.Connection, root: pathlib.Path, project_id: str, project_name: str, status: str,
                  existing_files: Mapping[str, Tuple[int, int, int]], seen_keys: Set[str],
                  status_changed: bool) -> int:
    path, source_key = root / "projects" / project_id / "journal.jsonl", f"jn:{project_id}"
    sig = _stat_sig(path)
    if sig is None:
        return 0
    seen_keys.add(source_key)
    mtime_ns, size_bytes = sig
    prev = existing_files.get(source_key)
    stored_offset = prev[2] if prev is not None else 0
    if prev is not None and prev[0] == mtime_ns and prev[1] == size_bytes:
        if status_changed:
            conn.execute("UPDATE docs SET status=? WHERE source_key=?", (status, source_key))
            return 1
        return 0
    if status_changed and prev is not None:
        conn.execute("UPDATE docs SET status=? WHERE source_key=?", (status, source_key))
    if size_bytes < stored_offset:
        conn.execute("DELETE FROM docs WHERE source_key=?", (source_key,))
        stored_offset = 0

    synced, new_offset = 0, stored_offset
    try:
        with path.open("rb") as fh:
            fh.seek(stored_offset)
            while True:
                line_start = fh.tell()
                raw_line = fh.readline()
                if not raw_line:
                    break
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    if raw_line.endswith(b"\n"):
                        new_offset = line_start + len(raw_line)
                        continue
                    break
                new_offset = line_start + len(raw_line)
                if not isinstance(row, dict):
                    continue
                body = str(row.get("text") or "").strip()
                if not body:
                    continue
                entry_kind = str(row.get("kind") or "note").strip() or "note"
                task_id = str(row.get("task_id") or "").strip()
                _upsert_doc(
                    conn, doc_key=f"journal:{project_id}:{line_start}", source_key=source_key,
                    family="journal", kind=f"journal:{entry_kind}", status=status, scope=f"project:{project_id}",
                    date_ymd=_extract_ymd(row.get("ts"), fallback_mtime_ns=mtime_ns),
                    canonical_address=f"journal_read(project_id='{project_id}')",
                    title=f"{project_name} journal ({entry_kind})",
                    keywords=" ".join(p for p in (project_id, project_name, entry_kind, task_id) if p),
                    body=body,
                )
                synced += 1
    except OSError:
        return synced
    _record_sync_file(conn, source_key, path.relative_to(root).as_posix(), mtime_ns, size_bytes, new_offset)
    return synced


# --- Family (d): Project closure briefs ----------------------------------------------------------


def _sync_closure_brief(conn: sqlite3.Connection, root: pathlib.Path, project_id: str, project_name: str,
                        existing_files: Mapping[str, Tuple[int, int, int]], seen_keys: Set[str]) -> int:
    path, source_key = root / "projects" / project_id / "closure_brief.md", f"cb:{project_id}"
    sig = _stat_sig(path)
    if sig is None: return 0
    seen_keys.add(source_key)
    mtime_ns, size_bytes = sig
    prev = existing_files.get(source_key)
    if prev is not None and prev[0] == mtime_ns and prev[1] == size_bytes: return 0
    try: raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError: return 0
    fm, body = _parse_frontmatter(raw_text)
    body_clean, summary = body.strip(), str(fm.get("summary") or "").strip()
    title = str(fm.get("title") or "").strip() or _first_heading_or_line(body_clean, f"{project_name} closure brief")
    date_ymd = _extract_ymd(fm.get("closed_at"), fm.get("updated_at"), fm.get("updated"),
                            fm.get("date"), fm.get("created_at"), fallback_mtime_ns=mtime_ns)
    kw_parts: List[str] = [project_id, project_name, "closure_brief"]
    for field in ("summary", "keywords", "touched_paths", "commit_shas", "error_signatures", "supersedes"):
        kw_parts.extend(_flatten_strings(fm.get(field)))
    indexed_body = (f"{summary}\n\n{body_clean}".strip() if summary and summary not in body_clean
                    else (body_clean or summary))
    _upsert_doc(
        conn, doc_key=f"closure_brief:{project_id}", source_key=source_key, family="closure_brief",
        kind="closure_brief", status="CLOSED", scope=f"project:{project_id}", date_ymd=date_ymd,
        canonical_address=f"memory_read(node_id='closure_brief:{project_id}')",
        title=title, keywords=" ".join(kw_parts), body=indexed_body,
    )
    _record_sync_file(conn, source_key, path.relative_to(root).as_posix(), mtime_ns, size_bytes)
    return 1


# --- Full sync & rebuild -------------------------------------------------------------------------


def _sync_into_connection(conn: sqlite3.Connection, root: pathlib.Path) -> Tuple[int, int]:
    existing_files = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in conn.execute("SELECT source_key, mtime_ns, size_bytes, extra_offset FROM sync_files")}
    seen_keys: Set[str] = set()
    synced = pruned = 0
    registry, reg_sig = _load_projects_meta(root)
    prev_lifecycles = _meta_get(conn, "project_lifecycles", {}) or {}
    cur_lifecycles = {pid: info["lifecycle"] for pid, info in registry.items()}
    synced += _sync_knowledge_shelf(conn, root, root / "memory" / "knowledge", "", existing_files, seen_keys)
    for pid in _discover_project_ids(root, registry):
        info = registry.get(pid) or {}
        project_name, lifecycle = str(info.get("name") or pid), str(info.get("lifecycle") or "active")
        status, status_changed = ("CLOSED" if lifecycle == "tombstoned" else "ACTIVE"), (prev_lifecycles.get(pid) != lifecycle)
        synced += _sync_knowledge_shelf(conn, root, root / "projects" / pid / "knowledge", pid, existing_files, seen_keys)
        synced += _sync_workpad(conn, root, pid, project_name, status, existing_files, seen_keys, status_changed)
        synced += _sync_journal(conn, root, pid, project_name, status, existing_files, seen_keys, status_changed)
        synced += _sync_closure_brief(conn, root, pid, project_name, existing_files, seen_keys)
    for stale_key in sorted(set(existing_files) - seen_keys):
        pruned += _delete_source(conn, stale_key)
    c_synced, c_pruned = _sync_chronicle(conn, root)
    if prev_lifecycles != cur_lifecycles:
        _meta_set(conn, "project_lifecycles", cur_lifecycles)
    cur_reg_sig = list(reg_sig) if reg_sig is not None else None
    if _meta_get(conn, "projects_reg_sig") != cur_reg_sig:
        _meta_set(conn, "projects_reg_sig", cur_reg_sig)
    return synced + c_synced, pruned + c_pruned


def _replace_with_retry(src: pathlib.Path, dst: pathlib.Path) -> None:
    for attempt in range(6):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def _rebuild_locked(root: pathlib.Path) -> SyncReport:
    """Build a fresh SQLite index in a sibling temp file and atomically replace `state/memory_index.sqlite`."""
    db_path = index_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(f".{_INDEX_FILENAME}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}")
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(tmp_path)
        _init_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        synced, pruned = _sync_into_connection(conn, root)
        conn.execute("COMMIT")
        conn.close()
        conn = None
        _replace_with_retry(tmp_path, db_path)
        for suffix in ("-journal", "-wal", "-shm"):
            with contextlib.suppress(OSError): pathlib.Path(str(db_path) + suffix).unlink()
        return SyncReport(synced=synced, pruned=pruned, rebuilt=True)
    except Exception:
        if conn is not None:
            with contextlib.suppress(Exception): conn.close()
        with contextlib.suppress(OSError): tmp_path.unlink()
        raise


def rebuild_index(drive_root: Any) -> SyncReport:
    """Force a clean full rebuild of `state/memory_index.sqlite` from authoritative sources."""
    with _index_lock(drive_root):
        return _rebuild_locked(pathlib.Path(drive_root))


def sync_index(drive_root: Any) -> SyncReport:
    """Bring `state/memory_index.sqlite` up to date incrementally, rebuilding on corruption or schema drift."""
    root, db_path = pathlib.Path(drive_root), index_path(drive_root)
    with _index_lock(root):
        if not db_path.is_file():
            return _rebuild_locked(root)
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _connect(db_path)
            if not _validate_existing(conn):
                conn.close()
                conn = None
                return _rebuild_locked(root)
            conn.execute("BEGIN")
            synced, pruned = _sync_into_connection(conn, root)
            if conn.in_transaction:
                conn.execute("COMMIT" if conn.total_changes > 0 else "ROLLBACK")
            conn.close()
            return SyncReport(synced=synced, pruned=pruned, rebuilt=False)
        except sqlite3.DatabaseError as exc:
            log.warning("memory_index: SQLite error during sync (%s); rebuilding cleanly", exc)
            if conn is not None:
                with contextlib.suppress(Exception): conn.execute("ROLLBACK")
                with contextlib.suppress(Exception): conn.close()
            return _rebuild_locked(root)
        except Exception:
            if conn is not None:
                with contextlib.suppress(Exception):
                    if conn.in_transaction: conn.execute("ROLLBACK")
                with contextlib.suppress(Exception): conn.close()
            raise


# --- Query escaping, composite ranking & formatting ----------------------------------------------


def _tokenize_fts_query(raw_query: str) -> str:
    """Convert arbitrary user text (paths, SHAs, colons, hyphens, quotes, operators) into a safe FTS5 query."""
    text = str(raw_query or "").strip()
    if not text:
        return ""
    tokens: List[str] = []
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace(): idx += 1
        if idx >= n: break
        if text[idx] == '"':
            end = text.find('"', idx + 1)
            if end != -1:
                phrase = text[idx + 1:end].strip()
                if _WORD_RE.search(phrase): tokens.append(phrase)
                idx = end + 1
                continue
        start = idx
        while idx < n and not text[idx].isspace(): idx += 1
        part = text[start:idx].strip('"')
        if _WORD_RE.search(part): tokens.append(part)
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)


def _fallback_fts_query(raw_query: str) -> str:
    words = _WORD_RE.findall(str(raw_query or ""))
    return " ".join(f'"{w}"' for w in words) if words else ""


def validate_scope(scope: str) -> str:
    """Validate optional search scope filter (`global`, `story`, `project:<id>`, `room:<id>`)."""
    text = str(scope or "").strip()
    if not text or text in VALID_EXACT_SCOPES:
        return text
    if text.startswith("project:"):
        pid = text[len("project:"):]
        if pid and explicit_project_id_ok(pid):
            return text
        raise ValueError(f"invalid project scope {text!r}: expected project:<exact project id>")
    if text.startswith("room:"):
        rid = text[len("room:"):]
        if rid and (rid == "legacy" or rid.lstrip("-").isdigit()):
            return text
        raise ValueError(f"invalid room scope {text!r}: expected room:<chat_id>")
    raise ValueError(f"invalid scope {text!r}: expected global, story, project:<id>, or room:<chat_id>")


def _recency_multiplier(date_ymd: str, today: datetime.date) -> float:
    if not date_ymd or not _DATE_RE.match(date_ymd):
        return 1.0
    try:
        age_days = max(0, (today - datetime.date.fromisoformat(date_ymd[:10])).days)
    except ValueError:
        return 1.0
    return 1.0 + 0.35 / (1.0 + age_days / 30.0)


def _composite_score(bm25_raw: float, status: str, date_ymd: str, today: datetime.date) -> float:
    # SQLite bm25() is negative (more negative = better match). Adding 1.0 preserves lifecycle
    # and recency ordering even on tiny tables where IDF approaches 0.
    base_rel = 1.0 + max(0.0, -float(bm25_raw))
    return base_rel * _LIFECYCLE_WEIGHTS.get(status, 0.50) * _recency_multiplier(date_ymd, today)


def _clean_snippet(raw_snippet: str, fallback_body: str, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    text = " ".join(str(raw_snippet or fallback_body or "").split())
    return text if len(text) <= max_chars else text[: max(0, max_chars - 1)].rstrip() + "…"


def _run_fts_query(conn: sqlite3.Connection, fts_expr: str, *, scope: str,
                   include_archived: bool, fetch_limit: int,
                   allowed_project_id: Optional[str] = None) -> List[Tuple[Any, ...]]:
    where: List[str] = ["docs_fts MATCH ?"]
    params: List[Any] = [fts_expr]
    if scope:
        where.append("d.scope = ?")
        params.append(scope)
    if allowed_project_id is not None:
        placeholders = ",".join("?" for _ in _PROJECT_PRIVATE_FAMILIES)
        if allowed_project_id:
            where.append(f"(d.family NOT IN ({placeholders}) OR d.scope = ?)")
            params.extend(_PROJECT_PRIVATE_FAMILIES)
            params.append(f"project:{allowed_project_id}")
        else:
            where.append(f"d.family NOT IN ({placeholders})")
            params.extend(_PROJECT_PRIVATE_FAMILIES)
    if not include_archived:
        where.append("d.status NOT IN (%s)" % ",".join("?" for _ in EXCLUDED_DEFAULT_STATUSES))
        params.extend(sorted(EXCLUDED_DEFAULT_STATUSES))
    # A hit names the next place to read: documents sharing one address (a project's journal
    # entries) collapse to their best match BEFORE the fetch limit, so many short metadata-heavy
    # entries can neither crowd out the brief or workpad nor shrink the candidate pool.
    sql = (
        "WITH ranked AS (SELECT d.id AS id, d.canonical_address AS addr, bm25(docs_fts, 3.0, 2.0, 1.0) AS rank_score "
        "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE " + " AND ".join(where) + "), "
        "best AS (SELECT id, rank_score, ROW_NUMBER() OVER (PARTITION BY addr ORDER BY rank_score, id) AS rn "
        "FROM ranked), top AS (SELECT id, rank_score FROM best WHERE rn = 1 ORDER BY rank_score ASC LIMIT ?) "
        "SELECT d.doc_key, d.family, d.kind, d.status, d.scope, d.date_ymd, d.canonical_address, "
        "d.title, d.body, snippet(docs_fts, -1, '[', ']', '...', 24), top.rank_score "
        "FROM top JOIN docs d ON d.id = top.id JOIN docs_fts ON docs_fts.rowid = top.id "
        "WHERE docs_fts MATCH ? ORDER BY top.rank_score ASC"
    )
    params.extend([fetch_limit, fts_expr])
    return conn.execute(sql, params).fetchall()


def search_memory(drive_root: Any, query: str, *, scope: str = "", include_archived: bool = False,
                  limit: int = 10, today: Optional[datetime.date] = None,
                  allowed_project_id: Optional[str] = None) -> SearchResult:
    """Synchronize the derived index lazily and execute a ranked FTS5 search."""
    started = time.monotonic()
    root = pathlib.Path(drive_root)
    norm_scope = validate_scope(scope)
    if allowed_project_id is not None and allowed_project_id != "" and not explicit_project_id_ok(allowed_project_id):
        raise ValueError(f"invalid allowed_project_id {allowed_project_id!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 50): raise ValueError("limit must be 1..50")
    raw_query = str(query or "").strip()
    if not raw_query: raise ValueError("query must be a non-empty string")

    rebuilt = sync_index(root).rebuilt
    fts_expr = _tokenize_fts_query(raw_query)
    if not fts_expr:
        return SearchResult(raw_query, norm_scope, bool(include_archived), limit, (), 0,
                            int((time.monotonic() - started) * 1000), rebuilt)

    fetch_limit, db_path = max(limit * 5, 100), index_path(root)

    def _execute() -> List[Tuple[Any, ...]]:
        conn = _connect(db_path, read_only=True)
        try:
            try:
                return _run_fts_query(conn, fts_expr, scope=norm_scope,
                                      include_archived=bool(include_archived), fetch_limit=fetch_limit,
                                      allowed_project_id=allowed_project_id)
            except sqlite3.OperationalError:
                fb = _fallback_fts_query(raw_query)
                if not fb or fb == fts_expr:
                    return []
                return _run_fts_query(conn, fb, scope=norm_scope,
                                      include_archived=bool(include_archived), fetch_limit=fetch_limit,
                                      allowed_project_id=allowed_project_id)
        finally:
            conn.close()

    try:
        rows = _execute()
    except sqlite3.DatabaseError:
        rebuild_index(root)
        rebuilt = True
        rows = _execute()

    return _build_search_result(raw_query, norm_scope, bool(include_archived), limit, rows, started, rebuilt, today)


def _build_search_result(
    raw_query: str, norm_scope: str, include_archived: bool, limit: int,
    rows: List[Tuple[Any, ...]], started: float, rebuilt: bool, today: Optional[datetime.date],
) -> SearchResult:
    ref_today = today or datetime.datetime.now(datetime.timezone.utc).date()
    ranked = [
        SearchHit(
            doc_key=str(r[0]), family=str(r[1]), kind=str(r[2]), status=str(r[3]), scope=str(r[4]),
            date_ymd=str(r[5]), canonical_address=str(r[6]), title=str(r[7]),
            snippet=_clean_snippet(str(r[9] or ""), str(r[8] or "")),
            score=_composite_score(float(r[10]), str(r[3]), str(r[5]), ref_today),
            bm25_raw=float(r[10]),
        )
        for r in rows
    ]
    ranked.sort(key=lambda h: (-h.score, h.date_ymd == "", h.doc_key))
    return SearchResult(raw_query, norm_scope, include_archived, limit, tuple(ranked[:limit]),
                        len(rows), int((time.monotonic() - started) * 1000), rebuilt)


def search_readonly(drive_root: Any, query: str, *, scope: str = "", include_archived: bool = False,
                    limit: int = 10, today: Optional[datetime.date] = None,
                    allowed_project_id: Optional[str] = None) -> Optional[SearchResult]:
    """Execute a strictly read-only FTS5 search without taking locks, syncing, or creating files.

    Returns None if the index file is absent or unreadable so callers can fall back cleanly.
    """
    started = time.monotonic()
    root = pathlib.Path(drive_root)
    norm_scope = validate_scope(scope)
    if allowed_project_id is not None and allowed_project_id != "" and not explicit_project_id_ok(allowed_project_id):
        raise ValueError(f"invalid allowed_project_id {allowed_project_id!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 50):
        raise ValueError("limit must be 1..50")
    raw_query = str(query or "").strip()
    if not raw_query:
        raise ValueError("query must be a non-empty string")

    db_path = index_path(root)
    if not db_path.is_file():
        return None
    fts_expr = _tokenize_fts_query(raw_query)
    if not fts_expr:
        return SearchResult(raw_query, norm_scope, bool(include_archived), limit, (), 0,
                            int((time.monotonic() - started) * 1000), False)

    fetch_limit = max(limit * 5, 100)
    try:
        conn = _connect(db_path, read_only=True)
        try:
            if not _validate_existing(conn):
                return None
            try:
                rows = _run_fts_query(conn, fts_expr, scope=norm_scope,
                                      include_archived=bool(include_archived), fetch_limit=fetch_limit,
                                      allowed_project_id=allowed_project_id)
            except sqlite3.OperationalError:
                fb = _fallback_fts_query(raw_query)
                if not fb or fb == fts_expr:
                    return SearchResult(raw_query, norm_scope, bool(include_archived), limit, (), 0,
                                        int((time.monotonic() - started) * 1000), False)
                rows = _run_fts_query(conn, fb, scope=norm_scope,
                                      include_archived=bool(include_archived), fetch_limit=fetch_limit,
                                      allowed_project_id=allowed_project_id)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None

    return _build_search_result(raw_query, norm_scope, bool(include_archived), limit, rows, started, False, today)


def format_search_result(result: SearchResult, *, max_chars: int = 80_000) -> str:
    """Format ranked search hits with mandatory `[STATUS | YYYY-MM-DD | scope | canonical_address]` badges."""
    rebuilt_note = "; index_rebuilt=true" if result.rebuilt else ""
    header = (f"memory_search: query={result.query!r}; scope={result.scope or 'all'}; "
              f"include_archived={'true' if result.include_archived else 'false'}; "
              f"hits={result.hit_count}{rebuilt_note}")
    if not result.hits:
        return f"{header}\ncomplete: no matching memory records"
    lines = [header, f"showing {result.hit_count} ranked hit{'s' if result.hit_count != 1 else ''} (limit={result.limit})"]
    used = sum(len(line) + 1 for line in lines)
    for idx, hit in enumerate(result.hits, 1):
        badge = f"[{hit.status} | {hit.date_ymd or 'unknown'} | {hit.scope} | {hit.canonical_address}]"
        entry = f"{idx}. {badge} {hit.title} ({hit.kind})\n   {hit.snippet}"
        if used + len(entry) + 1 > max_chars and idx > 1:
            lines.append(f"... ({result.hit_count - idx + 1} further hit(s) omitted at output limit)")
            break
        lines.append(entry)
        used += len(entry) + 1
    return "\n".join(lines)


# --- O(1) maintenance watermark & job helpers ----------------------------------------------------


def _has_any_entry(directory: pathlib.Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def source_watermark(drive_root: Any) -> Dict[str, Any]:
    """O(1) best-effort top-level source watermark (nested edits sync lazily on ``memory_read(query=...)``)."""
    root = pathlib.Path(drive_root)
    _sig_list = lambda p: (list(s) if (s := _stat_sig(p)) is not None else None)
    _dm = lambda p: (s[0] if (s := _stat_sig(p)) is not None else 0)
    return {
        "schema_version": SCHEMA_VERSION, "index": _sig_list(index_path(root)),
        "chronicle": _sig_list(root / "memory" / "chronicle" / "records.jsonl"),
        "projects_reg": _sig_list(root / "state" / "projects.json"),
        "knowledge_index": _sig_list(root / "memory" / "knowledge" / INDEX_FILE),
        "knowledge_dir_mtime": _dm(root / "memory" / "knowledge"),
        "projects_dir_mtime": _dm(root / "projects"),
    }


def maintenance_due(drive_root: Any, row: Mapping[str, Any], *, child: bool = False) -> bool:
    """Return True when the derived memory index should be synced by the maintenance child."""
    root = pathlib.Path(drive_root)
    if row.get("state") == "failed":
        return child
    mark = source_watermark(root)
    if mark["index"] is None:
        return ((root / "memory" / "chronicle" / "records.jsonl").is_file()
                or _has_any_entry(root / "memory" / "knowledge") or _has_any_entry(root / "projects"))
    return row.get("state") != "done" or row.get("watermark") != mark
