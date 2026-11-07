"""Explicit assessment of retained activity; the task document is the single store.

Like knowledge authorship, this is the integrating mind's own attributed judgment,
including under Observe: it grants no task control or authority to repair a failure.
It is intentionally not an Observe world-mutation verb.
"""
from __future__ import annotations

import json
from ouroboros.tools.registry import ToolEntry


def _assess_activity(ctx, task_id: str, activity_key: str = "", source_hash: str = "",
                     reason: str = "", investigating_task_id: str = "", declared_error: bool = False) -> str:
    from ouroboros.activity_history import activity_sources, activity_receipts, record_assessment
    from ouroboros.task_results import resolve_ctx_lineage, validate_task_id
    from ouroboros.tool_access import canonical_data_root, active_tool_profile
    from ouroboros.tools.tool_result import ToolResult, _publish_tool_result
    from ouroboros.task_reads import TaskReads

    def refused(code, text, status="blocked"):
        return _publish_tool_result(ctx, ToolResult(status=status, code=code, text=f"{code}: {text}"))

    if active_tool_profile(ctx) == "local_readonly_subagent" or not resolve_ctx_lineage(ctx)["is_root_task"]:
        return refused("ACTIVITY_ASSESSMENT_REFUSED", "only an integrating root records an activity judgment")
    if not str(ctx.task_id or "").strip():
        return refused("ACTIVITY_ASSESSMENT_REFUSED", "an activity judgment needs an attributed task author")
    root = canonical_data_root(ctx)
    try:
        task_id = validate_task_id(task_id)
        if not activity_key:
            row = TaskReads(root).physical(task_id)
            if not row:
                return refused("ACTIVITY_NOT_FOUND", "task not found", "unavailable")
            gaps = set()
            receipts = activity_receipts(root, row, gaps)
            return json.dumps({"task_id": task_id, "entries": activity_sources(row, receipts),
                               "gaps": sorted(gaps)}, ensure_ascii=False)
        saved = record_assessment(root, task_id, activity_key, source_hash, reason,
                                  actor={"kind": "ouroboros", "task_id": str(ctx.task_id)},
                                  investigating_task_id=investigating_task_id, declared_error=declared_error)
        return json.dumps({"task_id": task_id, "activity_key": activity_key, "assessment": saved}, ensure_ascii=False)
    except (ValueError, LookupError) as exc:
        return refused("ACTIVITY_ASSESSMENT_REFUSED", str(exc))


def get_tools():
    return [ToolEntry("assess_activity", {
        "name": "assess_activity",
        "description": (
            "Record that you evaluated a particular retained failure after reading its evidence—when you "
            "diagnose it, decide it was expected, move beyond it deliberately, or assign investigation. "
            "First call with only task_id to inspect exact activity keys and source hashes; then supply "
            "activity_key, source_hash and reason to record your attributed judgment on those exact facts. "
            "Root hashes bind lifecycle/failure facts, not the full answer wording. "
            "Optionally link the investigating task. This reduces an error row's emphasis; it does NOT "
            "mean fixed, PASS or owner approval. Set declared_error=true only when your judgment identifies "
            "an actual error in an otherwise neutral process occurrence; nonzero exit/Stop alone is not one. "
            "Do not infer assessment from opening a row, prose, time, "
            "Continue or a new task. Changed facts require a fresh read/judgment. No commands or task "
            "control, no settings, no new work; unknown task/source writes nothing. Integrating roots may "
            "inspect and assess retained activity across rooms; the current room is not a scope fence."
        ),
        "parameters": {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"}, "activity_key": {"type": "string", "default": ""},
            "source_hash": {"type": "string", "default": ""},
            "reason": {"type": "string", "default": ""},
            "investigating_task_id": {"type": "string", "default": ""},
            "declared_error": {"type": "boolean", "default": False}}}},
        _assess_activity, safety_policy="skip")]
