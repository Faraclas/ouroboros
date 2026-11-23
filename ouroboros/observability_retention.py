"""14-day rolling observability retention, holds, bounded tombstones, and Storage Free wiring.

Shared SSOT for ``maintenance.py`` (``observability_retention`` job, placed before
``evidence_packing`` so ``evidence_packing``'s quiet revision stays last under B6) and
``storage_free.py`` (``aged_observability``, owner-confirmed, unchecked by default).

Invariants:
1. Mandatory 14-day rolling retention (``OBSERVABILITY_RETENTION_DAYS = 14``;
   ``OUROBOROS_OBSERVABILITY_RETENTION_DAYS`` stays retired). Prunes only across
   ``observability/packs/``, ``observability/calls/``, and unreferenced ``observability/blobs/``;
   ``observability/salvaged/`` and ``*.model_send_violation.json`` are never pruned.
2. Tasks with a ``tasks`` row in ``state/current.sqlite`` must be terminal (``SETTLED_STATUSES``)
   and use their settled timestamp (``drive_retention_at`` / ``updated_at`` / ``ts``) for pack age;
   loose ``calls/<task>/*.json``, loose ``blobs/*.gz``, and storeless packs require ``mtime < cutoff``.
3. Strict fail-closed holds: live/non-terminal task or root, active/deleting projects, open reviews
   (``review_collection_pending`` or pending ``state/review_ledger/``), open delegated custody /
   task pending flags, and unresolved usage (``_OPEN_PREDICATE`` via ``attempts_open`` +
   ``dirty_owners`` + ``physical_failure.evidence_ref`` blobs).
4. ``observability/pruned_manifest.json`` is written BEFORE unlink (rolled back on failed removal)
   and ``observability/packs/index.sqlite`` is evicted under ``packs_index.lock`` so readers raise
   typed ``PrunedByRetentionError`` (``pruned_by_retention``) instead of missing or false corrupt.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ouroboros import artifact_retention, evidence_packs, maintenance_jobs, projects_registry, retention, store, usage_store
from ouroboros.utils import append_jsonl, atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

JOB = "observability_retention"
OBSERVABILITY_RETENTION_DAYS = 14
PRUNED_MANIFEST_NAME = "pruned_manifest.json"
PRUNED_STATUS = "pruned_by_retention"
MAX_PRUNED_TASKS = 50000
MAX_PRUNED_BLOBS = 100000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_CACHE_LOCK = threading.Lock()
_MANIFEST_CACHE: Dict[str, Tuple[Tuple[int, int, int], Dict[str, Any]]] = {}
_MANIFEST_CACHE_MAX = 16


def pruned_manifest_path(root: Any) -> pathlib.Path:
    return pathlib.Path(root) / "observability" / PRUNED_MANIFEST_NAME
def _stat_sig(path: pathlib.Path) -> Optional[List[int]]:
    try: st = path.lstat(); return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError: return None
def _load_pruned_manifest(root: Any) -> Optional[Dict[str, Any]]:
    path = pruned_manifest_path(root)
    try: st = path.lstat()
    except OSError: return None
    stamp, key = (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size)), str(path)
    with _MANIFEST_CACHE_LOCK:
        cached = _MANIFEST_CACHE.get(key)
        if cached is not None and cached[0] == stamp: return cached[1]
    doc = read_json_dict(path)
    if not isinstance(doc, dict) or doc.get("status") != PRUNED_STATUS: return None
    with _MANIFEST_CACHE_LOCK:
        if len(_MANIFEST_CACHE) >= _MANIFEST_CACHE_MAX: _MANIFEST_CACHE.pop(next(iter(_MANIFEST_CACHE)))
        _MANIFEST_CACHE[key] = (stamp, doc)
    return doc
def pruned_task_entry(root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    tid = str(task_id or "").strip()
    if not tid: return None
    doc = _load_pruned_manifest(root)
    entry = (doc.get("pruned_tasks") or {}).get(tid) if isinstance(doc, dict) and isinstance(doc.get("pruned_tasks"), dict) else None
    return dict(entry) if isinstance(entry, dict) else _query_pruned_index_row(pathlib.Path(root), "SELECT task_id, pruned_at, reason FROM pruned_tasks WHERE task_id=?", (tid,))
def pruned_blob_entry(root: Any, sha256: str) -> Optional[Dict[str, Any]]:
    digest = str(sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest): return None
    doc = _load_pruned_manifest(root)
    entry = (doc.get("pruned_blobs") or {}).get(digest) if isinstance(doc, dict) and isinstance(doc.get("pruned_blobs"), dict) else None
    return dict(entry) if isinstance(entry, dict) else _query_pruned_index_row(pathlib.Path(root), "SELECT sha256, kind, task_id, pruned_at, reason FROM pruned_members WHERE sha256=? LIMIT 1", (digest,))
def _query_pruned_index_row(root: pathlib.Path, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    idx_path, tier = evidence_packs.packs_dir(root) / evidence_packs.INDEX_NAME, evidence_packs.index_tier(root)
    if not idx_path.is_file() or not evidence_packs.index_valid(idx_path, tier, wait_sec=0.0): return None
    with contextlib.suppress(sqlite3.Error, OSError), contextlib.closing(store.connect(idx_path, tier, wait_sec=0.0)) as conn:
        row = conn.execute(sql, params).fetchone()
        if row is not None: return dict(row)
    return None

def hydrate_pruned_index_rows(root: Any, conn: sqlite3.Connection) -> None:
    doc = _load_pruned_manifest(root)
    if not isinstance(doc, dict):
        return
    for tid, entry in (doc.get("pruned_tasks") or {}).items():
        if isinstance(entry, dict) and tid:
            conn.execute("INSERT OR REPLACE INTO pruned_tasks VALUES (?,?,?)", (str(tid), str(entry.get("pruned_at") or ""), str(entry.get("reason") or "")))
    for sha, entry in (doc.get("pruned_blobs") or {}).items():
        if isinstance(entry, dict) and sha and entry.get("kind"):
            conn.execute("INSERT OR REPLACE INTO pruned_members VALUES (?,?,?,?,?)", (str(sha), str(entry["kind"]), str(entry.get("task_id") or ""), str(entry.get("pruned_at") or ""), str(entry.get("reason") or "")))

def pruned_handle_result(root: Any, task_id: str, rel_path: str, *, info_only: bool, include_pruned: bool) -> Any:
    """Shared checkpoint-handle retention lookup called by ``evidence_packs`` on miss."""
    if pathlib.PurePosixPath(rel_path).parts[:2] != evidence_packs.CHECKPOINTS_REL.parts:
        return None
    entry = pruned_task_entry(root, str(task_id))
    if entry is None:
        return None
    if info_only:
        return {"status": PRUNED_STATUS, "pruned_at": entry.get("pruned_at"), "reason": entry.get("reason"), "loose": False} if include_pruned else None
    raise evidence_packs.PrunedByRetentionError(f"pruned_by_retention: checkpoint handle {task_id}/{rel_path}", entry=entry)


def _evict_task_packs(root: pathlib.Path, task_id: str, files_to_unlink: List[pathlib.Path], *,
                      member_tombstones: Optional[Dict[str, Dict[str, Any]]] = None,
                      reason: str = "observability_14d_retention", pruned_at: str = "") -> List[pathlib.Path]:
    """Unlink pruned pack/sidecar files and delete their ``index.sqlite`` rows under ``packs_index.lock``."""
    failed: List[pathlib.Path] = []
    wait = evidence_packs.index_wait()
    with store.name_lock(root, evidence_packs.INDEX_LOCK_NAME, timeout_sec=wait):
        evidence_packs.close_pack_readers(files_to_unlink)
        for path in files_to_unlink:
            try:
                path.unlink()
            except FileNotFoundError: pass
            except OSError: failed.append(path)
        idx_path, tier = evidence_packs.packs_dir(root) / evidence_packs.INDEX_NAME, evidence_packs.index_tier(root)
        if not failed and idx_path.is_file() and evidence_packs.index_valid(idx_path, tier, wait_sec=wait):
            with contextlib.suppress(sqlite3.Error, OSError), contextlib.closing(store.connect(idx_path, tier, wait_sec=wait)) as conn:
                with store.transaction(conn, write=True):
                    for table in ("packs", "members", "handles", "call_manifests", "swept"):
                        conn.execute(f"DELETE FROM {table} WHERE task_id=?", (str(task_id),))
                    stamp = pruned_at or utc_now_iso()
                    conn.execute("INSERT OR REPLACE INTO pruned_tasks VALUES (?,?,?)", (str(task_id), stamp, str(reason)))
                    for sha, info in (member_tombstones or {}).items():
                        if info.get("kind"):
                            conn.execute("INSERT OR REPLACE INTO pruned_members VALUES (?,?,?,?,?)", (str(sha), str(info["kind"]), str(task_id), stamp, str(reason)))
    return failed
def _trim_tombstones(mapping: Dict[str, Dict[str, Any]], cap: int) -> Tuple[int, int]:
    if len(mapping) <= cap:
        return 0, 0
    ordered = sorted(mapping.items(), key=lambda kv: (str(kv[1].get("pruned_at") or ""), kv[0]))
    dropped_items = ordered[: len(ordered) - cap]
    for key, _val in dropped_items:
        mapping.pop(key, None)
    return len(dropped_items), sum(int(v.get("bytes") or 0) for _, v in dropped_items)


def _merge_pruned_manifest(root: pathlib.Path, *, add_tasks: Optional[Dict[str, Dict[str, Any]]] = None,
                           add_blobs: Optional[Dict[str, Dict[str, Any]]] = None, remove_tasks: Optional[Iterable[str]] = None,
                           remove_blobs: Optional[Iterable[str]] = None, reason: str = "observability_14d_retention") -> Dict[str, Any]:
    path = pruned_manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_json_dict(path) or {}
    now = utc_now_iso()

    def _apply_delta(base_key: str, id_key: str, add: Optional[Dict[str, Dict[str, Any]]], rem: Optional[Iterable[str]], cap: int) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
        out = {str(k): dict(v) for k, v in (existing.get(base_key) or {}).items() if k and isinstance(v, dict)}
        for k, entry in (add or {}).items():
            if k and isinstance(entry, dict):
                out[str(k)] = {id_key: str(k), "pruned_at": now, "reason": reason, **entry}
        for k in (rem or ()):
            out.pop(str(k), None)
        d_cnt, d_bytes = _trim_tombstones(out, cap)
        return dict(sorted(out.items())), d_cnt, d_bytes

    tasks, dt, dbt = _apply_delta("pruned_tasks", "task_id", add_tasks, remove_tasks, MAX_PRUNED_TASKS)
    blobs, db, dbb = _apply_delta("pruned_blobs", "sha256", add_blobs, remove_blobs, MAX_PRUNED_BLOBS)
    overflow_tasks, overflow_blobs = int(existing.get("overflow_tasks") or 0) + dt, int(existing.get("overflow_blobs") or 0) + db
    overflow_bytes = int(existing.get("overflow_bytes") or 0) + dbt + dbb

    doc: Dict[str, Any] = {
        "schema_version": 1, "status": PRUNED_STATUS, "retention_days": OBSERVABILITY_RETENTION_DAYS,
        "updated_at": now, "reason": reason, "pruned_tasks": tasks, "pruned_blobs": blobs,
    }
    for k, v in (("overflow_tasks", overflow_tasks), ("overflow_blobs", overflow_blobs), ("overflow_bytes", overflow_bytes)):
        if v:
            doc[k] = v

    atomic_write_json(path, doc, trailing_newline=True)
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE.pop(str(path), None)
    return doc


# ---- Authority signatures & fail-closed hold collection -------------------------------------


def _terminal_writes(root: pathlib.Path) -> int:
    if not store.store_path(root).is_file():
        return 0
    with store.open_store(root, write=False) as txn:
        return int(txn.meta(store.TERMINAL_WRITES) or 0)


def _projects_sig(root: pathlib.Path) -> Optional[List[int]]:
    return _stat_sig(root / "state" / "projects.json")

def _review_ledger_sig(root: pathlib.Path) -> Optional[List[Optional[List[int]]]]:
    ldir = root / "state" / "review_ledger"
    dir_sig = _stat_sig(ldir)
    return None if dir_sig is None else [dir_sig, _stat_sig(ldir / "index.jsonl")]

def _usage_sig(root: pathlib.Path) -> Optional[List[int]]:
    return _stat_sig(root / usage_store.STORE_REL)

def _queue_sig(root: pathlib.Path) -> Optional[List[int]]:
    return _stat_sig(root / "state" / "queue_snapshot.json")

def _queue_live(root: pathlib.Path) -> Callable[[str], Optional[bool]]:
    from ouroboros.task_status import task_has_live_queue_ownership, task_subtree_has_live_queue_rows
    return lambda tid: task_has_live_queue_ownership(root, tid) or task_subtree_has_live_queue_rows(root, tid)


def _walk_evidence_blob_shas(value: Any, out: Set[str]) -> None:
    if isinstance(value, dict):
        sha = value.get("sha256")
        if isinstance(sha, str) and _SHA256_RE.fullmatch(sha) and (
            value.get("encoding") == "gzip" or value.get("kind") in evidence_packs.BLOB_KINDS
        ):
            out.add(sha)
            return
        for v in value.values():
            _walk_evidence_blob_shas(v, out)
    elif isinstance(value, list):
        for item in value:
            _walk_evidence_blob_shas(item, out)


def _collect_active_projects(root: pathlib.Path) -> Tuple[Set[str], bool]:
    if not (root / "state" / "projects.json").exists():
        return set(), True
    try:
        rows = projects_registry.list_reserved_projects(root, strict=True)
    except Exception:
        log.warning("Observability retention fail-closed: projects registry unreadable", exc_info=True)
        return set(), False
    return {str(e.get("id") or "").strip() for e in rows if isinstance(e, dict) and e.get("id")
            and e.get("lifecycle") in (projects_registry.PROJECT_ACTIVE, projects_registry.PROJECT_DELETING)}, True


def _collect_open_review_ids(root: pathlib.Path) -> Tuple[Set[str], bool]:
    from ouroboros import review_ledger

    ldir = review_ledger.ledger_dir(root)
    if not ldir.exists():
        return set(), True
    if not ldir.is_dir() or ldir.is_symlink():
        return set(), False
    latest: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    try:
        for ipath in [review_ledger.index_path(root), *review_ledger.index_segments(root)]:
            if ipath.exists():
                for raw_line in ipath.read_text(encoding="utf-8").splitlines():
                    if not raw_line.strip():
                        continue
                    row = json.loads(raw_line)
                    if not isinstance(row, dict):
                        return set(), False
                    rid, rev = str(row.get("record_id") or ""), int(row.get("revision") or 0)
                    if rid and (rid not in latest or rev >= latest[rid][0]):
                        latest[rid] = (rev, row)
        with os.scandir(ldir) as entries:
            for e in entries:
                if e.name.startswith(".") or not e.name.endswith(".json") or e.is_symlink() or not e.is_file(follow_symlinks=False) or e.name[:-5] in latest:
                    continue
                rec = json.loads(pathlib.Path(e.path).read_text(encoding="utf-8"))
                if not isinstance(rec, dict):
                    return set(), False
                latest[e.name[:-5]] = (int(rec.get("revision") or 0), rec)
    except Exception:
        return set(), False
    return {
        str(rec.get(key) or "").strip() for _rev, rec in latest.values()
        if rec.get("state") == review_ledger.STATE_PENDING
        or (isinstance(rec.get("verdict"), dict) and rec["verdict"].get("reason") == "review_late_result_pending")
        for key in ("task_id", "root_task_id") if rec.get(key)
    }, True


def _collect_open_custody_ids(root: pathlib.Path) -> Tuple[Set[str], Set[str], bool]:
    if not store.store_path(root).is_file():
        return set(), set(), True
    try:
        from ouroboros import delegate_custody_current as dcc
        held_ids, held_blobs = set(), set()
        with store.open_store(root, write=False) as txn:
            for text in txn.custody_raw_rows(dcc.HELD_WHERE, (dcc.HELD,)).values():
                rec = json.loads(text)
                if not isinstance(rec, dict):
                    return set(), set(), False
                held_ids.update(str(rec.get(k) or "").strip() for k in ("task_id", "root_task_id", "parent_task_id", "staging_task_id") if rec.get(k))
                _walk_evidence_blob_shas(rec, held_blobs)
            for set_name in ("custody_open", "delegated_runs"):
                for key, facts in txn.set_rows(set_name).items():
                    if isinstance(facts, dict):
                        _walk_evidence_blob_shas(facts, held_blobs)
                        for src in (facts, facts.get("request")):
                            if isinstance(src, dict):
                                held_ids.update(str(src.get(k) or "").strip() for k in ("task_id", "root_task_id", "parent_task_id") if src.get(k))
                    elif key and ":" not in str(key):
                        held_ids.add(str(key))
        return held_ids, held_blobs, True
    except Exception:
        log.warning("Observability retention fail-closed: custody store unreadable", exc_info=True)
        return set(), set(), False


def _collect_unresolved_usage_holds(root: pathlib.Path) -> Tuple[Set[str], Set[str], bool]:
    """Query ``state/usage.sqlite`` via ``_OPEN_PREDICATE`` (``attempts_open``) + ``dirty_owners``."""
    if not (root / usage_store.STORE_REL).exists():
        return set(), set(), True
    held_tasks: Set[str] = set()
    held_blobs: Set[str] = set()
    try:
        with usage_store.read(root) as utxn:
            for rec in utxn.conn.execute(f"SELECT task_id, root_task_id, parent_task_id, extra FROM attempts WHERE {usage_store.OPEN_ATTEMPT_PREDICATE}").fetchall():
                held_tasks.update(str(rec[c] or "").strip() for c in ("task_id", "root_task_id", "parent_task_id") if rec[c])
                if rec["extra"]:
                    extra = json.loads(rec["extra"]) if isinstance(rec["extra"], str) else rec["extra"]
                    if isinstance(extra, dict) and isinstance(extra.get("physical_failure"), dict):
                        _walk_evidence_blob_shas(extra["physical_failure"], held_blobs)
            held_tasks.update(str(r["owner_id"]).strip() for r in utxn.conn.execute("SELECT owner_id FROM dirty_owners") if r["owner_id"])
        return held_tasks, held_blobs, True
    except Exception:
        return set(), set(), False


class _HoldState:
    __slots__ = ("ok", "reason", "active_projects", "open_review_ids", "open_custody_ids", "open_custody_blobs", "open_usage_ids", "open_usage_blobs")
    def __init__(self, *, ok: bool, reason: str = "", active_projects: Optional[Set[str]] = None, open_review_ids: Optional[Set[str]] = None,
                 open_custody_ids: Optional[Set[str]] = None, open_custody_blobs: Optional[Set[str]] = None,
                 open_usage_ids: Optional[Set[str]] = None, open_usage_blobs: Optional[Set[str]] = None) -> None:
        self.ok, self.reason = ok, reason
        self.active_projects, self.open_review_ids = active_projects or set(), open_review_ids or set()
        self.open_custody_ids, self.open_custody_blobs = open_custody_ids or set(), open_custody_blobs or set()
        self.open_usage_ids, self.open_usage_blobs = open_usage_ids or set(), open_usage_blobs or set()


def _load_holds(root: pathlib.Path, *, cached_review_ids: Optional[Set[str]] = None) -> _HoldState:
    if store.store_path(root).is_file():
        try:
            with store.open_store(root, write=False) as txn:
                txn.phase()
        except Exception:
            return _HoldState(ok=False, reason="current_store_unreadable")
    active_projects, proj_ok = _collect_active_projects(root)
    if not proj_ok:
        return _HoldState(ok=False, reason="projects_registry_unreadable")
    open_review_ids, rev_ok = (set(cached_review_ids), True) if cached_review_ids is not None else _collect_open_review_ids(root)
    if not rev_ok:
        return _HoldState(ok=False, reason="review_ledger_unreadable")
    open_custody_ids, open_custody_blobs, cust_ok = _collect_open_custody_ids(root)
    if not cust_ok:
        return _HoldState(ok=False, reason="custody_unreadable")
    open_usage_ids, open_usage_blobs, usage_ok = _collect_unresolved_usage_holds(root)
    if not usage_ok:
        return _HoldState(ok=False, reason="usage_store_unreadable")
    return _HoldState(ok=True, active_projects=active_projects, open_review_ids=open_review_ids, open_custody_ids=open_custody_ids, open_custody_blobs=open_custody_blobs, open_usage_ids=open_usage_ids, open_usage_blobs=open_usage_blobs)

def _load_task_rows(root: pathlib.Path, task_ids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    if not task_ids or not store.store_path(root).is_file():
        return {}, True
    try:
        return artifact_retention.load_retention_rows(root, task_ids), True
    except Exception:
        log.warning("Observability retention fail-closed: task store unreadable", exc_info=True)
        return {}, False


# ---- Task & file discovery ------------------------------------------------------------------


def _task_pack_files(root: pathlib.Path, task_id: str) -> List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]]:
    pdir = evidence_packs.packs_dir(root)
    if not pdir.is_dir():
        return []
    out, gen = [], 1
    while True:
        spath, ppath = pdir / evidence_packs.sidecar_name(task_id, gen), pdir / evidence_packs.pack_name(task_id, gen)
        if not spath.exists() and not ppath.exists():
            return out
        parsed = None
        if spath.exists():
            with contextlib.suppress(evidence_packs.SidecarCorrupt):
                parsed = evidence_packs.read_sidecar(spath)
            if parsed is not None and str(parsed.get("task_id") or "") != str(task_id):
                gen += 1
                continue
        out.append((ppath, spath, gen, parsed))
        gen += 1


def _all_packed_task_ids(root: pathlib.Path, *, stop: Optional[Callable[[], bool]] = None) -> Dict[str, List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]]]:
    pdir = evidence_packs.packs_dir(root)
    by_task: Dict[str, List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]]] = {}
    if not pdir.is_dir() or pdir.is_symlink():
        return by_task
    try:
        entries = sorted(os.scandir(pdir), key=lambda e: e.name)
    except OSError:
        return by_task
    for entry in entries:
        if stop and stop():
            break
        if entry.name.startswith(".") or not entry.name.endswith(".members.json") or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        spath = pathlib.Path(entry.path)
        stem = entry.name[:-len(".members.json")]
        try:
            sidecar = evidence_packs.read_sidecar(spath)
            tid, gen = str(sidecar["task_id"]), int(sidecar["generation"])
        except evidence_packs.SidecarCorrupt:
            sidecar = None
            head, _, tail = stem.rpartition(".")
            tid, gen = (head, int(tail)) if head and tail.isdigit() and int(tail) >= 2 else (stem, 1)
        ppath = pdir / evidence_packs.pack_name(tid, gen)
        by_task.setdefault(tid, []).append((ppath, spath, gen, sidecar))
    return by_task


def _loose_call_files(root: pathlib.Path, task_id: str) -> List[pathlib.Path]:
    cdir = root / "observability" / "calls" / str(task_id)
    if not cdir.is_dir() or cdir.is_symlink():
        return []
    out: List[pathlib.Path] = []
    with contextlib.suppress(OSError):
        for entry in sorted(os.scandir(cdir), key=lambda e: e.name):
            name = entry.name
            if not name.startswith(".") and name.endswith(".json") and not name.endswith(".model_send_violation.json") and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                out.append(pathlib.Path(entry.path))
    return out


def _all_call_task_ids(root: pathlib.Path, *, stop: Optional[Callable[[], bool]] = None) -> Set[str]:
    croot = root / "observability" / "calls"
    if not croot.is_dir() or croot.is_symlink():
        return set()
    ids: Set[str] = set()
    with contextlib.suppress(OSError), os.scandir(croot) as it:
        for entry in it:
            if stop and stop():
                break
            if not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                ids.add(entry.name)
    return ids


def _task_has_observability(root: pathlib.Path, task_id: str) -> bool:
    pdir = evidence_packs.packs_dir(root)
    if (pdir / evidence_packs.sidecar_name(task_id, 1)).exists() or (pdir / evidence_packs.pack_name(task_id, 1)).exists():
        return True
    cdir = root / "observability" / "calls" / str(task_id)
    return cdir.is_dir() and not cdir.is_symlink()


# ---- Hold & age evaluation ------------------------------------------------------------------


def _task_settled_epoch(row: Optional[Dict[str, Any]]) -> Optional[float]:
    return next((p for k in ("drive_retention_at", "updated_at", "ts") if (p := artifact_retention.parse_epoch_seconds(row.get(k))) is not None), None) if isinstance(row, dict) else None

def _file_mtime(path: pathlib.Path) -> Optional[float]:
    try:
        return path.lstat().st_mtime
    except OSError:
        return None
def _file_size(path: pathlib.Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


def _evaluate_task_hold(task_id: str, t_row: Optional[Dict[str, Any]], rows_by_id: Dict[str, Dict[str, Any]],
                        holds: _HoldState, live: Callable[[str], Optional[bool]]) -> Tuple[str, str]:
    from ouroboros.task_status import SETTLED_STATUSES

    root_tid, root_row = "", None
    if t_row is not None:
        if str(t_row.get("status") or "") not in SETTLED_STATUSES: return "non_terminal", task_id
        if live(task_id) is not False: return "live", task_id
        root_tid = str(t_row.get("root_task_id") or "").strip()
        if root_tid and root_tid != task_id:
            root_row = rows_by_id.get(root_tid)
            if root_row is None: return "open_custody", root_tid
            if str(root_row.get("status") or "") not in SETTLED_STATUSES: return "root_non_terminal", root_tid
            if live(root_tid) is not False: return "live", root_tid
        pid = str(t_row.get("project_id") or (root_row or {}).get("project_id") or "").strip()
        if pid and pid in holds.active_projects: return "active_project", pid
        for check_row in (t_row, root_row):
            if not isinstance(check_row, dict): continue
            hkind, hdetail = artifact_retention.pending_row_hold(check_row)
            if hkind: return hkind, hdetail
    elif live(task_id) is not False: return "live", task_id
    lineage = {task_id} | ({root_tid} if root_tid else set())
    if lineage & holds.open_review_ids: return "open_review", "review_ledger"
    if lineage & holds.open_custody_ids: return "open_custody", "delegate_custody"
    if lineage & holds.open_usage_ids: return "unresolved_usage", "usage_store"
    return "", ""


def _task_age_targets(t_row: Optional[Dict[str, Any]], pack_entries: List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]],
                      call_files: List[pathlib.Path], cutoff_ts: float) -> Tuple[List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]], List[pathlib.Path], Optional[float]]:
    earliest_young: Optional[float] = None
    prunable_packs: List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]] = []
    task_epoch = _task_settled_epoch(t_row)
    if pack_entries:
        if task_epoch is not None:
            if task_epoch < cutoff_ts: prunable_packs = list(pack_entries)
            else: earliest_young = task_epoch
        else:
            mtimes = [mt for pp, sp, _, _ in pack_entries for mt in (_file_mtime(pp), _file_mtime(sp)) if mt is not None]
            if mtimes and max(mtimes) < cutoff_ts: prunable_packs = list(pack_entries)
            elif mtimes: earliest_young = max(mtimes)
    prunable_calls = []
    for cpath in call_files:
        mt = _file_mtime(cpath)
        if mt is None: continue
        eff_mt = mt if task_epoch is None else max(mt, task_epoch)
        if eff_mt < cutoff_ts: prunable_calls.append(cpath)
        else: earliest_young = eff_mt if earliest_young is None else min(earliest_young, eff_mt)
    return prunable_packs, prunable_calls, earliest_young


# ---- Pruning execution ----------------------------------------------------------------------


def _prune_task_observability(root: pathlib.Path, task_id: str, *, live: Callable[[str], Optional[bool]], holds: _HoldState,
                              retention_days: int = OBSERVABILITY_RETENTION_DAYS, reason: str = "observability_14d_retention",
                              now: Optional[float] = None, stop: Optional[Callable[[], bool]] = None,
                              prewritten_tombstone: bool = False) -> Dict[str, Any]:
    from ouroboros.task_custody import task_custody_lock

    kept = lambda rsn, **kw: {"task_id": task_id, "status": "kept", "reason": rsn, "removed": 0, "removed_bytes": 0, **kw}
    if not holds.ok:
        return kept("unreadable_record")
    cutoff_ts = retention.age_cutoff(retention_days, now=now)
    with task_custody_lock(root, str(task_id), timeout_sec=0.0) as acquired:
        if not acquired:
            return kept("busy")
        if stop and stop():
            return kept("other")
        rows, rows_ok = _load_task_rows(root, [task_id])
        if not rows_ok:
            return kept("unreadable_record")
        t_row = rows.get(task_id)
        hold_kind, _detail = _evaluate_task_hold(task_id, t_row, rows, holds, live)
        if hold_kind:
            return kept("in_use", hold_kind=hold_kind)

        prunable_packs, prunable_calls, _young = _task_age_targets(t_row, _task_pack_files(root, task_id), _loose_call_files(root, task_id), cutoff_ts)
        if not prunable_packs and not prunable_calls:
            return {"task_id": task_id, "status": "empty", "reason": "", "removed": 0, "removed_bytes": 0}

        gens = [gen for _, _, gen, _ in prunable_packs]
        files_to_evict = [fp for pp, sp, _, _ in prunable_packs for fp in (sp, pp) if fp.exists()]
        pack_bytes = sum(_file_size(fp) for fp in files_to_evict)
        call_bytes = sum(_file_size(cp) for cp in prunable_calls)
        blob_tombstones = _collect_task_blob_map(root, task_id, prunable_packs, prunable_calls)
        task_tombstone = {"generations": sorted(set(gens)), "packs_removed": len(prunable_packs),
                          "calls_removed": len(prunable_calls), "bytes": pack_bytes + call_bytes}
        if not prewritten_tombstone:
            try:
                _merge_pruned_manifest(root, add_tasks={task_id: task_tombstone}, add_blobs=blob_tombstones, reason=reason)
            except OSError:
                return kept("other")
        removed_bytes, removed_packs, removed_calls, failed_any = 0, 0, 0, False
        if prunable_packs:
            if _evict_task_packs(root, task_id, files_to_evict, member_tombstones=blob_tombstones, reason=reason):
                failed_any = True
            else:
                removed_packs, removed_bytes = len(prunable_packs), removed_bytes + pack_bytes
        if prunable_calls:
            try:
                with store.name_lock(root, evidence_packs.PUBLICATION_LOCK_NAME, timeout_sec=5.0):
                    for cpath in prunable_calls:
                        mt = _file_mtime(cpath)
                        if mt is None: continue
                        if mt >= cutoff_ts: failed_any = True; continue
                        sz = _file_size(cpath)
                        try:
                            cpath.unlink()
                            removed_calls, removed_bytes = removed_calls + 1, removed_bytes + sz
                        except FileNotFoundError: pass
                        except OSError: failed_any = True
                    with contextlib.suppress(OSError):
                        (root / "observability" / "calls" / str(task_id)).rmdir()
            except store.StoreBusy:
                failed_any = True
        if failed_any and removed_packs == 0 and removed_calls == 0:
            if not prewritten_tombstone:
                with contextlib.suppress(OSError):
                    _merge_pruned_manifest(root, remove_tasks=[task_id], remove_blobs=blob_tombstones.keys(), reason=reason)
            return kept("busy")
        return {"task_id": task_id, "status": "partial" if failed_any else "pruned", "reason": "other" if failed_any else "",
                "removed": removed_packs + removed_calls, "packs_removed": removed_packs,
                "calls_removed": removed_calls, "removed_bytes": removed_bytes,
                "blob_shas": [sha for sha, info in blob_tombstones.items() if info.get("kind") in evidence_packs.BLOB_KINDS]}


def _collect_task_blob_map(root: pathlib.Path, task_id: str, p_packs: List[Tuple[pathlib.Path, pathlib.Path, int, Optional[Dict[str, Any]]]], p_calls: List[pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    bmap: Dict[str, Dict[str, Any]] = {}
    for _, _, _, sc in p_packs:
        for m in (sc.get("members") or ()) if isinstance(sc, dict) else ():
            sha, kind = str(m.get("sha256") or ""), str(m.get("kind") or "")
            if _SHA256_RE.fullmatch(sha) and kind in (evidence_packs.BLOB_KINDS | {evidence_packs.CALL_MANIFEST}):
                bmap[sha] = {"kind": kind, "task_id": task_id, "bytes": int(m.get("size") or 0)}
    for cp in p_calls:
        for kind, sha, sz in _call_file_blob_refs(root, cp):
            bmap.setdefault(sha, {"kind": kind, "task_id": task_id, "bytes": int(sz)})
    return bmap


def _call_file_blob_refs(root: pathlib.Path, cpath: pathlib.Path, *, strict: bool = False) -> List[Tuple[str, str, int]]:
    try:
        data = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if strict: raise
        return []
    if not isinstance(data, dict):
        if strict: raise ValueError("non-dict call manifest")
        return []
    found: List[Tuple[str, str, int]] = []
    seen: Set[Tuple[str, str]] = set()
    evidence_packs.walk_blob_refs(data, found, seen)
    if data.get("call_type") == "physical_stream":
        outer_ref = data.get("redacted_projection_ref") or data.get("full_payload_ref")
        if isinstance(outer_ref, dict):
            with contextlib.suppress(OSError, ValueError, evidence_packs.EvidenceCorrupt):
                outer = json.loads(evidence_packs.blob_bytes(root, outer_ref, expected_kind="json", max_bytes=1 << 20).decode("utf-8"))
                evidence_packs.walk_blob_refs(outer, found, seen)
    return found


def _collect_loose_call_referenced_shas(root: pathlib.Path, *, stop: Optional[Callable[[], bool]] = None) -> Tuple[Set[str], bool]:
    croot = root / "observability" / "calls"
    refs: Set[str] = set()
    if not croot.is_dir() or croot.is_symlink():
        return refs, True
    try:
        for task_entry in os.scandir(croot):
            if stop and stop(): return refs, False
            if task_entry.name.startswith(".") or task_entry.is_symlink() or not task_entry.is_dir(follow_symlinks=False): continue
            for call_entry in os.scandir(task_entry.path):
                if stop and stop(): return refs, False
                name = call_entry.name
                if name.startswith(".") or not name.endswith(".json") or name.endswith(".model_send_violation.json") or call_entry.is_symlink() or not call_entry.is_file(follow_symlinks=False): continue
                found = _call_file_blob_refs(root, pathlib.Path(call_entry.path), strict=True)
                refs.update(sha for _, sha, _ in found)
    except Exception:
        return refs, False
    return refs, True


def _collect_packed_referenced_shas(root: pathlib.Path, *, stop: Optional[Callable[[], bool]] = None) -> Tuple[Set[str], bool]:
    pdir = evidence_packs.packs_dir(root)
    if not pdir.is_dir() or pdir.is_symlink():
        return set(), True
    try:
        with evidence_packs.open_index(root) as conn:
            shas = {str(r[0]) for r in conn.execute("SELECT DISTINCT sha256 FROM members") if r[0]}
            err_row = conn.execute("SELECT value FROM meta WHERE key='sidecar_errors'").fetchone()
            return shas, not (err_row and json.loads(err_row[0]))
    except Exception:
        try:
            shas = {str(m.get("sha256") or "") for _sp, sc in evidence_packs.list_sidecars(root) for m in sc.get("members") or () if m.get("sha256") and not (stop and stop())}
            return shas, not (stop and stop())
        except Exception:
            return set(), False


def _scan_aged_unreferenced_blobs(root: pathlib.Path, *, holds: _HoldState, cutoff_ts: float, only_shas: Optional[Set[str]] = None, stop: Optional[Callable[[], bool]] = None) -> Tuple[List[Dict[str, Any]], Optional[float], bool]:
    wanted = set(only_shas) if only_shas else set()
    if not wanted:
        return [], None, True
    bdir = root / "observability" / "blobs"
    if not bdir.is_dir() or bdir.is_symlink(): return [], None, True
    aged, earliest_young = [], None
    try:
        with os.scandir(bdir) as it:
            for entry in it:
                if stop and stop(): return [], earliest_young, False
                if entry.name.startswith(".") or not entry.name.endswith(".gz") or entry.is_symlink() or not entry.is_file(follow_symlinks=False): continue
                parts = entry.name[:-3].rsplit(".", 1)
                if len(parts) != 2 or not _SHA256_RE.fullmatch(parts[0]) or parts[1] not in evidence_packs.BLOB_KINDS: continue
                if parts[0] not in wanted: continue
                try: st = entry.stat(follow_symlinks=False)
                except OSError: continue
                if st.st_mtime < cutoff_ts: aged.append((pathlib.Path(entry.path), parts[0], parts[1], int(st.st_size)))
                else: earliest_young = st.st_mtime if earliest_young is None else min(earliest_young, st.st_mtime)
    except OSError:
        return [], earliest_young, False
    if not aged: return [], earliest_young, True
    referenced, refs_ok = _collect_loose_call_referenced_shas(root, stop=stop)
    if not refs_ok: return [], earliest_young, False
    packed_refs, packs_ok = _collect_packed_referenced_shas(root, stop=stop)
    if not packs_ok: return [], earliest_young, False
    referenced |= packed_refs | holds.open_usage_blobs | holds.open_custody_blobs
    return [{"path": p, "sha256": s, "kind": k, "bytes": sz} for p, s, k, sz in aged if s not in referenced], earliest_young, True


def _prune_aged_unreferenced_blobs(root: pathlib.Path, *, holds: _HoldState, cutoff_ts: float, reason: str = "observability_14d_retention", only_shas: Optional[Set[str]] = None, stop: Optional[Callable[[], bool]] = None) -> Tuple[int, int, Optional[float], bool]:
    wanted = set(only_shas) if only_shas else set()
    if not holds.ok:
        return 0, 0, None, False
    if not wanted:
        return 0, 0, None, True
    pub_path = evidence_packs.packs_dir(root) / evidence_packs.PUBLICATIONS_NAME
    journal = None
    try:
        with store.name_lock(root, evidence_packs.PUBLICATION_LOCK_NAME, timeout_sec=5.0):
            pub_path.parent.mkdir(parents=True, exist_ok=True)
            journal = pub_path.open("w+b")
        candidates, earliest_young, ok = _scan_aged_unreferenced_blobs(root, holds=holds, cutoff_ts=cutoff_ts, only_shas=wanted, stop=stop)
        if not ok:
            return 0, 0, earliest_young, False
        if not candidates:
            return 0, 0, earliest_young, True
        with store.name_lock(root, evidence_packs.PUBLICATION_LOCK_NAME, timeout_sec=5.0):
            journal.seek(0)
            pub_refs = {ln.decode("ascii", errors="ignore").strip() for ln in journal if _SHA256_RE.fullmatch(ln.decode("ascii", errors="ignore").strip())}
            filtered = [c for c in candidates if c["sha256"] not in pub_refs]
            if not filtered:
                return 0, 0, earliest_young, True
            prior_doc = _load_pruned_manifest(root) or {}
            prior_blobs = prior_doc.get("pruned_blobs") if isinstance(prior_doc.get("pruned_blobs"), dict) else {}
            _merge_pruned_manifest(root, add_blobs={
                c["sha256"]: {"kind": c["kind"], "task_id": str((prior_blobs.get(c["sha256"]) or {}).get("task_id") or ""), "bytes": c["bytes"]}
                for c in filtered
            }, reason=reason)
            removed, removed_bytes, failed_shas = 0, 0, []
            for c in filtered:
                mt = _file_mtime(c["path"])
                if mt is None: continue
                if mt >= cutoff_ts: failed_shas.append(c["sha256"]); continue
                try:
                    c["path"].unlink()
                    removed, removed_bytes = removed + 1, removed_bytes + int(c["bytes"])
                except FileNotFoundError: pass
                except OSError: failed_shas.append(c["sha256"])
            if failed_shas:
                with contextlib.suppress(OSError):
                    _merge_pruned_manifest(root, remove_blobs=failed_shas, reason=reason)
            return removed, removed_bytes, earliest_young, True
    except (OSError, store.StoreBusy):
        return 0, 0, None, False
    finally:
        if journal is not None:
            with contextlib.suppress(OSError, store.StoreBusy):
                with store.name_lock(root, evidence_packs.PUBLICATION_LOCK_NAME, timeout_sec=5.0):
                    journal.close()
                    with contextlib.suppress(FileNotFoundError):
                        pub_path.unlink()
def list_aged_observability_candidates(root: Any, *, live: Callable[[str], Optional[bool]], retention_days: int = OBSERVABILITY_RETENTION_DAYS, stop: Optional[Callable[[], bool]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    root_path = pathlib.Path(root)
    if not (root_path / "observability").is_dir() or (root_path / "observability").is_symlink(): return [], 0, 0
    holds = _load_holds(root_path)
    packed_by_task = _all_packed_task_ids(root_path, stop=stop)
    all_tids = sorted(set(packed_by_task) | _all_call_task_ids(root_path, stop=stop))
    if not holds.ok: return [], 0, max(1, len(all_tids))
    rows_by_id, rows_ok = _load_task_rows(root_path, all_tids)
    if not rows_ok: return [], 0, max(1, len(all_tids))
    cutoff_ts = retention.age_cutoff(retention_days)
    candidates, in_use, unreadable = [], 0, 0
    attr_shas: Set[str] = set()
    doc = _load_pruned_manifest(root_path)
    if isinstance(doc, dict) and isinstance(doc.get("pruned_blobs"), dict):
        attr_shas.update(sha for sha, info in doc["pruned_blobs"].items() if isinstance(info, dict) and info.get("kind") in evidence_packs.BLOB_KINDS)
    for tid in all_tids:
        if stop and stop(): break
        t_row = rows_by_id.get(tid)
        if _evaluate_task_hold(tid, t_row, rows_by_id, holds, live)[0]: in_use += 1; continue
        p_packs, p_calls, _ = _task_age_targets(t_row, packed_by_task.get(tid, []), _loose_call_files(root_path, tid), cutoff_ts)
        if not p_packs and not p_calls: continue
        attr_shas.update(sha for sha, info in _collect_task_blob_map(root_path, tid, p_packs, p_calls).items() if info.get("kind") in evidence_packs.BLOB_KINDS)
        total_bytes = sum(_file_size(fp) for pp, sp, _, _ in p_packs for fp in (pp, sp) if fp.exists()) + sum(_file_size(cp) for cp in p_calls)
        candidates.append({"kind": "task", "task_id": tid, "key": f"obs_task:{tid}", "path": p_packs[0][0] if p_packs else p_calls[0].parent, "bytes": total_bytes, "unreadable": 0, "packs": len(p_packs), "calls": len(p_calls)})
    blobs, _, blobs_ok = _scan_aged_unreferenced_blobs(root_path, holds=holds, cutoff_ts=cutoff_ts, only_shas=attr_shas, stop=stop) if attr_shas else ([], None, True)
    if not blobs_ok: unreadable += 1
    elif blobs: candidates.append({"kind": "blobs", "task_id": "", "key": "obs_blobs:unreferenced", "name": f"Unreferenced aged blobs ({len(blobs)} files)", "path": root_path / "observability" / "blobs", "bytes": sum(int(b["bytes"]) for b in blobs), "unreadable": 0, "shas": [b["sha256"] for b in blobs]})
    return candidates, in_use, unreadable

def prune_observability_candidate(root: Any, item: Dict[str, Any], *, live: Callable[[str], Optional[bool]], retention_days: int = OBSERVABILITY_RETENTION_DAYS, reason: str = "owner_storage_free", stop: Optional[Callable[[], bool]] = None) -> str:
    root_path = pathlib.Path(root).resolve(strict=False)
    try:
        with store.name_lock(root_path, "packs_sweep.lock", timeout_sec=5.0):
            holds = _load_holds(root_path)
            if not holds.ok:
                return "unreadable_record"
            kind = str(item.get("kind") or "")
            if kind == "task":
                return str(_prune_task_observability(root_path, str(item.get("task_id") or ""), live=live, holds=holds, retention_days=retention_days, reason=reason, stop=stop).get("reason") or "")
            if kind == "blobs":
                cutoff_ts = retention.age_cutoff(retention_days)
                shas = {str(s) for s in (item.get("shas") or ()) if s}
                _, _, _, ok = _prune_aged_unreferenced_blobs(root_path, holds=holds, cutoff_ts=cutoff_ts, reason=reason, only_shas=shas, stop=stop)
                return "" if ok else "other"
            return "other"
    except store.StoreBusy:
        return "busy"


# ---- Maintenance child job (B6 rest & O(1) unrelated-activity compliant) --------------------


def _has_observability_dirs(root: pathlib.Path) -> bool:
    return (root / "observability" / "packs").is_dir() or (root / "observability" / "calls").is_dir() or (root / "observability" / "blobs").is_dir()


def _custody_sig(root: pathlib.Path) -> Optional[List[str]]:
    if not store.store_path(root).is_file():
        return None
    try:
        from ouroboros import delegate_custody_current as dcc

        with store.open_store(root, write=False) as txn:
            return [*sorted(txn.custody_raw_rows(dcc.HELD_WHERE, (dcc.HELD,)).keys()), "|",
                    *sorted(txn.set_rows("custody_open").keys()), "|", *sorted(txn.set_rows("delegated_runs").keys())]
    except Exception:
        return ["unreadable"]


def due(root: Any, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Cheap O(1) due check (no ``open`` or ``scandir`` inside ``observability/`` when resting)."""
    root_path, state = pathlib.Path(root), row.get("state")
    if state == "pending": return True
    if state != "done": return _has_observability_dirs(root_path)
    now = time.time()
    if (row.get("next_due") is not None and now >= float(row["next_due"])) or (row.get("next_due_blobs") is not None and now >= float(row["next_due_blobs"])): return True
    if row.get("authority_unreadable"):
        return any(fn(root_path) != row.get(k) for fn, k in ((_projects_sig, "projects_sig"), (_review_ledger_sig, "review_ledger_sig"), (_usage_sig, "usage_sig"), (_custody_sig, "custody_sig")))
    if _terminal_writes(root_path) != row.get("counter"): return True
    for wkey, fn, skey in (("waiting_project", _projects_sig, "projects_sig"), ("waiting_review", _review_ledger_sig, "review_ledger_sig"),
                           ("waiting_usage", _usage_sig, "usage_sig"), ("waiting_queue", _queue_sig, "queue_sig"), ("waiting_custody", _custody_sig, "custody_sig")):
        if row.get(wkey) and fn(root_path) != row.get(skey): return True
    return bool((row.get("waiting_retry") or []) and child)


def _record_hold(tid: str, hold_kind: str, hold_detail: str, buckets: Dict[str, Any]) -> None:
    if hold_kind in ("active_project", "root_non_terminal"):
        lst = buckets["project" if hold_kind == "active_project" else "root"].setdefault(hold_detail, [])
        if tid not in lst:
            lst.append(tid)
    elif hold_kind in ("open_review", "unresolved_usage", "live", "open_custody"):
        buckets[{"open_review": "review", "unresolved_usage": "usage", "live": "queue", "open_custody": "custody"}[hold_kind]].add(tid)


def _process_candidates(root_path: pathlib.Path, obs_candidates: List[str], packed_map: Optional[Dict[str, list]], holds: _HoldState, buckets: Dict[str, Any], young: Dict[str, float], *, now: float, cutoff_ts: float, stop: Callable[[], bool]) -> Dict[str, Any]:
    live = _queue_live(root_path)
    rows_by_id, rows_ok = _load_task_rows(root_path, obs_candidates)
    if not rows_ok:
        return {"authority_unreadable": True, "authority_reason": "current_store_unreadable", "stopped": False, "pruned_tasks": 0, "pruned_packs": 0, "pruned_calls": 0, "pruned_bytes": 0, "sweep_blobs": False}
    stats: Dict[str, Any] = {"authority_unreadable": False, "authority_reason": "", "stopped": False, "pruned_tasks": 0, "pruned_packs": 0, "pruned_calls": 0, "pruned_bytes": 0, "pruned_blob_shas": set()}
    batch_tasks, batch_blobs, planned = {}, {}, []
    for idx, tid in enumerate(obs_candidates):
        if stop():
            stats["stopped"] = True; buckets["retry"].update(obs_candidates[idx:]); break
        young.pop(tid, None)
        for k in ("review", "usage", "queue", "custody", "retry"):
            buckets[k].discard(tid)
        t_row = rows_by_id.get(tid)
        hold_kind, hold_detail = _evaluate_task_hold(tid, t_row, rows_by_id, holds, live)
        if hold_kind:
            _record_hold(tid, hold_kind, hold_detail, buckets); continue
        pack_entries = packed_map.get(tid, []) if packed_map is not None else _task_pack_files(root_path, tid)
        p_packs, p_calls, earliest_young = _task_age_targets(t_row, pack_entries, _loose_call_files(root_path, tid), cutoff_ts)
        if earliest_young is not None:
            young[tid] = earliest_young + OBSERVABILITY_RETENTION_DAYS * 86400
        if not p_packs and not p_calls:
            continue
        b_map = _collect_task_blob_map(root_path, tid, p_packs, p_calls)
        total_b = sum(_file_size(fp) for pp, sp, _, _ in p_packs for fp in (sp, pp) if fp.exists()) + sum(_file_size(cp) for cp in p_calls)
        batch_tasks[tid] = {"generations": sorted({g for _, _, g, _ in p_packs}), "packs_removed": len(p_packs), "calls_removed": len(p_calls), "bytes": total_b}
        batch_blobs.update(b_map)
        planned.append((tid, b_map))
    if planned and not stats["stopped"]:
        try:
            _merge_pruned_manifest(root_path, add_tasks=batch_tasks, add_blobs=batch_blobs, reason="observability_14d_retention")
        except OSError:
            buckets["retry"].update(tid for tid, _ in planned); return stats
    roll_tasks, roll_blobs = [], []
    for idx, (tid, b_map) in enumerate(planned):
        if stop():
            stats["stopped"] = True; buckets["retry"].update(t for t, _ in planned[idx:])
            roll_tasks.extend(t for t, _ in planned[idx:])
            for _, bm in planned[idx:]:
                roll_blobs.extend(bm)
            break
        out = _prune_task_observability(root_path, tid, live=live, holds=holds, retention_days=OBSERVABILITY_RETENTION_DAYS, reason="observability_14d_retention", now=now, stop=stop, prewritten_tombstone=True)
        ostatus = str(out.get("status") or "")
        if ostatus in ("pruned", "partial") and out.get("removed"):
            stats["pruned_tasks"] += 1; stats["pruned_packs"] += int(out.get("packs_removed") or 0)
            stats["pruned_calls"] += int(out.get("calls_removed") or 0); stats["pruned_bytes"] += int(out.get("removed_bytes") or 0)
            stats["pruned_blob_shas"].update(out.get("blob_shas") or ())
        elif ostatus == "kept":
            roll_tasks.append(tid); roll_blobs.extend(b_map)
        if ostatus in ("kept", "partial") and out.get("reason") in ("busy", "other"):
            buckets["retry"].add(tid)
    if roll_tasks:
        with contextlib.suppress(OSError):
            _merge_pruned_manifest(root_path, remove_tasks=roll_tasks, remove_blobs=roll_blobs, reason="observability_14d_retention")
    return stats


def run(root: Any, should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Execute one incremental or backlog pass of 14-day rolling observability retention."""
    from ouroboros.task_store import terminal_writes_since
    stop, root_path = should_stop or (lambda: False), pathlib.Path(root).resolve(strict=False)
    row = maintenance_jobs.job(root_path, JOB) or {}
    now, counter = time.time(), _terminal_writes(root_path)
    cutoff_ts = retention.age_cutoff(OBSERVABILITY_RETENTION_DAYS, now=now)
    proj_sig, rev_sig, usg_sig, q_sig = _projects_sig(root_path), _review_ledger_sig(root_path), _usage_sig(root_path), _queue_sig(root_path)
    backlog = row.get("counter") is None or bool(row.get("authority_unreadable"))
    young = {str(k): float(v) for k, v in (row.get("young") or {}).items() if isinstance(v, (int, float))}
    buckets: Dict[str, Any] = {
        "project": {str(k): [str(t) for t in v if t] for k, v in (row.get("waiting_project") or {}).items() if isinstance(v, list)},
        "root": {str(k): [str(t) for t in v if t] for k, v in (row.get("waiting_root") or {}).items() if isinstance(v, list)},
        **{k: {str(x) for x in (row.get(f"waiting_{k}") or []) if x} for k in ("review", "usage", "queue", "custody", "retry")},
    }
    next_due_blobs = float(row["next_due_blobs"]) if row.get("next_due_blobs") is not None else None
    sweep_shas: Set[str] = set()
    if backlog and isinstance(doc := _load_pruned_manifest(root_path), dict) and isinstance(doc.get("pruned_blobs"), dict):
        sweep_shas.update(sha for sha, info in doc["pruned_blobs"].items() if isinstance(info, dict) and info.get("kind") in evidence_packs.BLOB_KINDS)
    elif next_due_blobs is not None and now >= next_due_blobs and isinstance(doc := _load_pruned_manifest(root_path), dict) and isinstance(doc.get("pruned_blobs"), dict):
        sweep_shas.update(sha for sha, info in doc["pruned_blobs"].items() if isinstance(info, dict) and info.get("kind") in evidence_packs.BLOB_KINDS)
    packed_map = None
    if backlog:
        packed_map = _all_packed_task_ids(root_path, stop=stop)
        candidate_ids: Set[str] = set(packed_map) | _all_call_task_ids(root_path, stop=stop) | set(young)
        for k in ("review", "usage", "queue", "custody", "retry"):
            candidate_ids |= buckets[k]
        candidate_ids.update(t for m in (buckets["project"], buckets["root"]) for tids in m.values() for t in tids)
    else:
        written = set(terminal_writes_since(root_path, int(row["counter"]))) if store.store_path(root_path).is_file() else set()
        candidate_ids = set(written) | {tid for tid, elig in young.items() if elig <= now} | set(buckets["retry"])
        for root_tid in list(buckets["root"]):
            if root_tid in written:
                candidate_ids.update(buckets["root"].pop(root_tid, ()))
        if buckets["project"] and proj_sig != row.get("projects_sig"):
            candidate_ids.update(t for tids in buckets["project"].values() for t in tids)
            buckets["project"].clear()
        for k, cond in (("review", rev_sig != row.get("review_ledger_sig") or bool(buckets["review"] & written)),
                        ("usage", usg_sig != row.get("usage_sig")), ("queue", q_sig != row.get("queue_sig")),
                        ("custody", bool(buckets["custody"]) and (_custody_sig(root_path) != row.get("custody_sig") or bool(buckets["custody"] & written)))):
            if buckets[k] and cond:
                candidate_ids |= buckets[k]
    obs_candidates = sorted(tid for tid in candidate_ids if (packed_map is not None and tid in packed_map) or _task_has_observability(root_path, tid))
    for gone in candidate_ids - set(obs_candidates):
        young.pop(gone, None)
        for k in ("review", "usage", "queue", "custody", "retry"):
            buckets[k].discard(gone)
    pruned_tasks = pruned_packs = pruned_calls = pruned_blobs = pruned_bytes = 0
    stopped = authority_unreadable = False
    authority_reason = ""
    cached_rev_out = list(row["review_pending_ids"]) if isinstance(row.get("review_pending_ids"), list) and rev_sig == row.get("review_ledger_sig") else None
    if obs_candidates or sweep_shas:
        with store.name_lock(root_path, "packs_sweep.lock", timeout_sec=evidence_packs.INDEX_WAIT_SEC):
            holds = _load_holds(root_path, cached_review_ids=set(cached_rev_out) if (cached_rev_out is not None and not backlog) else None)
            if not holds.ok:
                authority_unreadable, authority_reason = True, holds.reason
            else:
                cached_rev_out = sorted(holds.open_review_ids)
                if obs_candidates:
                    stats = _process_candidates(root_path, obs_candidates, packed_map, holds, buckets, young, now=now, cutoff_ts=cutoff_ts, stop=stop)
                    authority_unreadable, authority_reason, stopped = stats["authority_unreadable"], stats["authority_reason"], stats["stopped"]
                    pruned_tasks, pruned_packs, pruned_calls, pruned_bytes = stats["pruned_tasks"], stats["pruned_packs"], stats["pruned_calls"], stats["pruned_bytes"]
                    sweep_shas.update(stats["pruned_blob_shas"])
                if sweep_shas and not stopped and not authority_unreadable:
                    b_rem, b_bytes, earliest_mt, b_ok = _prune_aged_unreferenced_blobs(root_path, holds=holds, cutoff_ts=cutoff_ts, only_shas=sweep_shas, stop=stop)
                    if b_ok:
                        pruned_blobs, pruned_bytes = pruned_blobs + b_rem, pruned_bytes + b_bytes
                        next_due_blobs = (earliest_mt + OBSERVABILITY_RETENTION_DAYS * 86400) if earliest_mt is not None else None
                elif not sweep_shas and not stopped and not authority_unreadable:
                    next_due_blobs = None
    if authority_unreadable and authority_reason == "current_store_unreadable":
        return {"state": "failed", "items": 0}
    state = "pending" if stopped else "done"
    fields: Dict[str, Any] = {
        "state": state, "young": young, "next_due": min(young.values()) if young else None, "next_due_blobs": next_due_blobs,
        "waiting_project": {k: sorted(set(v)) for k, v in sorted(buckets["project"].items()) if v},
        "waiting_root": {k: sorted(set(v)) for k, v in sorted(buckets["root"].items()) if v},
        **{f"waiting_{k}": sorted(buckets[k]) for k in ("review", "usage", "queue", "custody", "retry")},
        "authority_unreadable": authority_unreadable, "authority_reason": authority_reason,
        "last": {"at": now, "pruned_tasks": pruned_tasks, "pruned_packs": pruned_packs, "pruned_calls": pruned_calls, "pruned_blobs": pruned_blobs, "pruned_bytes": pruned_bytes, "authority_unreadable": authority_unreadable},
    }
    if not stopped:
        if not authority_unreadable:
            fields["counter"] = counter
        fields.update(projects_sig=proj_sig, review_ledger_sig=rev_sig, usage_sig=usg_sig, queue_sig=q_sig, custody_sig=_custody_sig(root_path) if (buckets["custody"] or authority_unreadable) else None)
        if cached_rev_out is not None:
            fields["review_pending_ids"] = cached_rev_out
    with store.open_store(root_path, write=True) as txn:
        maintenance_jobs.record_in(txn, JOB, **fields)
    total_items = pruned_tasks + pruned_blobs
    if total_items:
        append_jsonl(root_path / "logs" / "events.jsonl", {"ts": utc_now_iso(), "type": "observability_retention", "pruned_tasks": pruned_tasks, "pruned_packs": pruned_packs, "pruned_calls": pruned_calls, "pruned_blobs": pruned_blobs, "pruned_bytes": pruned_bytes})
    return {"state": state, "items": total_items}
