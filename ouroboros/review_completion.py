"""Episode-local review completion protocol, independent of provider stop markers.

Submission selects the author's exact answer, not its validity. Surface parsers
and extraction still judge that answer downstream. Strict whole-answer legacy
JSON remains compatible; commentary and report prose do not prove completion.
"""

from __future__ import annotations

import json
from typing import Any

from ouroboros.review_verdict_extraction import _canonical_payload_text, _strictly_parseable
from ouroboros.triad_review import _whole_json

SUBMIT_REVIEW = "submit_review"
SUBMIT_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": SUBMIT_REVIEW,
        "description": (
            "Finish this review episode by submitting your exact final deliverable. "
            "Put the task's complete output-contract text in answer, not surrounding "
            "commentary. Call this alone, without inspection or other tool calls. "
            "Submission ends the episode; it does not certify validity or approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}
COMPLETION_REMINDER = (
    "This review episode is still open. Your previous text is retained as commentary, "
    "not a submitted answer. Continue inspecting if needed, or finish by calling "
    "submit_review alone with your exact complete deliverable in its answer string. "
    "The task's output contract applies to that string; submission does not imply a "
    "clean verdict."
)


def is_review_submission(call: Any) -> bool:
    return (isinstance(call, dict) and isinstance(call.get("function"), dict)
            and call["function"].get("name") == SUBMIT_REVIEW)


def review_submission(tool_calls: list, validation_by_id: dict) -> tuple[str | None, str]:
    """Return a sole valid submission's exact string, or a call-local refusal.

    Other calls in a mixed batch remain executable; no result is silently dropped.
    A malformed verdict, refusal or empty string is still an authored submission.
    """
    submissions = [call for call in tool_calls if is_review_submission(call)]
    if not submissions:
        return None, ""
    if len(tool_calls) != 1:
        return None, "submit_review must be the sole tool call; continue reading or submit in a separate round"
    call = submissions[0]
    if not isinstance(call.get("id"), str) or not call["id"].strip() or call.get("type", "function") != "function":
        return None, "submit_review requires a function-call envelope with a nonempty id"
    validation = validation_by_id.get(call["id"])
    if validation is not None and not getattr(validation, "allows_execution", True):
        return None, str(getattr(validation, "error", "invalid arguments"))
    try:
        raw = call["function"].get("arguments")
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        args = None
    if not isinstance(args, dict) or set(args) != {"answer"} or not isinstance(args["answer"], str):
        return None, "submit_review arguments must be exactly an object containing answer:string"
    return args["answer"], ""


def legacy_review_answer(text: str, shape: str) -> bool:
    """Only a whole structured answer can finish without explicit submission."""
    if shape == "report":
        return False
    payload = _whole_json(text)
    if shape == "plan_reply":
        from ouroboros.plan_record import _REPLY_KEYS, parse_reply

        return (isinstance(payload, dict) and any(key in payload for key in _REPLY_KEYS)
                and parse_reply(json.dumps(payload, ensure_ascii=False))[0] is not None)
    if shape == "array" and isinstance(payload, dict) and _canonical_payload_text(payload, shape) is not None:
        return True
    return (_strictly_parseable(text, shape) or
            (payload is not None and _strictly_parseable(json.dumps(payload, ensure_ascii=False), shape)))
