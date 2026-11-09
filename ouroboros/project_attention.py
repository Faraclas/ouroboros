"""Project-idle opportunities, not work selection or another alarm.

The host feeds current occupancy and actual lifecycle witnesses. Unknown occupancy
never produces an opportunity. Admission consumes one episode; its own attention
turn is occupancy, but not a new ordinary-work witness. A later ordinary task,
including work the attention chose, can create the next opportunity.
"""
from __future__ import annotations

from typing import Any, Mapping


def observe_project(
    previous: Mapping[str, Any], *, ordinary_ids: set[str], occupied: bool,
    complete: bool, witness_id: str = "",
) -> dict[str, Any]:
    """Fold one current observation; source IDs are only the current membership.

    ``witness_id`` retains an admission that began and ended between observations.
    Release notifications invalidate the observation; they never mint witnesses.
    Missing/unknown observations preserve the earlier episode without certifying
    idleness. Pending episodes are cancelled by new ordinary work, not by their
    own attention turn. The launch owner must recheck occupancy before admission.
    """
    result = dict(previous)
    new_witness = bool(witness_id and witness_id != previous.get("witness_id"))
    if new_witness:
        result.update(witness_id=witness_id, eligible=True)
        result.pop("pending", None)
    if not complete:
        return result
    result["ordinary_ids"] = sorted(ordinary_ids)
    if not occupied and result.get("eligible"):
        result["pending"] = True
    return result


def admitted_project(previous: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    """Bind before generation starts: admission is not successful work selection."""
    if not task_id:
        raise ValueError("project attention admission requires its actual task ID")
    result = dict(previous)
    result.update(eligible=False, pending=False, attention_task_id=task_id)
    return result


EPISODES = "project_attention"


def consideration(task: Mapping[str, Any]) -> bool:
    """Research descendants keep this cause; independently started work does not."""
    metadata = {**(task.get("origin_metadata") or {}), **(task.get("metadata") or {})}
    return metadata.get("consciousness_cause", task.get("consciousness_cause")) == "project_idle"


def record_admission(txn: Any, task: Mapping[str, Any]) -> None:
    """Called by the successful admission owner in its existing transaction.

    Never called by task storage/import. The witness is the admitted task identity,
    not a commit counter, clock or first write. Consideration cannot arm itself.
    """
    project_id = str(task.get("project_id") or (task.get("metadata") or {}).get("project_id") or "")
    task_id = str(task.get("task_id") or task.get("id") or "")
    if not project_id or not task_id or consideration(task):
        return
    previous = txn.get_row(EPISODES, project_id) or {}
    witness = f"{task_id}:{task.get('task_attempt') or task.get('_attempt') or 1}"
    if previous.get("admission_witness_id") != witness:
        txn.put_row(EPISODES, project_id, {**previous, "admission_witness_id": witness,
                                        "eligible": True, "pending": False})


def consume(txn: Any, project_id: str, task_id: str) -> None:
    """Consume before generation; a definite no-body refusal restores this consume."""
    previous = txn.get_row(EPISODES, project_id) or {}
    txn.put_row(EPISODES, project_id, admitted_project(previous, task_id))


def current_owners(root: Any, queue_tasks: list[dict], direct: list[Any]) -> tuple[dict[str, dict], list[dict]]:
    """Current custody, not the display census or historical task tree.

    Returned task IDs are mapped through the host room resolver by the caller.
    Read failure propagates: an unavailable current set is never empty.
    """
    import json
    from ouroboros import store, store_hot_facts, task_store, usage_store
    from ouroboros.delegate_custody_current import snapshot
    from ouroboros.local_custody_repair import ended_unbound_model_request
    from ouroboros.owned_shutdown import owned_records
    from ouroboros.post_task_checkpoint import (
        POST_TASK_SYNTHESIS_INFLIGHT, POST_TASK_SYNTHESIS_LOCK, late_phase_state,
    )
    from ouroboros.process_custody import owned_process_state
    from ouroboros.tool_custody import claim_still_owns_effect
    import pathlib

    owners, gaps = {}, []

    def add(task_id: Any, facts: dict | None = None) -> None:
        task_id = str(task_id or "")
        if not task_id:
            gaps.append({"reason": "current_owner_identity_missing"})
            return
        if task_id not in owners:
            value = facts if facts is not None else task_store.read_document(root, task_id)
            if value is None:
                gaps.append({"reason": "current_owner_source_missing", "task_id": task_id})
            owners[task_id] = value or {}

    for task in queue_tasks:
        add(task.get("id"), task)
    for entry in direct:
        add(entry.activity_id, {"id": entry.activity_id, "chat_id": entry.chat_id,
                               "project_id": entry.project_id,
                               "metadata": {"consciousness_cause": entry.consciousness_cause}})
    custody = snapshot(root, closed=False)
    for pending in custody["pending"]:
        add(pending.get("task_id") or pending.get("root_task_id"))
    for entry in custody["state"].values():
        if not entry.terminal_state:
            add(entry.task_id or entry.root_task_id)
    with usage_store.read(root) as txn:
        attempts = usage_store.decode_attempts(txn.open_attempt_rows())
    for attempt in attempts:
        task_id = str(attempt.get("task_id") or attempt.get("root_task_id") or "")
        if not task_id or ended_unbound_model_request(attempt):
            continue
        if attempt.get("state") not in {"reserved", "dispatched", "unresolved"}:
            continue
        row = owners.get(task_id) or task_store.read_document(root, task_id)
        if row is None:
            add(task_id)
            continue
        consumer = attempt.get("local_answer_consumer_id")
        retired = (row.get("retired_model_consumers") or {}).get(consumer) if consumer else None
        if (isinstance(retired, dict) and retired.get("retired_at")
                and type(attempt.get("local_answer_task_attempt")) is int
                and retired.get("task_attempt") == attempt["local_answer_task_attempt"]):
            continue  # money may remain unresolved after its receiver ended
        add(task_id, row)
    with store.open_store(root, write=False) as txn:
        claims = list(txn.conn.execute(
            "SELECT task_id, facts FROM task_handoffs "
            f"WHERE {store_hot_facts.LAUNCH_CURRENT}"))
        phases = [row[0] for row in txn.conn.execute(
            "SELECT task_id FROM tasks INDEXED BY tasks_phase WHERE post_task_synthesis<>'' "
            "AND post_task_synthesis IN ('pending_once','running')")]
    for task_id, text in claims:
        claim = json.loads(text)
        if not isinstance(claim, dict):
            raise ValueError("tool claim is not an object")
        if claim.get("state") not in {"returned", "owner_dead"} or claim_still_owns_effect(root, claim):
            add(task_id)
    for task_id in phases:
        if late_phase_state(root, task_id):
            add(task_id)
    path = str(pathlib.Path(root).resolve(strict=False))
    with POST_TASK_SYNTHESIS_LOCK:
        upkeep = [tid for source, tid in POST_TASK_SYNTHESIS_INFLIGHT if source == path]
    for task_id in upkeep:
        add(task_id)
    for entry in owned_records(root, strict=True):
        task_id = entry.get("owner_task") or entry.get("task_id")
        if task_id and owned_process_state(entry, probe_backend=False) != "ended":
            add(task_id)
    return owners, gaps


# Admission callers may hold Q; they publish no IO there. The supervisor folds
# these exact successful admissions into its existing store off the event drain.
# A crash before that fold can lose a short episode, never manufacture one.
import threading
import pathlib
import logging

log = logging.getLogger(__name__)
_ADMISSIONS: dict[str, dict[str, dict]] = {}
_ADMISSION_LOCK = threading.Lock()


def note_admission(root: Any, task: dict) -> None:
    project_id = str(task.get("project_id") or (task.get("metadata") or {}).get("project_id") or "")
    if not project_id or consideration(task):
        return
    with _ADMISSION_LOCK:
        _ADMISSIONS.setdefault(str(pathlib.Path(root).resolve()), {})[project_id] = {
            "id": task.get("id"), "project_id": project_id, "_attempt": task.get("_attempt") or 1,
        }


def flush_admissions(root: Any) -> None:
    from ouroboros import store
    key = str(pathlib.Path(root).resolve())
    with _ADMISSION_LOCK:
        pending = dict(_ADMISSIONS.get(key) or {})
    if not pending:
        return
    with store.open_store(root, write=True) as txn:
        for task in pending.values():
            record_admission(txn, task)
    with _ADMISSION_LOCK:
        current = _ADMISSIONS.get(key, {})
        for project_id, task in pending.items():
            if current.get(project_id) == task:
                current.pop(project_id)


class ProjectAttention:
    """One coalesced supervisor duty; no timer or execution controller per project.

    Current store revisions invalidate, never arm an episode. Current owned
    handles are sampled on the existing supervisor pass so natural service exit
    needs no unrelated write. Expensive current-custody reads run only on change.
    """
    def __init__(self, root: Any, clock: Any):
        self.root, self.clock = pathlib.Path(root), clock
        self._lock = threading.Lock()
        self._signature = None
        self._retry_at = 0.0
        self._observation_error = None
        self._processes = []
        self.last_result = {}
        self._startup_pending = True

    def tick(self, *, ready: bool, pending_inputs: Any) -> None:
        if not ready or not isinstance(pending_inputs, (set, frozenset)) or pending_inputs or not self._lock.acquire(blocking=False):
            return
        try:
            threading.Thread(target=self._pass, name="project-attention", daemon=True).start()
        except BaseException:
            self._lock.release()
            raise

    def _pass(self) -> None:
        signature = None
        try:
            from ouroboros import store
            from ouroboros.process_custody import owned_process_state
            from supervisor import queue, workers
            from supervisor.active_activity import get_direct_activity_registry
            from ouroboros.server_restart import agent_restart_pending
            if agent_restart_pending():
                return
            flush_admissions(self.root)
            with store.open_store(self.root, write=False, wait_sec=0) as txn:
                revision = txn.revision()
            with queue._queue_lock:
                tasks = [dict(t) for t in queue.PENDING] + [dict(m.get("task") or {}) for m in queue.RUNNING.values()]
            registry = get_direct_activity_registry()
            direct = registry.actors()
            from supervisor.workspace_preparation import _LIVE, _STATE_LOCK
            from supervisor.events_coop_checkpoint import _COOP_CHECKPOINT_INFLIGHT, _COOP_CHECKPOINT_LOCK
            with _STATE_LOCK:
                preparations = tuple(sorted(_LIVE))
            with _COOP_CHECKPOINT_LOCK:
                checkpoints = tuple(sorted(_COOP_CHECKPOINT_INFLIGHT))
            with queue._queue_lock:
                reservations = tuple(sorted(queue.ADMISSION_RESERVATIONS))
            preparing = tuple(sorted(e.execution_id for e in registry.executions() if not e.task_id))
            from ouroboros.post_task_checkpoint import POST_TASK_SYNTHESIS_INFLIGHT, POST_TASK_SYNTHESIS_LOCK
            with POST_TASK_SYNTHESIS_LOCK:
                upkeep = tuple(sorted(tid for source, tid in POST_TASK_SYNTHESIS_INFLIGHT if source == str(self.root.resolve())))
            from ouroboros.consciousness import panic_blocks_wake
            control = self.clock._read_state()
            allowance = self.clock._allowance_view(__import__('time').time())
            from ouroboros import usage_store
            with usage_store.read(self.root) as txn:
                usage_marker = tuple(txn.marker())
            from ouroboros.config import runtime_setting
            signature = (revision, tuple(sorted(str(t.get("id")) for t in tasks)),
                         tuple(sorted(e.activity_id for e in direct)),
                         tuple(owned_process_state(e, probe_backend=False) for e in self._processes),
                         preparations, checkpoints, reservations, preparing,
                         self.clock._stopped, control.get("bg_consciousness_enabled"), panic_blocks_wake(self.root),
                         allowance.get("status"), allowance.get("remaining_usd"), allowance.get("resets_at"),
                         workers.repo_writer_admission_closed(), usage_marker, runtime_setting("TOTAL_BUDGET", "200"), upkeep)
            import time
            if signature == self._signature and (not self._retry_at or time.monotonic() < self._retry_at):
                return
            self._retry_at = 0.0
            from ouroboros.projects_registry import list_projects, RoomResolver
            from ouroboros.owned_shutdown import owned_records
            projects = list_projects(self.root, strict=True)
            owners, gaps = current_owners(self.root, tasks, direct)
            if preparations or reservations or preparing:
                gaps.append({"reason": "admission_preparation_live"})
            from ouroboros import task_store
            for task_id in checkpoints:
                owners.setdefault(task_id, task_store.read_document(self.root, task_id) or {})
            resolver = RoomResolver(self.root, strict=True)
            chats = {int(p["chat_id"]): p for p in projects}
            by_project = {str(p["id"]): [] for p in projects}
            for task_id, facts in owners.items():
                chat = resolver(task_id, facts.get("parent_task_id"), facts.get("root_task_id")) or facts.get("chat_id")
                project = chats.get(chat)
                if project:
                    by_project[str(project["id"])].append((task_id, facts))
            self._processes = [e for e in owned_records(self.root, strict=True)
                               if e.get("owner_task") or e.get("task_id")]
            with store.open_store(self.root, write=False) as txn:
                episodes = txn.set_rows(EPISODES)
            if self._startup_pending:
                # Startup is one consideration opportunity, not an ordinary-work
                # admission or a Resume. The normal complete-vacancy/admission
                # checks below still decide whether a mind can actually start.
                from ouroboros.delegate_recovery import _PROCESS_GENERATION
                with store.open_store(self.root, write=True) as txn:
                    for project in projects:
                        pid = str(project["id"])
                        prior = txn.get_row(EPISODES, pid) or {}
                        if prior.get("startup_generation") != _PROCESS_GENERATION:
                            prior = {**prior, "startup_generation": _PROCESS_GENERATION,
                                     "eligible": True, "pending": False}
                            txn.put_row(EPISODES, pid, prior)
                        episodes[pid] = prior
                self._startup_pending = False
            updates = {}
            for project in projects:
                pid = str(project["id"])
                members = by_project[pid]
                prior = episodes.get(pid) or {}
                row = observe_project(prior, ordinary_ids={tid for tid, fact in members if not consideration(fact)},
                                      occupied=bool(members), complete=not gaps,
                                      witness_id=str(prior.get("admission_witness_id") or ""))
                if row != prior:
                    updates[pid] = (prior, row)
                episodes[pid] = row
                if gaps and row.get("eligible"):
                    self.last_result[pid] = {"admitted": False, "reason": "current_custody_incomplete", "gaps": gaps}
            if updates:
                with store.open_store(self.root, write=True) as txn:
                    for pid, (prior, row) in updates.items():
                        if (txn.get_row(EPISODES, pid) or {}) == prior:
                            txn.put_row(EPISODES, pid, row)
                        else:
                            episodes[pid] = {}  # a concurrent admission requires a fresh observation
            revisit = False
            for project in projects:
                pid = str(project["id"])
                if not gaps and not by_project[pid] and episodes[pid].get("pending"):
                    receipt = self.last_result[pid] = self.launch(project, signature)
                    if receipt.get("reason") == "admission_failed":
                        # Shared recovery at the existing clock floor, not a project timer.
                        self._retry_at = time.monotonic() + self.clock.floor
                    if not receipt.get("admitted") and receipt.get("reason") not in {
                        "admission_failed", "budget_exhausted", "cost_accounting_unavailable", "repo_writer_gate_closed", "consciousness_disabled_or_unknown", "allowance_unavailable",
                    }:
                        revisit = True
            # Re-read next pass after our own writes/launch; unchanged passes rest.
            self._signature = None if revisit else signature
            self._observation_error = None
        except Exception as exc:
            from ouroboros.store import StoreBusy
            if isinstance(exc, StoreBusy):
                log.debug("Project attention observation deferred: store busy")
                self._signature = None
            else:
                error = (type(exc), str(exc))
                if error != self._observation_error:
                    log.warning("Project attention observation unavailable: %s", exc)
                self._observation_error = error
                import time
                self._signature = signature  # only a retry marker, never vacancy
                self._retry_at = time.monotonic() + self.clock.floor
        finally:
            self._lock.release()

    def launch(self, project: dict, signature: Any) -> dict:
        from ouroboros.consciousness import panic_blocks_wake
        from ouroboros.consciousness_wake import wake_task_metadata
        from ouroboros.config import get_consciousness_autonomy, runtime_setting
        from supervisor.state import control_is
        from supervisor import workers, queue
        from supervisor.active_activity import get_direct_activity_registry
        from ouroboros import store
        import time
        from ouroboros.server_restart import agent_restart_pending
        if agent_restart_pending():
            return {"admitted": False, "reason": "restart_pending"}
        if self.clock._stopped or panic_blocks_wake(self.root) or not control_is(self.clock._read_state(), "bg_consciousness_enabled", True):
            return {"admitted": False, "reason": "consciousness_disabled_or_unknown"}
        window = self.clock._allowance_view(time.time(), fresh=True)
        if window.get("status") != "available" or float(window.get("remaining_usd") or 0) <= 0:
            return {"admitted": False, "reason": "allowance_unavailable"}
        remaining = float(window["remaining_usd"])
        cap = float(runtime_setting("OUROBOROS_PER_TASK_COST_USD", "0") or 0)
        metadata = {**self.clock._routing_facts(int(project["chat_id"])),
                    **wake_task_metadata(get_consciousness_autonomy(), "project_idle", root_cost_ceiling_usd=min(cap, remaining) if cap > 0 else remaining),
                    "wake_cause": "project_idle", "consciousness_cause": "project_idle",
                    "project_id": project["id"],
                    "project_attention": {"project_id": project["id"], "name": project["name"]}}
        def vacant(attention_id=""):
            # Stop/Off may change during allowance/preparation, before registration.
            if (self.clock._stopped or panic_blocks_wake(self.root)
                    or not control_is(self.clock._read_state(), "bg_consciousness_enabled", True)):
                return False
            # Runs in the ordinary registration transaction: no IO under Q.
            tasks = [*queue.PENDING, *(m.get("task") or {} for m in queue.RUNNING.values())]
            registry = get_direct_activity_registry()
            from supervisor.workspace_preparation import _LIVE, _STATE_LOCK
            from supervisor.events_coop_checkpoint import _COOP_CHECKPOINT_INFLIGHT, _COOP_CHECKPOINT_LOCK
            from ouroboros.post_task_checkpoint import POST_TASK_SYNTHESIS_INFLIGHT, POST_TASK_SYNTHESIS_LOCK
            with _STATE_LOCK:
                if _LIVE:
                    return False
            with _COOP_CHECKPOINT_LOCK:
                if tuple(sorted(_COOP_CHECKPOINT_INFLIGHT)) != signature[5]:
                    return False
            with POST_TASK_SYNTHESIS_LOCK:
                if tuple(sorted(tid for source, tid in POST_TASK_SYNTHESIS_INFLIGHT if source == str(self.root.resolve()))) != signature[-1]:
                    return False
            from ouroboros.project_admission import project_admission_basis, project_admission_guard
            from ouroboros.project_admission import ProjectAdmissionError
            try:
                with project_admission_guard(self.root, project_admission_basis(str(project['id']), project)):
                    return (not agent_restart_pending() and tuple(sorted(str(t.get("id")) for t in tasks)) == signature[1]
                            and tuple(sorted(e.activity_id for e in registry.actors() if e.activity_id != attention_id)) == signature[2]
                            and not queue.ADMISSION_RESERVATIONS
                            and not any(not e.task_id for e in registry.executions()))
            except (ProjectAdmissionError, OSError, ValueError):
                return False  # ordinary admission_failed uses the shared retry floor
        binding = []
        def bind(task):
            from ouroboros.projects_registry import RoomResolver
            with queue._queue_lock:
                if not vacant(task["id"]):
                    raise RuntimeError("project attention owners changed during preparation")
                tasks = [dict(t) for t in queue.PENDING] + [dict(m.get("task") or {}) for m in queue.RUNNING.values()]
            direct = [entry for entry in get_direct_activity_registry().actors() if entry.activity_id != task["id"]]
            owners, gaps = current_owners(self.root, tasks, direct)
            resolver = RoomResolver(self.root, strict=True)
            if gaps or any((resolver(tid, facts.get("parent_task_id"), facts.get("root_task_id")) or facts.get("chat_id")) == project["chat_id"]
                           for tid, facts in owners.items()):
                raise RuntimeError("project attention current custody is no longer vacant")
            # Recheck volatile admission/control owners after the off-lock custody read.
            with queue._queue_lock:
                if not vacant(task["id"]):
                    raise RuntimeError("project attention owners changed during custody observation")
            with store.open_store(self.root, write=True) as txn:
                prior = txn.get_row(EPISODES, str(project["id"])) or {}
                consume(txn, str(project["id"]), str(task["id"]))
                binding.append((prior, txn.get_row(EPISODES, str(project["id"]))))
        from ouroboros.config import REPO_DIR
        text = (pathlib.Path(REPO_DIR) / "prompts" / "PROJECT_ATTENTION.md").read_text(encoding="utf-8")
        receipt = workers.handle_wake_direct(int(project["chat_id"]), text, metadata,
                                           bind_input=bind, bind_required=True, admission_check=vacant)
        if not receipt.get("admitted") and binding:
            # A returned refusal proves no body started. Restore only our consume;
            # a newer ordinary admission or another consideration owns its own row.
            prior, consumed = binding[-1]
            with store.open_store(self.root, write=True) as txn:
                if txn.get_row(EPISODES, str(project["id"])) == consumed:
                    txn.put_row(EPISODES, str(project["id"]), prior)
        return receipt


def admission_written(txn: Any, document: dict, current: dict) -> None:
    """An explicit live dispatch mark, never an import/first-store-write witness."""
    mark = document.get("project_attention_admission")
    if not isinstance(mark, dict) or mark == current.get("project_attention_admission") or consideration(document):
        return
    task = {"id": document["task_id"], "project_id": mark.get("project_id"),
            "_attempt": mark.get("attempt")}
    record_admission(txn, task)


def tick_attention(clock: Any, *, ready: bool, pending_inputs: Any) -> None:
    if not hasattr(clock, "_project_attention"):
        clock._project_attention = ProjectAttention(clock._drive_root, clock)
    clock._project_attention.tick(ready=ready, pending_inputs=pending_inputs)
