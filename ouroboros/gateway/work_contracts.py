"""``/api/state`` work projection contracts (v1): one row per work, one rollup per room.

Descriptive, not runtime validation. The browser twin is ``web/modules/work_types.js``; the frozen census
(``active_chat_activities``) stays beside these fields unchanged (docs/architecture/11-frozen-contracts-v1.md).
"""
from __future__ import annotations

from typing import Any, Dict, List, Union

try:  # Python 3.11+
    from typing import NotRequired, TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import NotRequired, TypedDict  # type: ignore[assignment]


class WorkDependency(TypedDict):
    """Authored operation context, never executable prerequisites or satisfaction."""
    waiting_for: str
    why: str
    release: str
    on: List[str]


class WorkTarget(TypedDict):
    task_id: str
    name: str
    state: str  # live | ended | unknown; ended does not satisfy the release
    selected_as_wake: bool
    work_id: NotRequired[str]
    room: NotRequired[Any]
    project_id: NotRequired[str]
    activity: NotRequired[str]


class WorkReason(TypedDict):
    """One typed reason: ``code`` is closed per release, ``text`` its English sentence (unknown codes stay raw)."""

    code: str
    text: str
    count: NotRequired[int]
    ids: NotRequired[List[str]]
    since: NotRequired[Any]
    observed_at: NotRequired[Any]  # chooser observation, not the beginning of a wait
    by: NotRequired["PauseAuthor"]
    wake: NotRequired["WakeSources"]
    dependency: NotRequired[WorkDependency]
    targets: NotRequired[List[WorkTarget]]


class WakeService(TypedDict):
    service_id: str
    name: str
    started_at: Any  # UTC ISO or null; exact selected start, not a process identity


class WakeSources(TypedDict):
    """OR triggers, not semantic prerequisites/ETA; owner/control/question wakes are independent."""
    mode: str
    any_mail: bool
    senders: List[str]
    tasks: List[str]
    runs: List[str]
    services: List[WakeService]
    at: Any  # UTC ISO or null


class PauseAuthor(TypedDict):
    """The declared author of a decided Pause (``control_actor.pause_author``): ``owner`` or ``ouroboros``."""

    actor: str
    text: str
    at: Any


class WorkTail(TypedDict):
    """What still runs beside or after the current episode, said as a second fact, never as the work's word."""

    kind: str  # helper | run | review | model | service
    state: str  # live | unknown | ended (service only)
    code: str
    text: str
    count: NotRequired[int]
    ids: NotRequired[List[str]]


class ServiceWorkTail(WorkTail):
    """One original service start; unknown output metadata does not change liveness."""
    task_id: str
    service_id: str
    name: str
    started_at: Any  # UTC ISO or null
    observed_at: Any  # UTC ISO or null, dated per service probe
    returncode: Any  # integer or null (Docker never invents one)
    output_bytes: Any  # integer or null
    output_updated_at: Any  # UTC ISO or null; no progress/ETA inference


class WorkTurn(TypedDict):
    """What waits for the owner: an unanswered question or an action only the owner takes."""

    kind: str
    task_id: str
    quiz_id: NotRequired[str]
    wait_id: NotRequired[str]
    code: NotRequired[str]


class WorkControl(TypedDict):
    """An action the named door accepts now, on its exact target."""

    action: str
    target: str
    scope: NotRequired[str]


class WorkEpisode(TypedDict):
    task_id: str
    live: bool


class WorkMembers(TypedDict):
    ids: List[str]
    states: NotRequired[Dict[str, Dict[str, Any]]]


class WorkFocus(TypedDict):
    text: str
    at: Any


class Work(TypedDict):
    """One request with its Continue episodes and live members."""

    work_id: str
    room: Any
    name: str
    kind: str
    project_id: str
    episode: WorkEpisode
    activity: str
    transition: str
    call: str
    because: List[WorkReason]
    tails: List[Union[WorkTail, ServiceWorkTail]]
    live: bool
    turn: List[WorkTurn]
    release: str
    members: WorkMembers
    controls: List[WorkControl]
    since: Any
    confirmed: bool
    mark: str
    stop: NotRequired[Dict[str, Any]]
    refusal: NotRequired[Dict[str, Any]]
    outcome: NotRequired[str]
    focus: NotRequired[WorkFocus]
    gaps: NotRequired[List[Dict[str, Any]]]
    client_message_id: NotRequired[str]


class RoomStatus(TypedDict):
    """One room's status from its works: the most active work speaks; ``amber`` while something waits for the owner."""

    activity: str
    transition: str
    call: str
    turn: List[Dict[str, Any]]
    amber: bool
    mark: str
    confirmed: bool
    works: List[str]
    counts: NotRequired[Dict[str, int]]
    running_services: NotRequired[int]


class WorkSummary(TypedDict):
    counts: Dict[str, int]
    turn: int
    confirmed: bool


class WorkStateFields(TypedDict):
    """The work projection's ``/api/state`` fields, inherited by ``contracts.StateResponse``."""

    works: List[Work]
    rooms: Dict[str, RoomStatus]
    work_summary: WorkSummary
    works_complete: bool
    works_gaps: List[Dict[str, Any]]
