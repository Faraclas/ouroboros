"""The owner's words that caused a work tree: carried by value, resolved for older tasks, rendered verbatim.

A helper's floor is its assignment plus the words of the owner that caused the work
(BIBLE P1: provenance matters). ``governing_words_for_schedule`` computes them once,
when a parent schedules a helper, from what the parent already holds: a child passes
on exactly the words it inherited, so a grandchild sees the root's words unchanged;
any other run passes the owner-sourced rows of its own corpus (``ctx._owner_directives``)
— never ``initial_text``, which is not the owner's — and never reads the chat chain.
The words ride the existing tree-origin channel (``origin_metadata``) into every
descendant's ``task.metadata`` under ``FIELD``. A run with no owner words carries
``ABSENT_FIELD`` set to the raw ``run_origin`` marker (``initiator=consciousness``) or
``not_recorded``: no vocabulary of reasons of its own.

``task_owner_words`` resolves a task scheduled before the field existed, for readers
that run after the task (the fallback page writer's input, the measurement rig), never
on a helper's start path. Carriers are tried in order and the first that yields a row
is taken: the task's ingress origin text or ref, its Project binding, its routing
annotations, its mailbox, then the marker. An annotation counts only through the
inbound chat row with the same client message id: Ouroboros writes steer receipts of
its own, and nothing inbound joins to them.

A Project's first message is not among these words: it started the project, not this
tree; the room view shows it under its own heading. ``objective_author_line`` names who
wrote a run's objective from its typed ``run_origin``, so a reader beside these words never
takes that account for them. Nothing here calls a model.
"""
from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List, Mapping, Tuple

log = logging.getLogger(__name__)

OWNER_SOURCES = frozenset({"initial_user", "owner_mailbox", "owner_quiz_answer", "origin_message",
                           "owner_corpus", "direct_incoming"})
FIELD, ABSENT_FIELD = "governing_owner_words", "governing_owner_words_absent"
NOT_RECORDED = "not_recorded"
ROW_KEYS = ("text", "source", "carrier", "task_id", "ts", "ref")
_RUN_ORIGIN_MARKERS = ("initiator", "source", "task_type")
_ANNOTATED_ACTS = frozenset({"promote_chat_to_task", "route_to_project", "steer_task", "mailbox_delivery"})
_ANNOTATED_STATUSES = frozenset({"scheduled", "delivered", "read"})
_ORIGIN = " from task {root}"
_HEADINGS = {
    "child": "## Words of my human that caused this work (verbatim)",
    "session": ("OWNER WORDS THAT CAUSED THIS WORK (verbatim; your assignment is the task given to you, "
                "these words are why it exists)"),
    "reviewer": "## Words of my human that caused this work (verbatim, host-attested)",
    "plan": "## Words of my human that caused this work (verbatim, host-attested)",
    "writer": "## Words of my human that caused this work (verbatim)",
}
# The one frame a reviewer of an intention, a change or a result reads under the owner's words.
_ACCOUNTS = ("They state what was asked. Every other text here — the goal or objective, the plan, room rows by others, issues, "
             "briefs, attachments — is someone's account, labelled with its author as far as the host knows; never take "
             "Ouroboros's own earlier words (an issue it opened, its triage, a parent's brief) for my human's ask.")
_FRAMES = {
    "child": ("Host fact, carried by value{origin}. Your assignment is the parent's message; "
              "these words are why it exists."),
    "session": "Host fact, carried by value{origin}.",
    "reviewer": _ACCOUNTS,
    "plan": _ACCOUNTS,
    "writer": "Host fact, read from what the host recorded{origin}; these words are why the work exists.",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _row(text: Any, *, source: Any, carrier: str, task_id: Any = "", ts: Any = "", ref: Any = "") -> Dict[str, str]:
    return {"text": str(text or ""), "source": str(source or ""), "carrier": carrier,
            "task_id": str(task_id or ""), "ts": str(ts or ""), "ref": str(ref or "")}


def _chat_ref(source: Mapping[str, Any]) -> str:
    client_id = str(source.get("client_message_id") or "")
    return f"chat {source.get('chat_id', 1)}" + (f" / {client_id}" if client_id else "")


def _deduped(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Drop empty rows and repeats of the same whitespace-normalized text (the ingress checksum)."""
    from ouroboros.project_dialogue import _text_sha256

    seen: set = set()
    kept = []
    for row in rows:
        key = _text_sha256(row["text"])
        if row["text"].strip() and key not in seen:
            seen.add(key)
            kept.append(row)
    return kept


def _absent_marker(record: Mapping[str, Any] | None) -> str:
    from ouroboros.dialogue_provenance import run_origin

    origin = run_origin(record)
    return next((f"{key}={origin[key]}" for key in _RUN_ORIGIN_MARKERS if origin.get(key)), NOT_RECORDED)


def _ctx_metadata(ctx: Any) -> Mapping[str, Any]:
    return _mapping(getattr(ctx, "task_metadata", None))


def owner_evidence_gaps(record: Mapping[str, Any]) -> list:
    """The final host capture supersedes admission's dated incompleteness, never vice versa."""
    inputs = _mapping(_mapping(record.get("review_evidence")).get("task_inputs"))
    if "owner_corpus_gaps" in inputs:
        return list(inputs["owner_corpus_gaps"])
    return list(_mapping(record.get("metadata")).get("owner_corpus_gaps") or [])


def objective_author_line(origin: Mapping[str, Any] | None) -> str:
    """Who wrote a run's objective, from the typed facts ``dialogue_provenance.run_origin`` returns,
    never from the objective's text: one phrase for every reader (the mind's first turn, a plan
    council's packet, a result review's subject). A kind this projection has never heard of shows
    its raw marker; only a run with no origin fact at all reads as having no recorded author."""
    origin = _mapping(origin)
    author = _mapping(origin.get("objective_author"))
    kind = str(author.get("kind") or "")
    if origin.get("delegation_role") == "subagent":
        return (f"the assignment parent task {origin.get('parent_task_id') or '?'} wrote for this helper — "
                "Ouroboros's account, not my human's words")
    if kind == "task":
        return f"drafted by task {author.get('task_id')}, not spoken by my human"
    if kind == "continuation":
        return f"the host's Continue work order for task {author.get('predecessor_task_id')}"
    if kind:
        return f"its author is recorded by the host as kind={kind}"
    if origin.get("initiator") == "consciousness":
        return "started by my consciousness"
    if origin.get("owner_ingress"):
        return "my human's message started this run; the host does not attest this text as their exact words"
    if origin.get("task_type") == "presence":
        return f"a message from {origin.get('text_author') or 'a correspondent'} in a connected place"
    if origin.get("schedule_id"):
        return f"the template of schedule {origin['schedule_id']}; the host does not attest this text as my human's words"
    if origin.get("origin_task_id"):
        return f"started from task {origin['origin_task_id']}; the host does not attest this text as my human's words"
    return "its author is not recorded by the host" + (f" (host marker: source={origin['source']})" if origin.get("source") else "")


def objective_author_text(ctx: Any) -> str:
    """``objective_author_line`` over this run's own typed origin (its task type and metadata)."""
    from ouroboros.dialogue_provenance import run_origin

    return objective_author_line(run_origin({"type": getattr(ctx, "current_task_type", None), "metadata": dict(_ctx_metadata(ctx))}))


def directive_owner_rows(ctx: Any) -> List[Dict[str, str]]:
    """The owner-sourced rows of the run's retained corpus, verbatim (images by reference).

    The door-stamped first message is the door's own record of it (``origin_message_text``)
    when the door kept one: the host may have prefixed a notice of its own to that user
    turn (a Swarm initiative), never to the door's text.
    """
    from ouroboros.review_evidence_sections import _owner_content_projection

    origin_ref = _mapping(_ctx_metadata(ctx).get("origin_message_ref"))
    door_text = _ctx_metadata(ctx).get("origin_message_text")
    door_text = door_text if isinstance(door_text, str) and door_text.strip() else None
    task_id = str(getattr(ctx, "task_id", "") or "")
    rows = []
    for item in getattr(ctx, "_owner_directives", None) or []:
        if not isinstance(item, dict) or item.get("source") not in OWNER_SOURCES:
            continue
        stamped = item["source"] == "initial_user" and origin_ref
        msg_id = str(item.get("msg_id") or "")
        content = door_text if stamped and door_text is not None else item.get("content")
        rows.append(_row(_owner_content_projection(content), source=item["source"], carrier="ctx",
                         task_id=task_id, ts=origin_ref.get("ts") if stamped else "",
                         ref=_chat_ref(origin_ref) if stamped else (f"msg {msg_id}" if msg_id else "")))
    return _deduped(rows)


def task_governing_words(task: Mapping[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    """``(rows, absence marker)`` carried by value, read at the top level or under ``metadata``."""
    source = _mapping(task)
    nested = _mapping(source.get("metadata"))
    holder = source if (FIELD in source or ABSENT_FIELD in source) else nested
    raw = holder.get(FIELD)
    rows = [_row(item.get("text"), source=item.get("source"), carrier=str(item.get("carrier") or ""),
                 task_id=item.get("task_id"), ts=item.get("ts"), ref=item.get("ref"))
            for item in (raw if isinstance(raw, list) else []) if isinstance(item, Mapping)]
    return [row for row in rows if row["text"].strip()], str(holder.get(ABSENT_FIELD) or "")


def governing_words_for_schedule(ctx: Any) -> Dict[str, Any]:
    """``{FIELD: rows}`` or ``{ABSENT_FIELD: marker}`` for the helper this run is about to schedule."""
    metadata = _ctx_metadata(ctx)
    if str(metadata.get("delegation_role") or "") == "subagent":
        rows, absent = task_governing_words(metadata)
    else:
        rows, absent = directive_owner_rows(ctx), ""
    if rows:
        gaps = getattr(ctx, "_owner_evidence_gaps", None) or metadata.get("owner_corpus_gaps")
        return {FIELD: rows, **({ABSENT_FIELD: "incomplete: " + "; ".join(gaps)} if gaps else {})}
    return {ABSENT_FIELD: absent or _absent_marker({"type": getattr(ctx, "current_task_type", None),
                                                    "metadata": dict(metadata)})}


def _inbound_rows(drive_root: Any, client_ids: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Owner rows addressed by client message id, in canonical order."""
    from ouroboros import dialogue_store as ds

    ds.require_imported(drive_root, "chat")
    return [entry.row for entry in ds.iter_probes(drive_root, "chat", [("client_message_id", key) for key in client_ids])
            if _owner_row(entry.row)]


def _owner_row(row: Mapping[str, Any] | None) -> bool:
    """The owner-source cut: inbound, non-empty, not a system/presence/skill-repair injection."""
    return bool(row and row.get("direction") == "in" and str(row.get("text") or "").strip()
                and not (row.get("system_type") or row.get("presence") or row.get("source") == "skill_repair"))


def _message_rows(drive_root: Any, task_id: str, text: Any, ref: Any, carrier: str) -> List[Dict[str, str]]:
    ref = _mapping(ref)
    if isinstance(text, str) and text.strip():
        return [_row(text, source="origin_message", carrier=carrier, task_id=task_id, ts=ref.get("ts"),
                     ref=_chat_ref(ref) if ref else "")]
    if not ref:
        return []
    from ouroboros.project_dialogue import resolve_owner_message_source

    row = resolve_owner_message_source(drive_root, dict(ref))
    if not _owner_row(row):
        return []
    return [_row(row["text"], source="origin_message", carrier=carrier, task_id=task_id, ts=row.get("ts"),
                 ref=_chat_ref(row))]


def _origin_carrier(drive_root: Any, task_id: str, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    nested = _mapping(record.get("metadata"))
    return _message_rows(drive_root, task_id,
                         record.get("origin_message_text") or nested.get("origin_message_text"),
                         record.get("origin_message_ref") or nested.get("origin_message_ref"), "origin_message")


def _binding_carrier(drive_root: Any, task_id: str, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    from ouroboros.projects_registry import project_task_bindings

    binding = _mapping(project_task_bindings(drive_root).get(task_id))
    return _message_rows(drive_root, task_id, binding.get("source_text"), binding.get("source_ref"), "binding")


def _annotation_carrier(drive_root: Any, task_id: str, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    from ouroboros.project_dialogue import _ANNOTATIONS_NAME, _latest_annotations_by_token

    acts: Dict[str, str] = {}
    for (client_id, _token), row in _latest_annotations_by_token(
            pathlib.Path(drive_root) / "logs" / _ANNOTATIONS_NAME).items():
        if (row.get("action") in _ANNOTATED_ACTS and row.get("status") in _ANNOTATED_STATUSES
                and str(row.get("target") or "") == task_id):
            acts.setdefault(client_id, str(row["action"]))
    return [_row(row["text"], source=acts[str(row["client_message_id"])], carrier="annotation", task_id=task_id,
                 ts=row.get("ts"), ref=_chat_ref(row)) for row in (_inbound_rows(drive_root, acts) if acts else [])]


def _mailbox_carrier(drive_root: Any, task_id: str, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    from ouroboros.owner_mailbox import KIND_OWNER_TEXT, drain_owner_entries
    from ouroboros.owner_relay import owner_text, _owner_row as relay_owner_row

    try:
        entries = drain_owner_entries(pathlib.Path(drive_root), task_id, include_acknowledged=True)
    except (OSError, ValueError):
        return []
    return [_row(entry.get("text"), source="owner_mailbox", carrier="mailbox", task_id=task_id, ts=entry.get("ts"),
                 ref=f"msg {entry.get('msg_id')}") for entry in entries if entry.get("kind") == KIND_OWNER_TEXT] + [
        _row(owner_text(row), source="owner_mailbox", carrier="relay", task_id=task_id, ts=row.get("ts"),
             ref=_chat_ref(row)) for entry in entries if entry.get("kind") == "relay_entry"
        for row in entry.get("owner_rows") or [] if relay_owner_row(row)]


def _task_record(drive_root: Any, task_id: str) -> Dict[str, Any]:
    if not task_id:
        return {}
    from ouroboros.task_status import load_effective_task_result

    try:
        record = load_effective_task_result(drive_root, task_id, materialize_artifacts=False)
    except Exception:
        log.debug("owner words: task record unreadable for %s", task_id, exc_info=True)
        return {}
    return dict(record) if isinstance(record, dict) else {}


def task_owner_words(drive_root: Any, task_id: str, *,
                     task: Mapping[str, Any] | None = None) -> Tuple[List[Dict[str, str]], str]:
    """The words for a task: its carried field, else the first carrier of its root that yields a row."""
    record = dict(task) if isinstance(task, Mapping) else _task_record(drive_root, task_id)
    gaps = owner_evidence_gaps(record)
    incomplete = "incomplete: " + "; ".join(gaps) if gaps else ""
    from ouroboros.owner_relay import owner_text, _owner_row as held_owner_row
    held = [_row(owner_text(row), source="owner_mailbox", carrier="held", task_id=task_id,
                 ts=row.get("ts"), ref=address) for address, row in (record.get("held_owner_messages") or {}).items()
            if held_owner_row(row)]
    rows, absent = task_governing_words(record)
    if rows or absent:
        return _deduped(rows + held), incomplete or ("" if rows or held else absent)
    task_id = str(task_id or "")
    root_id = str(record.get("root_task_id") or _mapping(record.get("metadata")).get("root_task_id") or task_id)
    if root_id != task_id:
        task_id, record = root_id, (_task_record(drive_root, root_id) or record)
    for carrier in (_origin_carrier, _binding_carrier, _annotation_carrier, _mailbox_carrier):
        rows = _deduped(carrier(drive_root, task_id, record))
        if rows:
            return _deduped(rows + held), incomplete
    return _deduped(held), incomplete or ("" if held else _absent_marker(record))


def ensure_followup_owner_evidence(ctx: Any) -> None:
    """Serialize the legacy actor's first source recovery across parallel tool checks."""
    if ctx is None:
        return
    metadata = _ctx_metadata(ctx)
    if getattr(ctx, "_followup_owner_evidence_loaded", False):
        return
    inherited = metadata.get("owner_corpus_gaps") or []
    ctx._owner_evidence_gaps = list(dict.fromkeys([
        *(getattr(ctx, "_owner_evidence_gaps", None) or []), *inherited]))
    if not metadata.get("followup_origin"):
        return
    import threading

    lock = vars(ctx).setdefault("_followup_owner_evidence_lock", threading.RLock())
    with lock:
        _recover_followup_owner_evidence(ctx)


def _legacy_mail_media(ctx: Any, root: pathlib.Path, task_id: str, rows: list, *, current: bool = False,
                       retained: Any = None) -> list:
    """Restore old capture's pixels from host mailbox identities, never rendered refs."""
    from ouroboros.owner_mailbox import drain_owner_entries, KIND_OWNER_TEXT
    from ouroboros.task_custody import own_child_drives
    from ouroboros.artifacts import resolve_attachment_manifest

    wanted = {row.get("msg_id") for row in rows if row.get("source") == "owner_mailbox" and not row.get("media")}
    if not wanted and not current:
        return rows
    by_id = {}
    raw_entries = []
    places = [root, *own_child_drives(root, task_id)]
    for drive in places:
        status = {}
        entries = drain_owner_entries(drive, task_id, include_acknowledged=True, _read_status=status)
        if not status.get("complete"):
            raise ValueError("legacy owner mailbox incomplete")
        raw_entries.extend(entries)
    if isinstance(retained, dict) and retained.get("rows"):
        if retained.get("read_complete") is not True:
            raise ValueError("retained owner mailbox incomplete")
        import json
        raw_entries.extend(json.loads(line) for line in retained["rows"])
    for raw in raw_entries:
        entries = [raw]
        if raw.get("kind") == "relay_entry":
            from ouroboros.owner_relay import _owner_row, owner_text, quote_content
            from ouroboros.chat_chain import format_address, row_address
            for quoted in raw.get("owner_rows") or []:
                if not _owner_row(quoted):
                    raise ValueError("legacy relay has no owner provenance")
                mid = format_address(row_address(quoted))
                if mid not in wanted and not current:
                    continue
                manifest = []
                quote_content(ctx, raw, quoted, owner_text(quoted), manifest_out=manifest)
                manifest = [{**item, "is_image": bool(item.get("is_image") or ref.get("kind") == "image")}
                            for ref, item in zip(quoted.get("attachments") or [], manifest)]
                entries.append({"kind": KIND_OWNER_TEXT, "msg_id": mid,
                                "text": owner_text(quoted), "attachment_manifest": manifest})
        for entry in entries:
            mid = entry.get("msg_id")
            kind = entry.get("kind") or KIND_OWNER_TEXT
            if mid not in wanted and not (current and kind in {KIND_OWNER_TEXT, "quiz_answer"}):
                continue
            if kind not in {KIND_OWNER_TEXT, "quiz_answer"} or not isinstance(entry.get("text"), str):
                raise ValueError("legacy owner mailbox identity is not owner text")
            previous = by_id.get(mid)
            if previous is not None and (previous.get("text"), previous.get("attachment_manifest")) != (
                    entry.get("text"), entry.get("attachment_manifest")):
                raise ValueError("legacy owner mailbox identity conflict")
            by_id[mid] = entry
    if current:
        present = {row.get("msg_id") for row in rows}
        rows.extend({"source": "owner_quiz_answer" if entry.get("kind") == "quiz_answer" else "owner_mailbox",
                     "content": entry["text"], "msg_id": mid}
                    for mid, entry in by_id.items() if mid not in present)
    for row in rows:
        entry = by_id.get(row.get("msg_id"))
        if not entry or row.get("media"):
            continue
        for place in places:
            try:
                manifest = resolve_attachment_manifest(place, task_id, entry)
                media = [{"path": item.get("abs_path") or "", "sha256": item.get("sha256") or "",
                          "caption": str(item.get("label") or item.get("name") or ""),
                          **({"unavailable": "legacy owner image not staged"} if item.get("status") != "staged" else {})}
                         for item in manifest if item.get("is_image")]
                if not media:
                    break
                from ouroboros.review_evidence_sections import materialize_owner_corpus_content, project_owner_corpus_row
                row["media"] = media
                native = materialize_owner_corpus_content(row, drive_root=place)
            except (OSError, ValueError):
                if place == places[-1] and not row.get("media"):
                    raise
                continue  # try this same owner's other store; never overwrite conflicting bytes
            row["media"] = project_owner_corpus_row({**row, "content": native, "media": []}, ctx=ctx)["media"]
            break
    return rows


def _recover_followup_owner_evidence(ctx: Any) -> None:
    """Repair the legacy missing-corpus carrier once per actor, not once per check.

    Follow only frozen schedule-admission origins, never money lineage or task prose.
    This supplies evidence for judgment; it grants no action or replay authority.
    """
    metadata = _ctx_metadata(ctx)
    if (getattr(ctx, "_followup_owner_evidence_loaded", False)
            or ("owner_corpus" in metadata and not metadata.get("owner_corpus_gaps"))
            or not metadata.get("followup_origin") or _mapping(metadata.get("objective_author")).get("kind") != "task"):
        return
    previous_gaps = _mapping(getattr(ctx, "_owner_evidence_recovery", None)).get("gaps") or []
    from ouroboros.task_results import load_task_result
    from ouroboros.tool_access import canonical_data_root
    from ouroboros.loop_messages import _record_owner_directive

    task_id = str(_mapping(metadata.get("followup_origin")).get("task_id") or "")
    seen, groups, gaps = set(), [], []
    root = canonical_data_root(ctx)
    while task_id:
        if task_id in seen:
            gaps.append(f"followup owner-source cycle at {task_id}")
            break
        seen.add(task_id)
        try:
            result = load_task_result(root, task_id, strict=True)
            if not isinstance(result, dict):
                raise ValueError("task source missing")
            frozen = _mapping(result.get("schedule_admission"))
            parent_meta = _mapping(_mapping(frozen.get("task")).get("metadata"))
            inputs = _mapping(_mapping(result.get("review_evidence")).get("task_inputs"))
            carried = parent_meta.get("owner_corpus")
            complete = isinstance(carried, list) and not parent_meta.get("owner_corpus_gaps")
            if isinstance(inputs.get("owner_requirements_and_decisions"), list):
                carried = inputs["owner_requirements_and_decisions"]
                complete = inputs.get("owner_corpus_gaps") == [] and (
                    "owner_requirements_and_decisions" not in (inputs.get("unavailable_sections") or []))
            captured = isinstance(inputs.get("owner_requirements_and_decisions"), list)
            if not complete and not captured and not isinstance(carried, list):
                raise ValueError("owner corpus capture unavailable")
            rows = carried if isinstance(carried, list) else inputs.get("owner_requirements_and_decisions") or []
            if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("source") not in (
                    OWNER_SOURCES | {"initial_text", "initial_text_transcript", "principal_task_message", "relayed_peer_message"})
                    for row in rows):
                raise ValueError("owner corpus source malformed")
            rows = [dict(row) for row in rows if isinstance(row, dict) and row.get("source") in OWNER_SOURCES]
            if not complete or not captured:
                rows = _legacy_mail_media(ctx, root, task_id, rows, current=not captured,
                                          retained=result.get("owner_mailbox"))
            groups.append((task_id, rows))
            if complete:
                break  # a complete producer-carried boundary, including intentional absence
            origin = _mapping(parent_meta.get("followup_origin"))
            if origin:
                if frozen.get("status") != "accepted":
                    raise ValueError("followup admission unproven")
                task_id = str(origin.get("task_id") or "")
            else:
                if parent_meta.get("owner_corpus_gaps") or inputs.get("owner_corpus_gaps") or (
                        "owner_requirements_and_decisions" in (inputs.get("unavailable_sections") or [])):
                    raise ValueError("original owner evidence incomplete")
                if not rows and not isinstance(inputs.get("owner_requirements_and_decisions"), list):
                    raise ValueError("original owner evidence unavailable")
                task_id = ""
        except Exception as exc:
            gaps.append(f"followup owner source {task_id}: {type(exc).__name__}")
            break
    # Keep current owner answers beside ancestral words, never replace either.
    current_count = len(getattr(ctx, "_owner_directives", None) or [])
    if groups and not gaps and not isinstance(getattr(ctx, "_owner_directives", None), list):
        ctx._owner_directives = []  # successful recovery can prove intentional absence
    for source_task, rows in reversed(groups if not gaps else []):
        for row in rows:
            _record_owner_directive(ctx, source=row["source"], content=row.get("content"), media=row.get("media"),
                owner_text=row.get("owner_text", ""),
                inherited=True,
                msg_id=str(row.get("msg_id") or ""),
                origin={key: row[key] for key in ("source_task_id", "relayed_from_task_id", "quiz_id") if row.get(key)})
    owned_gaps = list(dict.fromkeys([*previous_gaps, *gaps])) if gaps else []
    ctx._owner_evidence_recovery = {"source_tasks": sorted(seen), "gaps": owned_gaps}
    ctx._followup_owner_evidence_loaded = not gaps
    if not gaps:
        repaired = [*previous_gaps, *(metadata.get("owner_corpus_gaps") or [])]
        ctx._owner_evidence_gaps = [gap for gap in getattr(ctx, "_owner_evidence_gaps", []) if gap not in repaired]
        if isinstance(getattr(ctx, "task_metadata", None), dict):
            ctx.task_metadata.pop("owner_corpus_gaps", None)
    if current_count:
        corpus = ctx._owner_directives
        ctx._owner_directives = corpus[current_count:] + corpus[:current_count]
    if gaps:
        ctx._owner_evidence_gaps = list(dict.fromkeys([*(getattr(ctx, "_owner_evidence_gaps", []) or []), *gaps]))


def render_owner_words(rows: Any, absent: str = "", *, audience: str, root_task_id: str = "", indent: str = "") -> str:
    """One verbatim section for a child, a session, a reviewer, a plan review or the page writer.

    ``indent`` prefixes every line of the words (the memory view's two spaces), so a line of
    the owner's own that starts with ``## `` never reads as a section of the request.
    """
    if audience not in _HEADINGS:
        raise ValueError(f"unknown owner-words audience {audience!r}; expected one of {sorted(_HEADINGS)}")
    rows = [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, Mapping)
            and str(row.get("text") or "").strip()]
    if not rows:
        return f"No words of my human are recorded for this work (host marker: {absent})." if absent else ""
    origin = _ORIGIN.format(root=root_task_id) if root_task_id else ""
    blocks = []
    for row in rows:
        source, task_id = str(row.get("source") or ""), str(row.get("task_id") or "")
        stamp = [str(row.get("ts") or ""), "owner", f"{source} of task {task_id}" if task_id else source,
                 str(row.get("ref") or "")]
        words = "\n".join(indent + line if line else line for line in str(row["text"]).split("\n"))
        blocks.append("[" + " · ".join(part for part in stamp if part) + "]\n" + words)
    status = f"Host source status: {absent}.\n" if absent else ""
    return status + f"{_HEADINGS[audience]}\n{_FRAMES[audience].format(origin=origin)}\n" + "\n\n".join(blocks)


def owner_words_text(ctx: Any, *, audience: str = "reviewer") -> str:
    """``render_owner_words`` over the words this run would hand a helper."""
    words = governing_words_for_schedule(ctx)
    root = str(_ctx_metadata(ctx).get("root_task_id") or getattr(ctx, "task_id", "") or "")
    return render_owner_words(words.get(FIELD) or [], str(words.get(ABSENT_FIELD) or ""),
                              audience=audience, root_task_id=root)
