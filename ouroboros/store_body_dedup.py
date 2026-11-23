"""Lossless write-seam deduplication and resumable compaction for ``task_bodies``.

Task result documents carry two exact intra-row copies that double stored JSON
size on long-lived installs:

1. ``loop_outcome.trace_refs`` duplicates top-level ``trace_refs``.
2. ``artifact_bundle.artifacts`` is a pure field projection of top-level
   ``artifacts`` (with occasional per-item status overrides when an artifact
   record lacked an explicit ``status``).

``dedup`` replaces only those provably identical copies with compact sentinels
(``{"$dedup": ...}``) while keeping top-level ``trace_refs``, top-level
``artifacts``, ``artifact_bundle.status``, and ``loop_outcome.usage`` untouched
so SQL projections (``task_store.display_documents``) and direct top-level
extracts work identically. ``expand`` reconstructs the exact original dict
(key order included) at the ``store.materialize`` and ``task_store._documents``
read seams. ``compact_task_bodies`` (and the once-per-install ``body_compaction``
maintenance job) compacts pre-existing rows in bounded batches using exact
content CAS without advancing ``tasks.write_seq``.
"""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ouroboros.utils import machine_json

DEDUP_KEY = "$dedup"
COMPACTION_VERSION = 1
JOB = "body_compaction"
_BUNDLE_KEYS = frozenset({"kind", "name", "path", "size", "sha256", "status", "errors"})
_BUNDLE_KEYS_REL = frozenset((*_BUNDLE_KEYS, "relpath"))


def _pure_bundle_item(item: Mapping[str, Any], *, status_override: Optional[str] = None) -> Dict[str, Any]:
    path = str(item.get("path") or "")
    if status_override is not None:
        status = status_override
    elif item.get("copy_status") == "failed":
        status = "missing"
    else:
        status = str(item.get("status") or "").strip()
    errors = (list(item.get("errors") or []) if isinstance(item.get("errors"), list) else []) + (
        [str(item["copy_error"])] if item.get("copy_error") else []
    )
    raw_size = item.get("size")
    if isinstance(raw_size, bool):
        raise TypeError("boolean artifact size")
    size = int(raw_size or 0)
    record: Dict[str, Any] = {
        "kind": str(item.get("kind") or ""),
        "name": str(item.get("name") or pathlib.Path(path).name),
        "path": path,
        "size": size,
        "sha256": str(item.get("sha256") or ""),
        "status": status,
        "errors": errors,
    }
    if item.get("relpath"):
        record["relpath"] = str(item["relpath"])
    return record


def _dedup_bundle_artifacts(
    artifacts: List[Any], bundle_artifacts: List[Any],
) -> Optional[Dict[str, Any]]:
    if not bundle_artifacts:
        return None
    dict_items = [item for item in artifacts if isinstance(item, dict)]
    if len(dict_items) != len(bundle_artifacts):
        return None
    status_overrides: Dict[str, str] = {}
    for idx, (src, dst) in enumerate(zip(dict_items, bundle_artifacts)):
        if not isinstance(dst, dict):
            return None
        dst_keys = set(dst.keys())
        expected_keys = _BUNDLE_KEYS_REL if src.get("relpath") else _BUNDLE_KEYS
        if dst_keys != expected_keys:
            return None
        if not isinstance(dst.get("status"), str):
            return None
        try:
            projected = _pure_bundle_item(src)
        except (TypeError, ValueError, OverflowError):
            return None
        if projected["status"] != dst["status"]:
            status_overrides[str(idx)] = dst["status"]
            projected["status"] = dst["status"]
        if machine_json(projected) != machine_json(dst):
            return None
    sentinel: Dict[str, Any] = {DEDUP_KEY: "artifacts"}
    if status_overrides:
        sentinel["status_overrides"] = status_overrides
    return sentinel


def _expand_bundle_artifacts(
    artifacts: List[Any], sentinel: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    overrides = sentinel.get("status_overrides") if isinstance(sentinel.get("status_overrides"), dict) else {}
    records: List[Dict[str, Any]] = []
    idx = 0
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        override = overrides.get(str(idx))
        records.append(_pure_bundle_item(item, status_override=override if isinstance(override, str) else None))
        idx += 1
    return records


def dedup(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of DOCUMENT with exact intra-row duplicates replaced by sentinels."""
    if not isinstance(document, Mapping):
        return document  # type: ignore[return-value]
    out: Optional[Dict[str, Any]] = None

    lo = document.get("loop_outcome")
    if (
        isinstance(lo, dict)
        and "trace_refs" in lo
        and "trace_refs" in document
        and not (isinstance(lo["trace_refs"], dict) and lo["trace_refs"].get(DEDUP_KEY) == "trace_refs")
    ):
        lo_json = machine_json(lo["trace_refs"])
        if lo_json == machine_json(document["trace_refs"]) and len(lo_json) > len(machine_json({DEDUP_KEY: "trace_refs"})):
            out = dict(document)
            out["loop_outcome"] = {
                k: ({DEDUP_KEY: "trace_refs"} if k == "trace_refs" else v) for k, v in lo.items()
            }

    ab = document.get("artifact_bundle")
    arts = document.get("artifacts")
    if isinstance(ab, dict) and isinstance(ab.get("artifacts"), list) and isinstance(arts, list):
        sentinel = _dedup_bundle_artifacts(arts, ab["artifacts"])
        if sentinel is not None and len(machine_json(ab["artifacts"])) > len(machine_json(sentinel)):
            if out is None:
                out = dict(document)
            out["artifact_bundle"] = {
                k: (sentinel if k == "artifacts" else v) for k, v in ab.items()
            }

    return dict(document) if out is None else out


def expand(document: Any) -> Any:
    """Restore any write-seam dedup sentinels back to the exact logical document."""
    if not isinstance(document, dict):
        return document

    lo = document.get("loop_outcome")
    if (
        isinstance(lo, dict)
        and isinstance(lo.get("trace_refs"), dict)
        and lo["trace_refs"] == {DEDUP_KEY: "trace_refs"}
        and "trace_refs" in document
    ):
        document["loop_outcome"] = {
            k: (copy.deepcopy(document["trace_refs"]) if k == "trace_refs" else v) for k, v in lo.items()
        }

    ab = document.get("artifact_bundle")
    arts = document.get("artifacts")
    if (
        isinstance(ab, dict)
        and isinstance(ab.get("artifacts"), dict)
        and ab["artifacts"].get(DEDUP_KEY) == "artifacts"
        and isinstance(arts, list)
    ):
        restored = _expand_bundle_artifacts(arts, ab["artifacts"])
        document["artifact_bundle"] = {
            k: (restored if k == "artifacts" else v) for k, v in ab.items()
        }

    return document


def due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """One-shot per-install backfill of pre-dedup task_bodies; rests with zero DB queries once done.

    An empty store has no legacy rows to compact (O(1) PK existence probe).
    """
    if row.get("state") == "done" and int(row.get("version") or 0) >= COMPACTION_VERSION:
        return False
    if row.get("state") == "failed":
        return child
    from ouroboros import store

    try:
        with store.open_store(root, write=False) as txn:
            return txn.conn.execute("SELECT 1 FROM task_bodies LIMIT 1").fetchone() is not None
    except store.StoreError:
        return False


def compact_task_bodies(
    root: Any,
    *,
    batch_size: int = 64,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Compact existing task_bodies rows in bounded batches with exact content CAS.

    Verifies lossless round-trip (``expand(dedup(doc)) == doc``) for every row
    before writing, never changes ``tasks.write_seq`` (the logical document is
    unchanged), and persists a resumable cursor in ``meta["job:body_compaction"]``.
    """
    from ouroboros import maintenance_jobs, store

    stop = should_stop or (lambda: False)
    root_path = pathlib.Path(root)
    existing = maintenance_jobs.job(root_path, JOB) or {}
    if existing.get("state") == "done" and int(existing.get("version") or 0) >= COMPACTION_VERSION:
        return dict(existing)

    same_run = existing.get("state") == "running" and int(existing.get("version") or 0) == COMPACTION_VERSION
    cursor = str(existing.get("cursor") or "") if same_run else ""
    checked = int(existing.get("checked") or 0) if same_run else 0
    compacted = int(existing.get("compacted") or 0) if same_run else 0
    bytes_saved = int(existing.get("bytes_saved") or 0) if same_run else 0
    limit = max(1, int(batch_size))

    while not stop():
        with store.open_store(root_path, write=False) as txn:
            rows = txn.conn.execute(
                "SELECT task_id, body FROM task_bodies WHERE task_id > ? ORDER BY task_id LIMIT ?",
                (cursor, limit),
            ).fetchall()
        if not rows:
            return maintenance_jobs.record(
                root_path,
                JOB,
                state="done",
                version=COMPACTION_VERSION,
                cursor="",
                checked=checked,
                compacted=compacted,
                bytes_saved=bytes_saved,
            )

        updates: List[Tuple[str, str, str, int]] = []
        for record in rows:
            if stop():
                break
            tid = str(record["task_id"])
            raw = record["body"]
            cursor = tid
            checked += 1
            if not isinstance(raw, str):
                continue
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    continue
                deduped = dedup(parsed)
                new_raw = machine_json(deduped)
                if len(new_raw) >= len(raw):
                    continue
                # Byte-exact round-trip proof (key order + scalar types) before touching the store.
                if machine_json(expand(json.loads(new_raw))) != machine_json(parsed):
                    continue
                saved = len(raw.encode("utf-8")) - len(new_raw.encode("utf-8"))
            except Exception:
                continue
            updates.append((new_raw, tid, raw, saved))

        if updates:
            with store.open_store(root_path, write=True) as txn:
                for new_raw, tid, old_raw, saved in updates:
                    if txn.conn.execute(
                        "UPDATE task_bodies SET body=? WHERE task_id=? AND body=?",
                        (new_raw, tid, old_raw),
                    ).rowcount:
                        compacted += 1
                        bytes_saved += saved
                maintenance_jobs.record_in(
                    txn,
                    JOB,
                    state="running",
                    version=COMPACTION_VERSION,
                    cursor=cursor,
                    checked=checked,
                    compacted=compacted,
                    bytes_saved=bytes_saved,
                )
        else:
            maintenance_jobs.record(
                root_path,
                JOB,
                state="running",
                version=COMPACTION_VERSION,
                cursor=cursor,
                checked=checked,
                compacted=compacted,
                bytes_saved=bytes_saved,
            )

    return maintenance_jobs.job(root_path, JOB) or {
        "state": "running",
        "version": COMPACTION_VERSION,
        "cursor": cursor,
        "checked": checked,
        "compacted": compacted,
        "bytes_saved": bytes_saved,
    }


def expand_task_bodies(root: Any, *, batch_size: int = 64) -> Dict[str, Any]:
    """Restore any deduplicated task_bodies rows back to full undeduplicated JSON (pre-dedup rollback helper)."""
    from ouroboros import store

    root_path = pathlib.Path(root)
    cursor, checked, expanded = "", 0, 0
    limit = max(1, int(batch_size))
    while True:
        with store.open_store(root_path, write=False) as txn:
            rows = txn.conn.execute(
                "SELECT task_id, body FROM task_bodies WHERE task_id > ? ORDER BY task_id LIMIT ?",
                (cursor, limit),
            ).fetchall()
        if not rows:
            return {"checked": checked, "expanded": expanded}
        updates: List[Tuple[str, str, str]] = []
        for record in rows:
            tid, raw = str(record["task_id"]), record["body"]
            cursor = tid
            checked += 1
            if not isinstance(raw, str) or DEDUP_KEY not in raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            restored = expand(parsed)
            new_raw = machine_json(restored)
            if new_raw != raw:
                updates.append((new_raw, tid, raw))
        if updates:
            with store.open_store(root_path, write=True) as txn:
                for new_raw, tid, old_raw in updates:
                    expanded += txn.conn.execute(
                        "UPDATE task_bodies SET body=? WHERE task_id=? AND body=?",
                        (new_raw, tid, old_raw),
                    ).rowcount


def run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    row = compact_task_bodies(root, should_stop=should_stop) or {}
    return {
        "state": str(row.get("state") or "unknown"),
        "items": int(row.get("compacted") or 0),
        "bytes_saved": int(row.get("bytes_saved") or 0),
    }


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Compact or expand deduplicated task_bodies in current.sqlite.")
    parser.add_argument("data_root", help="Data root (or child drive root) containing state/current.sqlite")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--expand", action="store_true", help="Restore deduplicated rows before pre-dedup rollback (stop server first)")
    group.add_argument("--compact", action="store_true", help="Compact legacy undeduplicated task_bodies rows")
    args = parser.parse_args()
    result = expand_task_bodies(args.data_root) if args.expand else compact_task_bodies(args.data_root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
