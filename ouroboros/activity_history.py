"""Retained room activity and explicit source-bound judgments, over existing task records.

This view grants no controls or liveness. Works owns current state. A service exit
is a process fact, not a failed check; Stop never becomes an error by its code.
Assessments change emphasis, never the underlying outcome or whether it was fixed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def source_hash(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _membership_read(read, *args, **kwargs):
    """Strict resolver ValueErrors describe unavailable authority, not bad input."""
    from ouroboros.store import StoreError
    try:
        return read(*args, **kwargs)
    except ValueError as exc:
        raise StoreError("Activity room membership is unavailable") from exc


def merge_assessments(canonical: dict | None, replica: dict | None) -> dict:
    """The integrating writer owns judgments; stale execution replicas cannot undo them."""
    return {**(replica or {}), **(canonical or {})}


def activity_receipts(root: Any, row: dict, gaps: set[str]) -> list[dict]:
    """Use the real per-task receipt owner, including a still-live execution replica.

    No task or global log is scanned. Missing receipts are not invented from a
    task's failure flag or its bounded verification-ledger summary.
    """
    from ouroboros.outcome_receipt_store import read_verification_receipts_from_roots
    from ouroboros.task_status import _child_drive_candidates
    return read_verification_receipts_from_roots(
        [root, *_child_drive_candidates(row)], str(row.get("task_id") or ""), gap_reasons=gaps)


def activity_sources(row: dict, receipts: list | None = None) -> list[dict]:
    """One root and its retained exact-start services/checks; no inferred execution."""
    from ouroboros.service_observations import service_start_stamp
    from ouroboros.task_status import shown_outcome
    from ouroboros.project_dialogue import outcome_phase

    tid = str(row.get("task_id") or "")
    outcome = shown_outcome(row)
    phase = outcome_phase(row, {})
    root = {"key": f"task:{tid}", "kind": "task", "task_id": tid,
            "name": str(row.get("suggested_name") or row.get("title") or "Task"),
            "at": row.get("started_at") or row.get("ts"), "outcome": outcome,
            "error": phase == "error", "phase": phase,
            "source": {"task_id": tid, "task_attempt": row.get("task_attempt") or 1,
                       "status": outcome, "phase": phase,
                       "facts": {key: row.get(key) for key in (
                           "reason_code", "error", "failure", "outcome_axes", "artifact_status",
                           "completion_reason", "terminal_host_notice")}}}
    entries = [root]
    services = row.get("service_observations") or {}
    for service in services.get("services") or []:
        if not isinstance(service, dict):
            continue
        sid, started = service.get("service_id"), service_start_stamp(service.get("started_at"))
        if not sid or not started:
            continue  # caller reports incomplete retained observations, never a made-up start
        identity = {**{k: service.get(k) for k in ("service_id", "state", "returncode")}, "started_at": started}
        entries.append({"key": f"service:{tid}:{sid}:{started}", "kind": "service", "task_id": tid,
                        "name": str(service.get("name") or "Service"), "at": started,
                        "outcome": service.get("state") or "unknown", "error": False,
                        "source": identity, "service": {**service, "started_at": started}})
    # Each failed occurrence stays in history even after a later pass. Reconciled
    # verification health is a different question from whether this event was
    # evaluated: a pass must not silently assess an earlier failed occurrence.
    for receipt in receipts or []:
        if not isinstance(receipt, dict) or receipt.get("status") != "fail":
            continue
        digest = source_hash(receipt)
        entries.append({"key": f"check:{tid}:{digest}", "kind": "check", "task_id": tid,
                        "name": str(receipt.get("criterion_id") or "Verification check"),
                        "at": receipt.get("ts"), "outcome": "failed", "error": True,
                        "source": dict(receipt)})
    judgments = row.get("activity_assessments") or {}
    for entry in entries:
        entry["source_hash"] = source_hash(entry.pop("source"))
        judgment = judgments.get(entry["key"])
        if isinstance(judgment, dict) and judgment.get("source_hash") == entry["source_hash"]:
            entry["assessment"] = dict(judgment)
            if judgment.get("declared_error") is True:
                entry["error"] = True  # explicit judgment, never raw nonzero-exit inference
    return entries


def retained_activity(row: dict, receipts: list | None = None, receipt_gaps: set | None = None) -> dict:
    """Small history row: exact facts only; private prompts/output/custody stay at task detail."""
    from ouroboros.service_observations import service_start_stamp
    entries = activity_sources(row, receipts)
    services = (row.get("service_observations") or {}).get("services") or []
    gaps = (["Some service observations have no exact start identity"]
            if any(not isinstance(s, dict) or not s.get("service_id") or not service_start_stamp(s.get("started_at")) for s in services)
            else [])
    return {"task_id": str(row.get("task_id") or ""), "at": row.get("started_at") or row.get("ts"),
            "entries": entries, "gaps": gaps + sorted(receipt_gaps or [])}


def record_assessment(root: Any, task_id: str, activity_key: str, expected_source: str,
                      reason: str, *, actor: dict, investigating_task_id: str = "", declared_error: bool = False) -> dict:
    """Explicit judgment under the existing task transaction, with source CAS and attribution."""
    from ouroboros.task_results import validate_task_id, load_task_result
    from ouroboros.task_store import patch_document
    from ouroboros.task_status import effective_task_result, _child_drive_candidates
    from ouroboros.utils import utc_now_iso

    task_id = validate_task_id(task_id)
    if not reason.strip():
        raise ValueError("An assessment must say what was evaluated")
    if not actor.get("task_id") and actor.get("kind") != "owner":
        raise ValueError("An assessment needs an attributed author")
    if investigating_task_id:
        validate_task_id(investigating_task_id)
    saved = {}
    canonical = load_task_result(root, task_id, strict=True)
    if not canonical:
        raise LookupError("Task not found")
    # Inspect the SAME physical/replica occurrence as the public history reader.
    # All cross-drive reads happen before the canonical mutation transaction.
    observed = effective_task_result(root, canonical, materialize_artifacts=False,
                                    follow_successors=False, infer_orphans=False)
    replica_binding = list(_child_drive_candidates(canonical))
    # Receipt files have their own lock. Read outside the task transaction; the
    # immutable occurrence is the judged source, not a mutable 'latest red' slot.
    receipt_gaps = set()
    receipts = activity_receipts(root, observed, receipt_gaps)
    if activity_key.startswith("check:") and receipt_gaps:
        raise ValueError("Verification source is incomplete; read its evidence before assessing it")

    entry = next((e for e in activity_sources(observed, receipts) if e["key"] == activity_key), None)
    if entry is None or entry["source_hash"] != expected_source:
        raise ValueError("Activity changed; read its current source before assessing it")
    def canonical_source(row):
        return next((e["source_hash"] for e in activity_sources({**row, "task_id": task_id}, receipts) if e["key"] == activity_key), None)
    canonical_before = canonical_source(canonical)

    def mutate(current):
        if not current:
            raise LookupError("Task not found")
        if (list(_child_drive_candidates(current)) != replica_binding
                or canonical_source(current) not in {canonical_before, expected_source}):
            raise ValueError("Activity changed; read its current source before assessing it")
        judgment = {"state": "assessed", "source_hash": expected_source, "reason": reason,
                    "by": dict(actor), "at": utc_now_iso()}
        if declared_error:
            judgment["declared_error"] = True
        if investigating_task_id:
            judgment["investigating_task_id"] = investigating_task_id
        judgments = {**(current.get("activity_assessments") or {}), activity_key: judgment}
        saved.update(judgment)
        return {**current, "activity_assessments": judgments}

    patch_document(root, task_id, mutate)
    return saved


def room_activity_page(root: Any, room_id: int, *, limit: int = 30, cursor: str = "",
                       scan_budget: int = 256) -> dict:
    """Bounded indexed candidates, canonical room membership, and a last-SCANNED cursor.

    Sparse rooms can need another page even when this page has no matches. That
    boundary is visible, not evidence that the room has no older activity.
    """
    import base64
    import binascii
    from ouroboros.dialogue_provenance import presence_provenance_fields
    from ouroboros.projects_registry import RoomResolver, reserved_project_chat_ids
    from ouroboros.room_membership import room_membership
    from ouroboros.task_reads import TaskReads
    from ouroboros.task_store import root_ids_page
    from ouroboros.utils import utc_now_iso

    if room_id <= 0:
        raise ValueError("An activity page needs a visible room")
    def view():
        resolver = _membership_read(RoomResolver, root, strict=True)
        project_ids = set(_membership_read(reserved_project_chat_ids, root, strict=True))
        # Hash the already-read membership inputs, not racy file times. A new
        # process may continue a page when those exact inputs are unchanged.
        return resolver, project_ids, source_hash({"bindings": resolver.bindings, "rooms": sorted(project_ids)})

    rooms, project_ids, stamp = view()
    before = None
    if cursor:
        try:
            if len(cursor) > 4096:
                raise ValueError("cursor too large")
            value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if not isinstance(value, dict):
                raise ValueError("Invalid activity cursor")
            before = value["before"]
            if (value.get("v") != 1 or value["room"] != room_id or before is not None and (
                    not isinstance(before, list) or len(before) != 2
                    or before[0] is not None and not isinstance(before[0], str)
                    or not isinstance(before[1], str))):
                raise ValueError("cursor shape or room differs")
            if value.get("bindings") != stamp:
                raise ValueError("activity_view_changed")
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("Invalid activity cursor") from exc
    member = room_membership(room_id, project_ids, [], rooms)
    reads = TaskReads(root)
    matches, gaps, scanned, reached_start = [], [], 0, False
    last = before
    while len(matches) < limit and scanned < scan_budget:
        batch_size = min(64, scan_budget - scanned)
        candidates = root_ids_page(root, batch_size, before=last)
        if not candidates:
            reached_start = True
            break
        if not _membership_read(rooms.warm, (c["task_id"] for c in candidates)):
            gaps.append("Activity lineage could not be read; this window was not skipped")
            break
        presence_ids = [c["task_id"] for c in candidates if c["presence_authority_recorded"]]
        reads.prefetch(presence_ids, follow_successors=False)
        used = 0
        for candidate in candidates:
            tid = candidate["task_id"]
            scanned += 1
            used += 1
            last = [candidate["ts"], tid]
            chat = _membership_read(rooms.lineage, tid).get("chat_id")
            chat = 1 if chat is None else chat  # the existing history default; 0 remains hidden
            probe = {"task_id": tid, "root_task_id": tid, "chat_id": chat}
            if candidate["presence_authority_recorded"]:
                body = reads.load(root, tid)
                if body is None:
                    gaps.append(f"Presence provenance unavailable for {tid}; record omitted")
                    continue
                probe.update(presence_provenance_fields(body))
            if _membership_read(member, chat, probe):
                matches.append(tid)
                if len(matches) == limit:
                    break
        if gaps:
            break
        if used == len(candidates) and len(candidates) < batch_size:
            reached_start = True
            break
    reads.prefetch(matches, follow_successors=False)
    rows = []
    for tid in matches:
        row = reads.physical(tid)
        if row:
            receipt_gaps = set()
            receipts = activity_receipts(root, row, receipt_gaps)
            rows.append(retained_activity(row, receipts, receipt_gaps))
        else:
            gaps.append(f"Activity record unavailable for {tid}")
    _end_rooms, _end_ids, end_stamp = view()
    if stamp != end_stamp:
        raise ValueError("activity_view_changed")
    next_cursor = None
    if not reached_start:
        payload = {"v": 1, "room": room_id, "before": last, "bindings": stamp}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return {"room": room_id, "view": stamp, "tasks": rows, "next_cursor": next_cursor,
            "observed_at": utc_now_iso(), "gaps": gaps,
            "coverage": {"scanned": scanned, "reached_start": reached_start,
                         "bindings_stamped": bool(stamp and end_stamp), "candidate_budget": scan_budget}}
