"""Settled artifact bulk retention, bounded tombstones, and stale backup discovery.

Shared single source of truth used by both:
- ``storage_free.py`` (Dashboard -> Storage -> Temporary space: ``stale_backups`` and
  ``settled_artifact_bulk``, both owner-confirmed and unchecked by default), and
- Automated settled-artifact retention (7-day grace window / closed-project pruning).

Safety invariants:
1. True positive-identification allowlist: registered deliverables (``.artifact_manifest.json``,
   including ``user_file``, ``process_output``, ``process_output_directory*``,
   ``verification_ledger``, ``workspace_patch*``, ``memory_export``, ``delete_recovery``),
   compact JSON verdicts/recipes (``*.json`` <= ``COMPACT_VERDICT_BYTES`` at root in
   closed-project mode or inside test-evidence runs), staged inputs (``attachments/``,
   ``chat_media/``), delegated patches (``delegated_runs/``), ``source_handles/`` other than
   ``working`` and raw ``test-evidence``, compact root files (<= ``BULK_FILE_BYTES`` in default
   7-day/Storage Free mode; closed-project mode sets ``bulk_file_bytes=0`` to prune remaining
   unregistered bulk files/dirs), and metadata/tombstones are NEVER pruned here.
2. Only settled tasks proven terminal, not live, whose root task is also terminal and not
   live, whose child drive is already settled/absent, and holding zero open custody /
   promotion / review / plan obligations (treating NULL collection flags as pending) are
   eligible; ``prune_task_artifact_bulk`` takes the per-task ``.custody.lock`` non-blocking.
3. ``pruned_manifest.json`` is written/merged BEFORE moving/unlinking targets (rolling back
   any entry whose removal fails) and capped at ``MAX_TOMBSTONE_ENTRIES`` with overflow
   rollups so readers consistently surface typed ``pruned_by_retention``.
4. Stale backups under ``backups/*`` and ``state/backup_dir*`` enforce a 24h mtime age floor,
   resolve active record paths relative to ``root``, and treat any directory targeted by a
   sibling symlink (e.g. ``state/backup_dir -> backup_dir_*``) as in use.
"""
from __future__ import annotations

import errno
import logging
import os
import pathlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

PRUNED_MANIFEST_NAME = "pruned_manifest.json"
PRUNED_STATUS = "pruned_by_retention"
SETTLED_ARTIFACT_GRACE_DAYS = 7
STALE_BACKUP_MIN_AGE_SEC = 24 * 3600  # never offer a backup written in the last 24h
BULK_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB threshold for unregistered bulk files & dirs
COMPACT_VERDICT_BYTES = 1 * 1024 * 1024  # keep compact <= 1 MiB JSON verdicts/recipes inside test-evidence
MAX_TOMBSTONE_ENTRIES = 500

PRESERVED_ROOT_SUBDIRS = frozenset({"attachments", "chat_media", "delegated_runs", "source_handles"})
PRESERVED_METADATA_FILES = frozenset({
    ".artifact_manifest.json",
    ".artifact_manifest.json.lock",
    ".scratch_manifest.json",
    PRUNED_MANIFEST_NAME,
    "verification_receipts.jsonl",
    "verification_ledger.json",
})
GENERATED_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache"})


def _utc_mtime_label(path: pathlib.Path) -> str:
    try:
        ts = path.lstat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        return "time unknown"


def _resolve_under_root(root: pathlib.Path, raw: str) -> str:
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = root / p
    return str(p.resolve(strict=False))


def _walk_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _sibling_symlink_targets(directory: pathlib.Path) -> Set[str]:
    """Resolved targets of any sibling symlink in DIRECTORY (e.g. state/backup_dir -> backup_dir_*)."""
    targets: Set[str] = set()
    if not directory.is_dir() or directory.is_symlink():
        return targets
    try:
        for entry in directory.iterdir():
            if entry.is_symlink():
                try:
                    targets.add(str(entry.resolve(strict=False)))
                except OSError:
                    continue
    except OSError:
        pass
    return targets


def _active_backup_paths(root: pathlib.Path) -> Set[str]:
    """Paths referenced by active switch records, skill rollback records, or sibling symlinks."""
    refs: Set[str] = set()
    state_dir = root / "state"
    for name in ("switch_intent.json", "switch_receipt.json"):
        doc = read_json_dict(state_dir / name)
        if isinstance(doc, dict):
            for value in _walk_strings(doc):
                if "backup" in value.lower():
                    refs.add(_resolve_under_root(root, value))
    skills_state = state_dir / "skills"
    if skills_state.is_dir() and not skills_state.is_symlink():
        for marker in skills_state.glob("*/update_backup.json"):
            doc = read_json_dict(marker)
            raw = str((doc or {}).get("backup_dir") or "").strip()
            if raw:
                refs.add(_resolve_under_root(root, raw))
    refs |= _sibling_symlink_targets(root / "backups")
    refs |= _sibling_symlink_targets(state_dir)
    return refs


def list_stale_backups(
    root: Any,
    *,
    min_age_sec: float = STALE_BACKUP_MIN_AGE_SEC,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ``(candidates, in_use_count)`` for stale backup folders under ``root``.

    Inspects non-symlink directories directly under ``backups/`` and ``state/backup_dir*``.
    Directories targeted by a sibling symlink, referenced by an active switch/skill rollback
    record, or modified more recently than ``min_age_sec`` are counted as ``in_use`` and never offered.
    """
    root_path = pathlib.Path(root)
    active = _active_backup_paths(root_path)
    now_ts = time.time() if now is None else float(now)
    candidates: List[Dict[str, Any]] = []
    in_use = 0

    def _consider(entry: pathlib.Path, rel_label: str) -> None:
        nonlocal in_use
        if entry.is_symlink() or not entry.is_dir():
            return
        resolved = str(entry.resolve(strict=False))
        if resolved in active:
            in_use += 1
            return
        try:
            age = now_ts - entry.lstat().st_mtime
        except OSError:
            in_use += 1
            return
        if age < min_age_sec:
            in_use += 1
            return
        stamp = _utc_mtime_label(entry)
        trash_tag = rel_label.replace("/", "_")
        candidates.append({
            "key": f"backup:{trash_tag}",
            "trash_tag": trash_tag,
            "relpath": rel_label,
            "path": entry,
            "name": f"{rel_label} ({stamp})",
            "mtime_label": stamp,
        })

    backups_dir = root_path / "backups"
    if backups_dir.is_dir() and not backups_dir.is_symlink():
        for entry in sorted(backups_dir.iterdir(), key=lambda p: p.name):
            _consider(entry, f"backups/{entry.name}")

    state_dir = root_path / "state"
    if state_dir.is_dir() and not state_dir.is_symlink():
        for entry in sorted(state_dir.iterdir(), key=lambda p: p.name):
            if entry.name == "backup_dir" or entry.name.startswith("backup_dir_"):
                _consider(entry, f"state/{entry.name}")

    return candidates, in_use


def retire_stale_backup(
    root: Any,
    target: pathlib.Path,
    *,
    min_age_sec: float = STALE_BACKUP_MIN_AGE_SEC,
    now: Optional[float] = None,
) -> str:
    """Re-verify TARGET is an inactive, aged stale backup under ROOT and move it to custody_trash.

    Returns ``""`` on success (or already gone), else a typed kept reason.
    """
    root_path = pathlib.Path(root).resolve(strict=False)
    if not os.path.lexists(target):
        return ""
    if target.is_symlink() or not target.is_dir():
        return "other"
    backups_dir = root_path / "backups"
    state_dir = root_path / "state"
    if backups_dir.is_symlink() or state_dir.is_symlink():
        return "other"
    resolved = target.resolve(strict=False)
    backups_root = backups_dir.resolve(strict=False)
    state_root = state_dir.resolve(strict=False)
    allowed = (
        resolved.parent == backups_root
        or (resolved.parent == state_root and (target.name == "backup_dir" or target.name.startswith("backup_dir_")))
    )
    if not allowed:
        return "other"
    if str(resolved) in _active_backup_paths(root_path):
        return "in_use"
    now_ts = time.time() if now is None else float(now)
    try:
        if now_ts - target.lstat().st_mtime < min_age_sec:
            return "in_use"
    except OSError:
        return "other"
    rel_prefix = "backups" if resolved.parent == backups_root else "state"
    return _move_to_custody_trash(root_path, target, tag=f"{rel_prefix}_{target.name}")


def _move_to_custody_trash(root: pathlib.Path, path: pathlib.Path, *, tag: str = "") -> str:
    """Atomically move PATH into ``state/custody_trash`` for the maintenance child to delete.

    Never falls back to an in-thread recursive ``shutil.rmtree`` on permission/busy errors;
    cross-device rename (EXDEV) stages under a sibling ``.retiring-<uuid>`` name or fails typed.
    """
    trash_dir = root / "state" / "custody_trash"
    try:
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / f"{tag or path.name}-{uuid.uuid4().hex[:12]}"
        os.rename(path, dest)
        return ""
    except FileNotFoundError:
        return ""
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            try:
                sibling = path.with_name(f".retiring-{path.name}-{uuid.uuid4().hex[:12]}")
                os.rename(path, sibling)
                return "other"
            except OSError:
                return "other"
        return "busy" if exc.errno in (errno.EBUSY, errno.EAGAIN) else "other"


# ---- Settled artifact bulk classification, pruning, and tombstones --------------------------


def _dir_summary(path: pathlib.Path, *, stop: Optional[Callable[[], bool]] = None) -> Tuple[int, int, bool, int]:
    """Return ``(file_count, total_bytes, has_evidence_marker, unreadable)`` without following symlinks."""
    from ouroboros.test_evidence import EVIDENCE_RUN_MARKER

    files = 0
    total = 0
    marker = False
    unreadable = 0
    stack = [path]
    while stack:
        if stop and stop():
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(pathlib.Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name == EVIDENCE_RUN_MARKER:
                                marker = True
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        unreadable += 1
        except OSError:
            unreadable += 1
    return files, total, marker, unreadable


def _registered_names(artifact_dir: pathlib.Path) -> Set[str]:
    manifest = read_json_dict(artifact_dir / ".artifact_manifest.json") or {}
    entries = manifest.get("artifacts")
    if not isinstance(entries, dict):
        return set()
    return {str(name) for name, row in entries.items() if isinstance(row, dict)}


def _classify_test_evidence_tree(
    base: pathlib.Path,
    rel_prefix: str,
    *,
    stop: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Select only heavy raw bulk inside a test-evidence tree, preserving compact JSON verdicts & markers."""
    from ouroboros.test_evidence import EVIDENCE_RUN_MARKER

    evictable: List[Dict[str, Any]] = []
    unreadable = 0

    def _collect_run_bulk(run_dir: pathlib.Path, run_rel: str) -> None:
        nonlocal unreadable
        try:
            children = sorted(run_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            unreadable += 1
            return
        for child in children:
            if stop and stop():
                return
            if child.is_symlink():
                continue
            child_rel = f"{run_rel}/{child.name}"
            if child.is_dir():
                fc, sz, _, unr = _dir_summary(child, stop=stop)
                unreadable += unr
                evictable.append({
                    "relpath": child_rel,
                    "path": child,
                    "kind": "test_evidence_raw_dir",
                    "is_dir": True,
                    "file_count": fc,
                    "bytes": sz,
                })
            elif child.is_file():
                if child.name == EVIDENCE_RUN_MARKER:
                    continue
                try:
                    sz = child.lstat().st_size
                except OSError:
                    unreadable += 1
                    continue
                # Preserve compact JSON verdicts/recipes (result.json, source.json, etc.).
                if child.suffix.lower() == ".json" and sz <= COMPACT_VERDICT_BYTES:
                    continue
                evictable.append({
                    "relpath": child_rel,
                    "path": child,
                    "kind": "test_evidence_raw_file",
                    "is_dir": False,
                    "file_count": 1,
                    "bytes": sz,
                })

    if (base / EVIDENCE_RUN_MARKER).is_file():
        _collect_run_bulk(base, rel_prefix)
        return evictable, unreadable

    try:
        top_children = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return [], 1

    for sub in top_children:
        if stop and stop():
            break
        if sub.is_symlink():
            continue
        sub_rel = f"{rel_prefix}/{sub.name}"
        if sub.is_dir():
            if (sub / EVIDENCE_RUN_MARKER).is_file():
                _collect_run_bulk(sub, sub_rel)
            else:
                fc, sz, _, unr = _dir_summary(sub, stop=stop)
                unreadable += unr
                evictable.append({
                    "relpath": sub_rel,
                    "path": sub,
                    "kind": "test_evidence_raw_dir",
                    "is_dir": True,
                    "file_count": fc,
                    "bytes": sz,
                })
        elif sub.is_file():
            try:
                sz = sub.lstat().st_size
            except OSError:
                unreadable += 1
                continue
            if sub.suffix.lower() == ".json" and sz <= COMPACT_VERDICT_BYTES:
                continue
            evictable.append({
                "relpath": sub_rel,
                "path": sub,
                "kind": "test_evidence_raw_file",
                "is_dir": False,
                "file_count": 1,
                "bytes": sz,
            })

    return evictable, unreadable


def classify_task_artifact_bulk(
    artifact_dir: pathlib.Path,
    *,
    bulk_file_bytes: Optional[int] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ``(evictable_entries, unreadable_count)`` inside one settled task's artifact directory.

    True allowlist: all registered deliverables (``.artifact_manifest.json``, including
    ``user_file``, ``process_output``, ``process_output_directory*``, ``verification_ledger``,
    ``workspace_patch*``), compact JSON verdicts inside test-evidence runs (<= 1 MiB) and at root
    when ``bulk_file_bytes == 0``, ``attachments/``, ``chat_media/``, ``delegated_runs/``, and
    compact root files (<= ``BULK_FILE_BYTES`` in default mode) stay intact.
    """
    if bulk_file_bytes is None:
        bulk_file_bytes = BULK_FILE_BYTES
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        return [], 0
    registered = _registered_names(artifact_dir)
    evictable: List[Dict[str, Any]] = []
    unreadable = 0

    try:
        entries = sorted(artifact_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return [], 1

    for entry in entries:
        if stop and stop():
            break
        name = entry.name
        if entry.is_symlink() or name in PRESERVED_METADATA_FILES:
            continue
        if entry.is_dir():
            if name in GENERATED_CACHE_DIRS:
                fc, sz, _, unr = _dir_summary(entry, stop=stop)
                unreadable += unr
                evictable.append({
                    "relpath": name,
                    "path": entry,
                    "kind": "generated_cache_dir",
                    "is_dir": True,
                    "file_count": fc,
                    "bytes": sz,
                })
                continue
            if name == "source_handles":
                try:
                    sh_entries = sorted(entry.iterdir(), key=lambda p: p.name)
                except OSError:
                    unreadable += 1
                    continue
                for sub in sh_entries:
                    if stop and stop():
                        break
                    if sub.is_symlink() or not sub.is_dir():
                        continue
                    if sub.name.startswith("test-evidence"):
                        items, unr = _classify_test_evidence_tree(sub, f"source_handles/{sub.name}", stop=stop)
                        unreadable += unr
                        if sum(int(i["bytes"]) for i in items) > bulk_file_bytes:
                            evictable.extend(items)
                    elif sub.name == "working":
                        fc, sz, _, unr = _dir_summary(sub, stop=stop)
                        unreadable += unr
                        if sz > bulk_file_bytes:
                            evictable.append({
                                "relpath": "source_handles/working",
                                "path": sub,
                                "kind": "working_handle_dir",
                                "is_dir": True,
                                "file_count": fc,
                                "bytes": sz,
                            })
                continue
            if name in PRESERVED_ROOT_SUBDIRS or name in registered:
                continue
            fc, sz, has_marker, unr = _dir_summary(entry, stop=stop)
            unreadable += unr
            if has_marker:
                items, te_unr = _classify_test_evidence_tree(entry, name, stop=stop)
                unreadable += te_unr
                if sum(int(i["bytes"]) for i in items) > bulk_file_bytes or sz > bulk_file_bytes:
                    evictable.extend(items)
            elif sz > bulk_file_bytes:
                evictable.append({
                    "relpath": name,
                    "path": entry,
                    "kind": "unregistered_bulk_directory",
                    "is_dir": True,
                    "file_count": fc,
                    "bytes": sz,
                })
            continue
        if entry.is_file():
            if name in registered:
                continue  # registered deliverables (user_file, process_output, process_output_directory, etc.) always stay
            try:
                sz = entry.lstat().st_size
            except OSError:
                unreadable += 1
                continue
            if bulk_file_bytes == 0 and entry.suffix.lower() == ".json" and sz <= COMPACT_VERDICT_BYTES:
                continue  # closed-project prune keeps compact JSON verdicts/receipts alongside deliverables
            if sz > bulk_file_bytes:
                evictable.append({
                    "relpath": name,
                    "path": entry,
                    "kind": "unregistered_bulk_file",
                    "is_dir": False,
                    "file_count": 1,
                    "bytes": sz,
                })

    return evictable, unreadable


def parse_epoch_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def pending_row_hold(row: Dict[str, Any]) -> Tuple[str, str]:
    """Shared task-row hold predicate for artifact and observability retention."""
    for flag in (
        "owes_synthesis",
        "owes_terminal_projection",
        "has_pending_promotion",
        "delegated_runs_unreconciled",
        "pending_drive",
    ):
        if int(row.get(flag) or 0) != 0:
            return "open_custody", flag
    if row.get("review_collection_pending") is None or int(row.get("review_collection_pending")) != 0:
        return "open_review", "review_collection_pending"
    if row.get("plan_collection_pending") is None or int(row.get("plan_collection_pending")) != 0:
        return "open_custody", "plan_collection_pending"
    return "", ""


def task_eligible_for_bulk_prune(
    root: pathlib.Path,
    task_id: str,
    row: Optional[Dict[str, Any]],
    rows_by_id: Dict[str, Dict[str, Any]],
    live: Callable[[str], Optional[bool]],
    cutoff_ts: Optional[float],
) -> Tuple[str, bool]:
    """Return ``(verdict, inside_wait)`` where verdict is ``offer`` | ``in_use`` | ``unreadable``."""
    from ouroboros.task_status import FINAL_STATUSES

    if row is None:
        return "unreadable", False
    status = str(row.get("status") or "")
    if status not in FINAL_STATUSES:
        return "in_use", False
    if live(task_id) is not False:
        return "in_use", False
    # Check root task lineage: never prune a terminal child while its root is still live or non-terminal.
    root_tid = str(row.get("root_task_id") or "").strip()
    if root_tid and root_tid != task_id:
        root_row = rows_by_id.get(root_tid)
        if root_row is None:
            fetched = _task_retention_rows(root, [root_tid])
            root_row = fetched.get(root_tid)
            if root_row is not None:
                rows_by_id[root_tid] = root_row
        if root_row is None:
            return "unreadable", False
        if str(root_row.get("status") or "") not in FINAL_STATUSES or live(root_tid) is not False:
            return "in_use", False
    if pending_row_hold(row)[0]:
        return "in_use", False
    # Require any child execution drive to already be retired/gone so late copy-back cannot resurrect bulk.
    for drive_col in ("child_drive_root", "headless_child_drive_root"):
        raw_drive = str(row.get(drive_col) or "").strip()
        if raw_drive and os.path.lexists(raw_drive):
            return "in_use", False

    epoch: Optional[float] = None
    for key in ("drive_retention_at", "updated_at", "ts"):
        parsed = parse_epoch_seconds(row.get(key))
        if parsed is not None:
            epoch = parsed
            break
    inside_wait = bool(cutoff_ts is not None and (epoch is None or epoch > cutoff_ts))
    return "offer", inside_wait


def load_retention_rows(root: Any, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load retention rows for TASK_IDS and prefetch any missing ``root_task_id`` rows."""
    root_path = pathlib.Path(root)
    rows_by_id = _task_retention_rows(root_path, task_ids)
    missing_roots = sorted({
        str(r.get("root_task_id") or "")
        for r in rows_by_id.values()
        if r.get("root_task_id") and str(r.get("root_task_id")) not in rows_by_id
    })
    if missing_roots:
        rows_by_id.update(_task_retention_rows(root_path, missing_roots))
    return rows_by_id


def _task_retention_rows(root: pathlib.Path, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Read typed task columns only (no body decodes) for eligibility classification."""
    from ouroboros import store

    if not task_ids:
        return {}
    cols = (
        "task_id", "status", "root_task_id", "parent_task_id", "project_id",
        "child_drive_root", "headless_child_drive_root",
        "ts", "updated_at", "drive_retention_at",
        "owes_synthesis", "owes_terminal_projection", "has_pending_promotion",
        "delegated_runs_unreconciled", "pending_drive",
        "review_collection_pending", "plan_collection_pending",
    )
    out: Dict[str, Dict[str, Any]] = {}
    with store.open_store(root, write=False) as txn:
        for start in range(0, len(task_ids), 256):
            chunk = task_ids[start:start + 256]
            rows = txn.conn.execute(
                f"SELECT {','.join(cols)} FROM tasks WHERE task_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            for rec in rows:
                out[str(rec["task_id"])] = {c: rec[c] for c in cols}
    return out


def list_settled_artifact_bulk_candidates(
    root: Any,
    *,
    live: Callable[[str], Optional[bool]],
    grace_days: int = SETTLED_ARTIFACT_GRACE_DAYS,
    stop: Optional[Callable[[], bool]] = None,
    only_outside_wait: bool = False,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Scan ``task_results/artifacts/*`` and return ``(candidates, in_use, unreadable)``."""
    root_path = pathlib.Path(root)
    art_root = root_path / "task_results" / "artifacts"
    if not art_root.is_dir() or art_root.is_symlink():
        return [], 0, 0

    cutoff_ts = (datetime.now(timezone.utc).timestamp() - max(0, int(grace_days)) * 86400) if grace_days > 0 else None

    dirs = [e for e in sorted(art_root.iterdir(), key=lambda p: p.name) if e.is_dir() and not e.is_symlink()]
    if not dirs:
        return [], 0, 0
    rows_by_id = load_retention_rows(root_path, [d.name for d in dirs])

    candidates: List[Dict[str, Any]] = []
    in_use = 0
    unreadable = 0
    for task_dir in dirs:
        if stop and stop():
            break
        tid = task_dir.name
        verdict, inside_wait = task_eligible_for_bulk_prune(
            root_path, tid, rows_by_id.get(tid), rows_by_id, live, cutoff_ts,
        )
        if verdict != "offer":
            if verdict == "unreadable":
                unreadable += 1
            else:
                in_use += 1
            continue
        if only_outside_wait and inside_wait:
            continue
        evictable, entry_unreadable = classify_task_artifact_bulk(task_dir, stop=stop)
        if not evictable:
            continue
        total_bytes = sum(int(item["bytes"]) for item in evictable)
        candidates.append({
            "task_id": tid,
            "key": f"bulk:bulk_{tid}",
            "path": task_dir,
            "inside_wait": inside_wait,
            "targets": evictable,
            "bytes": total_bytes,
            "unreadable": entry_unreadable,
            "target_count": len(evictable),
        })
    return candidates, in_use, unreadable


def _merge_pruned_manifest(
    artifact_dir: pathlib.Path,
    new_entries: List[Dict[str, Any]],
    *,
    reason: str,
    remove_relpaths: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Write/merge bounded directory-level ``pruned_manifest.json`` tombstone."""
    manifest_path = artifact_dir / PRUNED_MANIFEST_NAME
    existing = read_json_dict(manifest_path) or {}
    prior_entries = list(existing.get("entries") or []) if isinstance(existing.get("entries"), list) else []
    by_rel: Dict[str, Dict[str, Any]] = {}
    for item in prior_entries:
        if isinstance(item, dict) and item.get("relpath"):
            by_rel[str(item["relpath"])] = dict(item)
    now = utc_now_iso()
    for item in new_entries:
        rel = str(item.get("relpath") or "")
        if not rel:
            continue
        by_rel[rel] = {
            "relpath": rel,
            "kind": str(item.get("kind") or "bulk"),
            "is_dir": bool(item.get("is_dir")),
            "file_count": int(item.get("file_count") or 0),
            "bytes": int(item.get("bytes") or 0),
            "pruned_at": now,
            "reason": reason,
        }
    for rel in (remove_relpaths or ()):
        by_rel.pop(rel, None)
    ordered = sorted(by_rel.values(), key=lambda r: r["relpath"])
    overflow_count = int(existing.get("overflow_count") or 0)
    overflow_bytes = int(existing.get("overflow_bytes") or 0)
    if len(ordered) > MAX_TOMBSTONE_ENTRIES:
        dropped = ordered[MAX_TOMBSTONE_ENTRIES:]
        ordered = ordered[:MAX_TOMBSTONE_ENTRIES]
        overflow_count += len(dropped)
        overflow_bytes += sum(int(d.get("bytes") or 0) for d in dropped)
    doc: Dict[str, Any] = {
        "schema_version": 1,
        "status": PRUNED_STATUS,
        "updated_at": now,
        "reason": reason,
        "entries": ordered,
    }
    if overflow_count:
        doc["overflow_count"] = overflow_count
        doc["overflow_bytes"] = overflow_bytes
    atomic_write_json(manifest_path, doc, trailing_newline=True)
    return doc


def prune_task_artifact_bulk(
    root: Any,
    task_id: str,
    *,
    live: Callable[[str], Optional[bool]],
    reason: str = "owner_storage_free",
    grace_days: int = 0,
    bulk_file_bytes: Optional[int] = None,
    only_relpaths: Optional[Iterable[str]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Re-verify eligibility under the per-task custody lock, write ``pruned_manifest.json``,
    and move/unlink evictable bulk for TASK_ID."""
    from ouroboros.task_custody import task_custody_lock

    root_path = pathlib.Path(root).resolve(strict=False)
    artifact_dir = root_path / "task_results" / "artifacts" / str(task_id)
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        return {"task_id": task_id, "status": "absent", "removed": 0, "removed_bytes": 0, "moved_dirs": 0, "reason": ""}

    with task_custody_lock(root_path, str(task_id), timeout_sec=0.0) as acquired:
        if not acquired:
            return {"task_id": task_id, "status": "kept", "removed": 0, "removed_bytes": 0, "moved_dirs": 0, "reason": "busy"}

        cutoff_ts = (datetime.now(timezone.utc).timestamp() - max(0, int(grace_days)) * 86400) if grace_days > 0 else None
        rows = _task_retention_rows(root_path, [str(task_id)])
        verdict, inside_wait = task_eligible_for_bulk_prune(
            root_path, str(task_id), rows.get(str(task_id)), rows, live, cutoff_ts,
        )
        if verdict != "offer":
            return {"task_id": task_id, "status": "kept", "removed": 0, "removed_bytes": 0, "moved_dirs": 0,
                    "reason": "unreadable_record" if verdict == "unreadable" else "in_use"}
        if grace_days > 0 and inside_wait:
            return {"task_id": task_id, "status": "kept", "removed": 0, "removed_bytes": 0, "moved_dirs": 0, "reason": "inside_wait"}

        targets, _unr = classify_task_artifact_bulk(artifact_dir, bulk_file_bytes=bulk_file_bytes, stop=stop)
        if only_relpaths is not None:
            allowed = set(only_relpaths)
            targets = [t for t in targets if t["relpath"] in allowed]
        if not targets:
            return {"task_id": task_id, "status": "empty", "removed": 0, "removed_bytes": 0, "moved_dirs": 0, "reason": ""}

        # Write tombstone BEFORE moving/unlinking so readers never observe an untombstoned 404.
        try:
            _merge_pruned_manifest(artifact_dir, targets, reason=reason)
        except OSError:
            return {"task_id": task_id, "status": "kept", "removed": 0, "removed_bytes": 0, "moved_dirs": 0, "reason": "other"}

        pruned_count = 0
        moved_dirs = 0
        removed_bytes = 0
        failed_relpaths: Set[str] = set()
        art_resolved = artifact_dir.resolve(strict=False)
        trash_tag = f"bulk_{task_id}"
        for item in targets:
            if stop and stop():
                failed_relpaths.add(str(item["relpath"]))
                continue
            target_path: pathlib.Path = item["path"]
            if target_path.is_symlink():
                failed_relpaths.add(str(item["relpath"]))
                continue
            resolved = target_path.resolve(strict=False)
            if not resolved.is_relative_to(art_resolved) or resolved == art_resolved:
                failed_relpaths.add(str(item["relpath"]))
                continue
            try:
                if item["is_dir"]:
                    err = _move_to_custody_trash(root_path, target_path, tag=trash_tag)
                    if err:
                        failed_relpaths.add(str(item["relpath"]))
                        continue
                    moved_dirs += 1
                else:
                    target_path.unlink(missing_ok=True)
                pruned_count += 1
                removed_bytes += int(item["bytes"])
            except OSError:
                failed_relpaths.add(str(item["relpath"]))

        if failed_relpaths:
            try:
                _merge_pruned_manifest(artifact_dir, [], reason=reason, remove_relpaths=failed_relpaths)
            except OSError:
                pass

        if failed_relpaths:
            status = "partial" if pruned_count else "kept"
            out_reason = "other"
        else:
            status = "pruned"
            out_reason = ""
        return {
            "task_id": task_id,
            "status": status,
            "removed": pruned_count,
            "removed_bytes": removed_bytes,
            "moved_dirs": moved_dirs,
            "failed": len(failed_relpaths),
            "reason": out_reason,
        }


def pruned_retention_entry(
    artifact_dir: pathlib.Path,
    relpath_or_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the matching ``pruned_manifest.json`` entry if RELPATH_OR_NAME was pruned by retention."""
    rel = str(relpath_or_name or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    manifest = read_json_dict(artifact_dir / PRUNED_MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("status") != PRUNED_STATUS:
        return None
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_rel = str(entry.get("relpath") or "").replace("\\", "/").strip("/")
        if not entry_rel:
            continue
        if rel == entry_rel or (entry.get("is_dir") and rel.startswith(entry_rel + "/")):
            return entry
    return None


def pruned_retention_for_path(raw_path: str) -> Optional[Dict[str, Any]]:
    """Infer task artifact dir from RAW_PATH and check its ``pruned_manifest.json`` tombstone."""
    if not raw_path:
        return None
    path = pathlib.Path(raw_path)
    parts = path.parts
    for idx in range(len(parts) - 3):
        if parts[idx] == "task_results" and parts[idx + 1] == "artifacts":
            artifact_dir = pathlib.Path(*parts[: idx + 3])
            rel = "/".join(parts[idx + 3 :])
            if rel and (artifact_dir / PRUNED_MANIFEST_NAME).is_file():
                return pruned_retention_entry(artifact_dir, rel)
    return None


def project_pruned_artifacts(stores: Iterable[pathlib.Path], rows: Any) -> Any:
    """Stamp ``status='pruned_by_retention'`` on recorded rows covered by a store's ``pruned_manifest.json``."""
    if not isinstance(rows, list) or not rows:
        return rows
    store_list = [s for s in stores if (s / PRUNED_MANIFEST_NAME).is_file()]
    if not store_list:
        return rows
    projected: List[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            projected.append(row)
            continue
        rel = str(row.get("relpath") or row.get("name") or "").strip("/")
        raw_path = str(row.get("path") or "")
        if rel and (not raw_path or not os.path.lexists(raw_path)):
            matched = next((pruned_retention_entry(st, rel) for st in store_list if pruned_retention_entry(st, rel)), None)
            if matched is not None:
                projected.append({**row, "status": PRUNED_STATUS})
                continue
        projected.append(row)
    return projected
