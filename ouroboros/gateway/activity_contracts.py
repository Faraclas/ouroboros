"""Retained room activity: browser twin ``activity_types.js``."""
from typing import Any, Dict, List
try:
    from typing import TypedDict, NotRequired
except ImportError:
    from typing_extensions import TypedDict, NotRequired


class ActivityAssessment(TypedDict):
    state: str
    source_hash: str
    reason: str
    by: Dict[str, str]
    at: str
    investigating_task_id: NotRequired[str]
    declared_error: NotRequired[bool]


class ActivityEntry(TypedDict):
    key: str
    kind: str
    task_id: str
    name: str
    at: Any
    outcome: str
    error: bool
    source_hash: str
    phase: NotRequired[str]
    service: NotRequired[Dict[str, Any]]
    assessment: NotRequired[ActivityAssessment]


class ActivityTask(TypedDict):
    task_id: str
    at: Any
    entries: List[ActivityEntry]
    gaps: List[str]


class ActivityCoverage(TypedDict):
    scanned: int
    reached_start: bool
    bindings_stamped: bool
    candidate_budget: int


class ActivityPageResponse(TypedDict):
    room: int
    view: str
    tasks: List[ActivityTask]
    next_cursor: Any
    observed_at: str
    gaps: List[str]
    coverage: ActivityCoverage
