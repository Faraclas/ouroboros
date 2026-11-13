"""Explicit maintenance Pause return through ordinary Resume, once per boot.

The existing owner fence owns intent and outcome; there is no second grant or
history sweep. Refusals stay held and reported. A later boot can reselect an
unconsumed grant through normal revocation; consuming it releases the fence.
"""
from __future__ import annotations

import pathlib
from ouroboros.control_actor import mind_control
from ouroboros.delegate_recovery import _PROCESS_GENERATION
from ouroboros.owner_pause import fence_closed, launch_lock, read_fence
from ouroboros.utils import utc_now_iso


def _independent_hold(task: dict, fence: dict) -> str:
    """Only a clean owner Pause is selected; Resume rechecks the other floors."""
    for key in ("_budget_pause_hold", "_owner_hold", "_project_admission_restore_hold",
                "_cancel_intent_authority_hold", "_terminalization_retry"):
        if task.get(key):
            return key
    pause = task.get("_budget_pause") or {}
    if pause and (pause.get("reason") != "owner" or
                  str(pause.get("checkpoint", {}).get("owner_fence_id") or
                      pause.get("owner_fence_id") or "") not in {"", str(fence.get("fence_id") or "")}):
        return "independent_pause"
    return ""


def pending_maintenance(task, root) -> bool:
    """Restore may omit its NEW app-stop hold; the closed owner fence still gates dispatch.

    Existing independent holds and unreadable authority always retain the normal
    holding path. A pausing/unsaved actor is not safe to return by analogy.
    """
    try:
        fence = read_fence(pathlib.Path(task.get("budget_drive_root") or root),
                           str(task.get("root_task_id") or task["id"]))
        return (not task.get("parent_task_id") and fence.get("state") == "paused"
                and (fence.get("maintenance_return") or {}).get("state") == "pending"
                and not _independent_hold(task, fence))
    except (OSError, ValueError, RuntimeError):
        return False


def grant_maintenance_resume(task, pause, external, fence_id):
    """Queue→launch serializes final intent validation and durable grant publication."""
    from supervisor import queue as q
    from supervisor.budget_resume import grant_exact_budget_resume
    from ouroboros.owner_pause import LaunchBusy
    source = pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT)
    root_id = str(task.get("root_task_id") or task["id"])
    try:
        with launch_lock(source, root_id, wait=False):
            fence = read_fence(source, root_id)
            if (fence.get("fence_id") != fence_id
                    or (fence.get("maintenance_return") or {}).get("state") != "pending"):
                return {"ok": False, "error": "selection_authority_changed"}
            if blocker := _independent_hold(task, fence):
                return {"ok": False, "error": blocker}
            return grant_exact_budget_resume(task, pause, external=external)
    except LaunchBusy:
        return {"ok": False, "error": "maintenance_selection_busy"}


def _return_one(root, task):
    from supervisor.queue_transitions import resume_budget_paused_task
    from ouroboros.task_store import patch_document
    tid = str(task["id"])
    root_id = str(task.get("root_task_id") or tid)
    source = pathlib.Path(task.get("budget_drive_root") or root)
    fence = read_fence(source, root_id)
    intent = fence.get("maintenance_return") or {}
    if (intent.get("state") != "pending" or not fence_closed(fence)
            or intent.get("generation") == _PROCESS_GENERATION
            or not intent.get("generation") or intent.get("observed_generation") == _PROCESS_GENERATION):
        return None
    blocker = _independent_hold(task, fence)
    if fence.get("state") != "paused":
        blocker = blocker or "pause_not_settled"
    if blocker:
        outcome = {"ok": False, "error": blocker}
    else:
        with mind_control(tid, "Returning explicitly maintenance-paused work after restart"):
            outcome = resume_budget_paused_task(tid, expected_owner_fence_id=str(fence["fence_id"]))
    from ouroboros.pause_notices import recovery_fields
    reason = {"_project_admission_restore_hold": "the original project identity needs verification",
                  "_budget_pause_hold": "an independent pause still applies",
                  "pause_not_settled": "the maintenance pause did not finish saving safely",
                  "owner_pause_effects_unsettled": "previous effects have not settled"}.get(
                      outcome.get("error"), str(outcome.get("error") or "unknown blocker").replace("_", " "))
    text = ("Maintenance recovery returned this work to the queue; execution has not yet been confirmed."
                if outcome.get("ok") else f"Maintenance recovery kept this work held: {reason}. Check its task controls.")
    if outcome.get("eligible_descendants") or outcome.get("held_descendants"):
        text += " Its child work remains paused pending individual selection."
    def annotate(current):
        actual = current.get("owner_pause") or {}
        notice = recovery_fields(current, root_id, f"{fence['fence_id']}:{_PROCESS_GENERATION}", text)
        if actual.get("fence_id") != fence["fence_id"] or actual.get("maintenance_return") != intent:
            return {**current, **notice}  # historical outcome, never overwrite a newer Pause
        # Field-only annotation: preserve the original pause/state timestamps.
        return {**current, "owner_pause": {**actual, "maintenance_return": {**intent,
                "observed_generation": _PROCESS_GENERATION, "observed_at": utc_now_iso(), "outcome": outcome}},
                **notice}
    try:
        with launch_lock(source, root_id):
            patch_document(source, root_id, annotate)
    except Exception as exc:
        # Resume has already answered: do not relabel an accepted selection as refused.
        outcome = {**outcome, "notice_error": str(exc)}
    return {"task_id": tid, "title": str(task.get("title") or task.get("suggested_name") or tid), **outcome}


def resume_maintenance_pauses(root) -> list[dict]:
    """After restore/custody/Panic handling, select roots; one damaged row cannot stop boot.

    Warm/direct pauses must first be retained by the existing shutdown/restore
    owners. New pauses in this process are deliberately not startup selections.
    Grants mean queued, never proof that a model has resumed. Children are not
    selected implicitly. Independent authority errors remain concrete outcomes.
    """
    from supervisor import queue as q
    from ouroboros.installation_stop import installation_panic
    from ouroboros.utils import append_jsonl
    root = pathlib.Path(root)
    if installation_panic(root):
        return []
    with q._queue_lock:
        candidates = [dict(t) for t in q.PENDING if not t.get("parent_task_id")]
    outcomes = []
    for task in candidates:
        try:
            outcome = _return_one(root, task)
        except Exception as exc:
            outcome = {"task_id": str(task["id"]), "title": str(task.get("title") or task["id"]),
                       "ok": False, "error": "maintenance_authority_unavailable", "detail": str(exc)}
            from ouroboros.pause_notices import recovery_fields
            from ouroboros.task_store import patch_document
            tid = str(task.get("root_task_id") or task["id"])
            try:
                patch_document(pathlib.Path(task.get("budget_drive_root") or root), tid,
                    lambda current: {**current, **recovery_fields(current, tid,
                        f"unavailable:{_PROCESS_GENERATION}",
                        "Recovery could not read this task's saved recovery authority. "
                        "No automatic return was confirmed; check its current task controls.")} if current else None)
            except Exception as notice_exc:
                outcome["notice_error"] = str(notice_exc)
        if outcome is not None:
            outcomes.append(outcome)
    if outcomes:
        append_jsonl(root / "logs" / "supervisor.jsonl", {
            "ts": utc_now_iso(), "type": "maintenance_return", "outcomes": outcomes})
    return outcomes
