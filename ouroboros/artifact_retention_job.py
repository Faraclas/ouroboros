"""Scheduled maintenance-child job for settled artifact bulk (7-day window) and closed-project pruning.

Reuses ``ouroboros.artifact_retention`` (true semantic allowlist, per-task ``.custody.lock``,
bounded directory-level ``pruned_manifest.json``, and typed ``pruned_by_retention`` readers).

Two automated retention rules:
1. Settled tasks outside ``SETTLED_ARTIFACT_GRACE_DAYS`` (7 days): prunes unregistered bulk
   (``> BULK_FILE_BYTES``), generated caches, and heavy raw test-evidence logs/subdirectories while
   preserving registered deliverables, compact verdicts (<= 1 MiB), attachments, chat media,
   delegated runs, and open custody/review/plan/live lineage.
2. Closed (tombstoned) projects: prunes raw artifacts (``grace_days=0``, ``bulk_file_bytes=0``,
   still preserving registered deliverables, compact JSON verdicts, and preserved metadata/subdirs)
   strictly gated on verified existence AND active FTS5 indexing of
   ``projects/<id>/closure_brief.md`` (fail closed if missing, unindexed, reopened, or unreadable).

Rest/activity law (``tests/test_cost_contract.py`` B6): unchanged inputs rest with zero store
writes and zero history/execution walks; unrelated completions inspect only newly written terminal
tasks (plus due young tasks, held waiting tasks on store movement, and newly closed/indexed projects).
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ouroboros import artifact_retention, maintenance_jobs, store
from ouroboros.utils import append_jsonl, utc_now_iso

JOB = "artifact_retention"


def _terminal_writes(root: pathlib.Path) -> int:
    with store.open_store(root, write=False) as txn:
        return int(txn.meta(store.TERMINAL_WRITES) or 0)


def _waiting_sig(root: pathlib.Path, waiting_ids: List[str]) -> Optional[List[Any]]:
    if not waiting_ids:
        return None
    qsig = _stat_sig(root / "state" / "queue_snapshot.json")
    rows: List[List[Any]] = []
    with store.open_store(root, write=False) as txn:
        for start in range(0, len(waiting_ids), 128):
            chunk = waiting_ids[start:start + 128]
            for rec in txn.conn.execute(
                f"SELECT task_id, write_seq, review_collection_pending, plan_collection_pending "
                f"FROM tasks WHERE task_id IN ({','.join('?' * len(chunk))}) ORDER BY task_id",
                chunk,
            ).fetchall():
                rows.append([str(rec[0]), int(rec[1] or 0), rec[2], rec[3]])
    return [qsig, rows]


def _stat_sig(path: pathlib.Path) -> Optional[List[int]]:
    try:
        st = path.lstat()
        return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError:
        return None


def _projects_index_sig(root: pathlib.Path) -> List[Optional[List[int]]]:
    from ouroboros import memory_index

    return [
        _stat_sig(root / "state" / "projects.json"),
        _stat_sig(memory_index.index_path(root)),
    ]


def _queue_live(root: pathlib.Path) -> Callable[[str], bool]:
    """Fail-open child liveness probe over ``queue_snapshot.json`` (self + subtree)."""
    from ouroboros.task_status import task_has_live_queue_ownership, task_subtree_has_live_queue_rows

    return lambda task_id: (
        task_has_live_queue_ownership(root, task_id)
        or task_subtree_has_live_queue_rows(root, task_id)
    )


def _has_any_entry(directory: pathlib.Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def due(root: Any, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Due when pending or when artifact directories exist and backlog, 7-day grace expiry,
    new terminal writes, held-task store/queue changes, or project/index changes occurred."""
    root_path = pathlib.Path(root)
    state = row.get("state")
    if state == "pending":
        return True
    waiting_ids = [str(x) for x in (row.get("waiting") or []) if x]
    if not _has_any_entry(root_path / "task_results" / "artifacts"):
        if state == "done" and (waiting_ids or row.get("young")):
            if _terminal_writes(root_path) != row.get("counter"):
                return True
            if row.get("next_due") is not None and time.time() >= float(row["next_due"]):
                return True
            if waiting_ids and _waiting_sig(root_path, waiting_ids) != row.get("waiting_sig"):
                return True
        return False
    if state != "done":
        return True
    next_due = row.get("next_due")
    if next_due is not None and time.time() >= float(next_due):
        return True
    if _terminal_writes(root_path) != row.get("counter"):
        return True
    if waiting_ids and _waiting_sig(root_path, waiting_ids) != row.get("waiting_sig"):
        return True
    if _projects_index_sig(root_path) != row.get("projects_sig"):
        return True
    return False


def _inspect_closed_projects(root: pathlib.Path) -> Tuple[Dict[str, List[int]], Set[str], bool]:
    """Return ``(verified_closed_sigs, unindexed_closed_ids, registry_ok)`` (fail closed)."""
    from ouroboros import memory_index, projects_registry
    from ouroboros.project_facts import project_closure_brief_path

    reg_path = root / "state" / "projects.json"
    if not reg_path.exists():
        return {}, set(), True
    try:
        rows = projects_registry.list_reserved_projects(root, strict=True)
    except Exception:
        return {}, set(), False

    tombstoned = [
        str(entry.get("id") or "").strip()
        for entry in rows
        if entry.get("id") and entry.get("lifecycle") == projects_registry.PROJECT_TOMBSTONED
    ]
    if not tombstoned:
        return {}, set(), True
    indexed_ids = memory_index.indexed_closure_briefs(root, tombstoned)
    verified: Dict[str, List[int]] = {}
    unindexed: Set[str] = set()
    for pid in tombstoned:
        if pid in indexed_ids:
            sig = _stat_sig(project_closure_brief_path(pid, drive_root=root))
            if sig is not None:
                verified[pid] = sig
                continue
        unindexed.add(pid)
    return verified, unindexed, True


def _still_closed_and_verified(root: pathlib.Path, pid: str, expected_sig: Optional[List[int]]) -> bool:
    """Re-verify project tombstone lifecycle and brief sig right before closed-project prune."""
    from ouroboros import projects_registry
    from ouroboros.project_facts import project_closure_brief_path

    try:
        entry = projects_registry.get_reserved_project(root, pid, strict=True)
    except Exception:
        return False
    if not isinstance(entry, dict) or entry.get("lifecycle") != projects_registry.PROJECT_TOMBSTONED:
        return False
    sig = _stat_sig(project_closure_brief_path(pid, drive_root=root))
    return sig is not None and sig == expected_sig


def _project_task_ids(root: pathlib.Path, project_ids: List[str]) -> Set[str]:
    """Return tasks directly in PROJECT_IDS plus child tasks whose root belongs to PROJECT_IDS."""
    if not project_ids:
        return set()
    ids: Set[str] = set()
    with store.open_store(root, write=False) as txn:
        for start in range(0, len(project_ids), 128):
            chunk = project_ids[start:start + 128]
            placeholders = ",".join("?" * len(chunk))
            rows = txn.conn.execute(
                f"SELECT task_id FROM tasks WHERE project_id IN ({placeholders}) "
                f"UNION "
                f"SELECT c.task_id FROM tasks c JOIN tasks r ON c.root_task_id = r.task_id "
                f"WHERE r.project_id IN ({placeholders})",
                chunk + chunk,
            ).fetchall()
            for rec in rows:
                ids.add(str(rec["task_id"]))
    return ids


def _task_epoch(row: Optional[Dict[str, Any]], now: float) -> float:
    if isinstance(row, dict):
        for key in ("drive_retention_at", "updated_at", "ts"):
            parsed = artifact_retention.parse_epoch_seconds(row.get(key))
            if parsed is not None:
                return parsed
    return now


def _list_artifact_dirs(art_root: pathlib.Path) -> List[str]:
    if not art_root.is_dir() or art_root.is_symlink():
        return []
    try:
        with os.scandir(art_root) as it:
            return sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and not e.is_symlink())
    except OSError:
        return []


def run(root: Any, should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Execute one incremental or backlog pass of automated settled & closed-project retention."""
    from ouroboros.task_store import terminal_writes_since

    stop = should_stop or (lambda: False)
    root_path = pathlib.Path(root).resolve(strict=False)
    row = maintenance_jobs.job(root_path, JOB) or {}
    now = time.time()
    counter = _terminal_writes(root_path)
    proj_sig = _projects_index_sig(root_path)
    backlog = row.get("counter") is None

    young: Dict[str, float] = {
        str(k): float(v)
        for k, v in (row.get("young") or {}).items()
        if isinstance(v, (int, float))
    }
    waiting: Set[str] = {str(x) for x in (row.get("waiting") or []) if x}
    prior_closed: Dict[str, List[int]] = {
        str(k): list(v)
        for k, v in (row.get("closed_verified") or {}).items()
        if isinstance(v, list)
    }
    prior_unindexed: Set[str] = {str(x) for x in (row.get("closed_unindexed") or []) if x}
    prior_registry_bad = bool(row.get("registry_unreadable"))
    art_root = root_path / "task_results" / "artifacts"

    sig_changed = backlog or proj_sig != row.get("projects_sig")
    closed_checked = False
    verified_closed: Dict[str, List[int]] = dict(prior_closed)
    unindexed_closed: Set[str] = set(prior_unindexed)
    registry_ok = not prior_registry_bad

    def _ensure_closed() -> None:
        nonlocal closed_checked, verified_closed, unindexed_closed, registry_ok
        if not closed_checked:
            if sig_changed or "closed_verified" not in row:
                verified_closed, unindexed_closed, registry_ok = _inspect_closed_projects(root_path)
            closed_checked = True

    if backlog:
        candidate_ids: Set[str] = set(_list_artifact_dirs(art_root)) | waiting | set(young)
        _ensure_closed()
    else:
        written = terminal_writes_since(root_path, int(row["counter"]))
        due_young = {tid for tid, elig in young.items() if elig <= now}
        waiting_list = sorted(waiting)
        due_waiting = waiting if (waiting_list and _waiting_sig(root_path, waiting_list) != row.get("waiting_sig")) else set()
        candidate_ids = set(written) | due_waiting | due_young
        if sig_changed:
            _ensure_closed()
            newly_verified = [pid for pid, sig in verified_closed.items() if prior_closed.get(pid) != sig]
            if newly_verified:
                candidate_ids |= _project_task_ids(root_path, newly_verified)
            if prior_registry_bad or prior_unindexed:
                candidate_ids |= waiting

    dir_candidates = sorted(
        tid for tid in candidate_ids
        if (art_root / tid).is_dir() and not (art_root / tid).is_symlink()
    )
    present_set = set(dir_candidates)
    for gone in (waiting & candidate_ids) - present_set:
        waiting.discard(gone)
    for gone in (set(young) & candidate_ids) - present_set:
        young.pop(gone, None)

    pruned_tasks = 0
    closed_pruned_tasks = 0
    settled_pruned_tasks = 0
    pruned_bytes = 0
    fail_closed_skipped = 0
    stopped = False

    if dir_candidates:
        _ensure_closed()
        live = _queue_live(root_path)
        rows_by_id = artifact_retention.load_retention_rows(root_path, dir_candidates)
        cutoff_ts = now - artifact_retention.SETTLED_ARTIFACT_GRACE_DAYS * 86400
        for idx, tid in enumerate(dir_candidates):
            if stop():
                stopped = True
                waiting.update(dir_candidates[idx:])
                break
            t_row = rows_by_id.get(tid)
            root_tid = str((t_row or {}).get("root_task_id") or "").strip()
            root_row = rows_by_id.get(root_tid) if root_tid else None
            pid = str((t_row or {}).get("project_id") or (root_row or {}).get("project_id") or "").strip()

            if not registry_ok and pid:
                fail_closed_skipped += 1
                waiting.add(tid)
                young.pop(tid, None)
                continue

            if pid and pid in unindexed_closed:
                fail_closed_skipped += 1
                waiting.add(tid)
                young.pop(tid, None)
                continue

            is_closed_verified = bool(pid and pid in verified_closed)
            verdict, inside_wait = artifact_retention.task_eligible_for_bulk_prune(
                root_path,
                tid,
                t_row,
                rows_by_id,
                live,
                None if is_closed_verified else cutoff_ts,
            )
            if verdict == "unreadable":
                waiting.discard(tid)
                young.pop(tid, None)
                continue
            if verdict == "in_use":
                if tid not in waiting:
                    evictable, _unr = artifact_retention.classify_task_artifact_bulk(
                        art_root / tid,
                        bulk_file_bytes=0 if is_closed_verified else artifact_retention.BULK_FILE_BYTES,
                        stop=stop,
                    )
                    if evictable:
                        waiting.add(tid)
                young.pop(tid, None)
                continue

            if not is_closed_verified and inside_wait:
                art_dir = art_root / tid
                evictable, _unr = artifact_retention.classify_task_artifact_bulk(art_dir, stop=stop)
                if evictable:
                    epoch = _task_epoch(t_row, now)
                    young[tid] = epoch + artifact_retention.SETTLED_ARTIFACT_GRACE_DAYS * 86400
                else:
                    young.pop(tid, None)
                waiting.discard(tid)
                continue

            if is_closed_verified and not _still_closed_and_verified(root_path, pid, verified_closed.get(pid)):
                fail_closed_skipped += 1
                waiting.add(tid)
                young.pop(tid, None)
                continue

            outcome = artifact_retention.prune_task_artifact_bulk(
                root_path,
                tid,
                live=live,
                reason="closed_project_retention" if is_closed_verified else "settled_7d_retention",
                grace_days=0 if is_closed_verified else artifact_retention.SETTLED_ARTIFACT_GRACE_DAYS,
                bulk_file_bytes=0 if is_closed_verified else artifact_retention.BULK_FILE_BYTES,
                stop=stop,
            )
            status = str(outcome.get("status") or "")
            reason = str(outcome.get("reason") or "")
            if status in ("pruned", "partial") and outcome.get("removed"):
                pruned_tasks += 1
                pruned_bytes += int(outcome.get("removed_bytes") or 0)
                if is_closed_verified:
                    closed_pruned_tasks += 1
                else:
                    settled_pruned_tasks += 1
            if stop():
                stopped = True
                waiting.add(tid)
                waiting.update(dir_candidates[idx + 1:])
                young.pop(tid, None)
                break
            if (status in ("kept", "partial") and reason in ("busy", "in_use", "other")) or int(outcome.get("failed") or 0) > 0:
                waiting.add(tid)
                young.pop(tid, None)
            elif reason == "inside_wait" and not is_closed_verified:
                epoch = _task_epoch(t_row, now)
                young[tid] = epoch + artifact_retention.SETTLED_ARTIFACT_GRACE_DAYS * 86400
                waiting.discard(tid)
            else:
                waiting.discard(tid)
                young.pop(tid, None)

    next_due = min(young.values()) if young else None
    state = "pending" if stopped else "done"
    waiting_sorted = sorted(waiting)
    fields: Dict[str, Any] = {
        "state": state,
        "waiting": waiting_sorted,
        "young": young,
        "next_due": next_due,
        "last": {
            "at": now,
            "pruned_tasks": pruned_tasks,
            "closed_pruned_tasks": closed_pruned_tasks,
            "settled_pruned_tasks": settled_pruned_tasks,
            "pruned_bytes": pruned_bytes,
            "fail_closed_skipped": fail_closed_skipped,
        },
    }
    if not stopped:
        fields["counter"] = counter
        fields["waiting_sig"] = _waiting_sig(root_path, waiting_sorted)
        fields["projects_sig"] = proj_sig
        fields["registry_unreadable"] = not registry_ok
        if closed_checked and registry_ok:
            fields["closed_verified"] = verified_closed
            fields["closed_unindexed"] = sorted(unindexed_closed)
    maintenance_jobs.record(root_path, JOB, **fields)
    if pruned_tasks:
        append_jsonl(
            root_path / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "artifact_retention",
                "pruned_tasks": pruned_tasks,
                "closed_pruned_tasks": closed_pruned_tasks,
                "settled_pruned_tasks": settled_pruned_tasks,
                "pruned_bytes": pruned_bytes,
                "fail_closed_skipped": fail_closed_skipped,
            },
        )
    return {"state": state, "items": pruned_tasks}
