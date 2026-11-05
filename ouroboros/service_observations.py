"""Read-only service facts for existing heartbeats and work tails.

Owners sample their real handles; the server discovers surviving services from the
current ownership set, never spawn history. Stop/replacement retains the last fact
in the existing task document before forgetting the handle. No observation grants
controls, execution authority, readiness, test progress or success to the author.
"""
from __future__ import annotations

import logging
import pathlib
import time
from typing import Any

log = logging.getLogger(__name__)


def observe(record: Any, facts: dict | None) -> dict:
    """One exact start, dated after its own probe; output availability is independent."""
    row = {"task_id": record.task_id, "service_id": record.service_id, "name": record.name,
           "started_at": record.started_at, "state": "unknown", "returncode": None,
           "output_bytes": None, "output_updated_at": None}
    if isinstance(facts, dict) and facts.get("started_at") == record.started_at:
        row.update({key: facts.get(key) for key in ("state", "returncode")})
    row["observed_at"] = (facts or {}).get("observed_at") or time.time()
    path = getattr(record, "log_path", None)
    if path is None and getattr(getattr(record, "executor", None), "kind", None) == "local":
        path = getattr(record, "backend_log_path", None)
    if path:
        try:
            info = pathlib.Path(path).stat()
            row.update(output_bytes=info.st_size, output_updated_at=info.st_mtime)
        except OSError:
            pass  # a denied/missing log is not a denied/missing process
    return row


def collect(task_id: str, attempt: int) -> dict:
    from ouroboros.tools import services
    from ouroboros.workspace_executor import _services_snapshot

    with services._LOCK:
        records = [r for r in services._SERVICES.values() if r.task_id == task_id]
    records += [r for r in _services_snapshot() if r.task_id == task_id]
    rows = []
    for record in records:
        try:
            if getattr(getattr(record, "executor", None), "kind", None) == "docker_exec":
                facts = dict(getattr(record, "execution_observation", None) or {})
            else:
                facts = services.service_execution_facts(record.service_id)
        except Exception:
            facts = None
        rows.append(observe(record, facts))
    return {"task_id": task_id, "task_attempt": attempt, "observed_at": time.time(), "services": rows}


def retain_final_observation(record: Any, facts: dict | None) -> None:
    """Publish before registry/custody removal; failure cannot delay Stop or Panic."""
    from ouroboros.task_results import write_task_result

    root = getattr(record, "drive_root", None)
    if root is None:
        return
    row = observe(record, facts)

    def merge(existing, fields):
        if not existing:
            return None  # a service never invents a task author
        prior = dict(existing.get("service_observations") or {})
        rows = [old for old in prior.get("services") or [] if
                (old.get("service_id"), old.get("started_at")) != (row["service_id"], row["started_at"])]
        rows.append(row)
        return {"status": existing["status"], "service_observations": {
            "task_id": record.task_id, "task_attempt": existing.get("task_attempt") or existing.get("attempt") or 1,
            "observed_at": row["observed_at"], "services": rows}}
    try:
        write_task_result(root, record.task_id, "running", _field_projector=merge)
    except Exception:
        log.warning("Final service observation unavailable for %s", record.service_id, exc_info=True)


def discover(drive_root: Any, *, skip_tasks: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Bounded ownership-set discovery, including kept services after their author.

    This is a passive host read: no Docker command or readiness/network work.
    Local identity mismatch/unreadability remains unknown; missing leader alone is
    not exit while its service group survives. Legacy starts without metadata stay
    unknown with null start, not a fabricated current start.
    """
    from ouroboros.owned_shutdown import owned_records
    from ouroboros.process_custody import owned_process_state
    from ouroboros import workspace_executor as executor

    rows, gaps = [], []
    try:
        entries = owned_records(drive_root, strict=True)
    except Exception:
        return [], [{"reason": "service_ownership_unavailable"}]
    # Executor local services have a second supervised workspace_service row.
    # The executor registration is the single descriptive record for that start.
    for entry in entries:
        owner = str(entry.get("owner_task") or entry.get("task_id") or "")
        if owner and owner in (skip_tasks or ()):
            continue  # owner heartbeat already describes it; no per-process probe
        ledger = entry.get("ledger_entry")
        info, state, rc = {}, "unknown", None
        if isinstance(ledger, dict):
            if not str(ledger.get("purpose") or "").startswith("service:"):
                continue
            info = dict(ledger.get("service_info") or {})
            info.setdefault("task_id", entry.get("owner_task"))
            info.setdefault("name", str(ledger.get("purpose") or "").removeprefix("service:"))
            info.setdefault("service_id", f"{info.get('task_id')}:{info.get('name')}")
        elif entry.get("kind") == "service":
            path = pathlib.Path(str(entry.get("record_path") or ""))
            record = executor._load_process_record(path)
            if record and executor._valid_process_record(path, record, check_identity=False):
                info = dict(record)
                # Docker without an owner observation is unknown, never host-CLI exit.
            else:
                info = {"task_id": entry.get("task_id"), "service_id": entry.get("record_id"), "name": "Service"}
        else:
            continue
        task_id = str(info.get("task_id") or "")
        if not task_id:
            continue
        state = {"live": "running", "ended": "exited"}.get(owned_process_state(entry, probe_backend=False), "unknown")
        row = {key: info.get(key) for key in ("service_id", "name", "started_at")}
        row.update(task_id=task_id, state=state, returncode=rc, observed_at=time.time(),
                   output_bytes=None, output_updated_at=None)
        output = info.get("log_path") or (info.get("backend_log_path") if info.get("executor_type") == "local" else None)
        if output:
            try:
                stat = pathlib.Path(output).stat()
                row.update(output_bytes=stat.st_size, output_updated_at=stat.st_mtime)
            except OSError:
                pass
        rows.append(row)
    return rows, gaps
