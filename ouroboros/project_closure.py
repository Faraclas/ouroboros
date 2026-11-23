"""Structured project closure briefs (`projects/<id>/closure_brief.md`).

Generates deterministic YAML-frontmatter + Markdown summaries from a project's
durable records on close (registry row, workpad, journal, project knowledge, and
room chronicle if activated) and provides idempotent backfill for already-closed
tombstoned projects. Auto-generated briefs refresh on re-close; manually authored
briefs are preserved unless `overwrite=True`.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import yaml

from ouroboros import memory_index, project_footprint, projects_registry
from ouroboros.chronicle_store import ChronicleStore, capture as chronicle_capture
from ouroboros.contracts.chat_id_policy import project_chat_id
from ouroboros.knowledge import INDEX_FILE, KnowledgeAddress, read_knowledge_note, sanitize_topic
from ouroboros.project_facts import (
    explicit_project_id_ok,
    project_closure_brief_path,
    project_display_name,
    sanitize_project_id,
)
from ouroboros.utils import strip_markdown, utc_now_iso, write_text_atomic

log = logging.getLogger(__name__)

CLOSURE_BRIEF_SCHEMA_VERSION = 1
CLOSURE_BRIEF_AUTHOR = "project_close"

FRONTMATTER_FIELDS: Tuple[str, ...] = (
    "schema_version",
    "project_id",
    "chat_id",
    "closed_at",
    "authored_by",
    "title",
    "summary",
    "keywords",
    "touched_paths",
    "commit_shas",
    "error_signatures",
    "supersedes",
)

_MAX_TITLE_CHARS = 140
_MAX_SUMMARY_CHARS = 600
_MAX_KEYWORDS = 40
_MAX_PATHS = 60
_MAX_SHAS = 40
_MAX_ERRORS = 40
_MAX_SUPERSEDES = 20

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "the", "to", "with", "project",
})
_HEX_ENGLISH_WORDS = frozenset({
    "baggage", "cabbage", "deceded", "defaced", "effaced", "facaded", "feedbag",
})

_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/.-])(/(?:workspaces|home|var|etc|opt|tmp|mnt|usr)/[A-Za-z0-9._~/@+-]+)"
)
_REL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/.-])("
    r"(?:(?:ouroboros|tests|docs|web|scripts|supervisor|prompts|projects|state|memory|observability|src|lib|pkg|cmd|config)/[A-Za-z0-9._/@+-]+)"
    r"|(?:[A-Za-z0-9_.-]+/[A-Za-z0-9._/-]+\.(?:py|js|jsx|ts|tsx|md|toml|json|jsonl|ya?ml|sh|css|html|rs|go|sql|sqlite3?|txt|cfg|ini))"
    r")"
)
_COMMIT_CTX_RE = re.compile(
    r"(?i)(?:\b(?:commit(?:ted|s)?|head|sha(?!256)|merge[d]?|adopt(?:ed|ion)|pushed|cherry-pick(?:ed)?|revert(?:ed)?|"
    r"baseline|base|ref|origin/[A-Za-z0-9_.-]+|candidate/[A-Za-z0-9_.-]+)\b(?:\s+(?:to|on|at|from|in|of))?|\B@)"
    r"\s*(?:[:=\(`\"'\[])?\s*([0-9a-f]{7,40})(?![0-9a-fA-F-])"
)
_FULL_SHA1_RE = re.compile(r"(?<![0-9a-fA-F-])([0-9a-f]{40})(?![0-9a-fA-F-])")
_COMMIT_LIST_RE = re.compile(
    r"(?i)\bcommits?\s*[:=]?\s*((?:`?[0-9a-f]{7,40}`?(?:\s*,\s*|\s+)){1,10})"
)
_BARE_SHA_RE = re.compile(r"(?<![0-9a-fA-F-])([0-9a-f]{7,40})(?![0-9a-fA-F-])")
_STRUCTURED_ERR_RE = re.compile(
    r"\b(?:"
    r"TOOL_[A-Z][A-Z0-9_]+"
    r"|PREFLIGHT_[A-Z][A-Z0-9_]+"
    r"|SQLITE_[A-Z][A-Z0-9_]+(?::[A-Za-z0-9_.-]+)?"
    r"|[A-Z][A-Z0-9_]+_(?:ERROR|FAILED|DENIED|TIMEOUT|EXHAUSTED|CORRUPT|REFUSED|CONFLICT|BUSY|LOCKED|MISMATCH)"
    r"|[A-Z][A-Za-z0-9]+(?:Error|Exception)"
    r")\b"
)
_SUPERSEDES_RE = re.compile(
    r"(?i)\b(?:supersedes|replaces\s+(?:plan|brief|closure_brief|doc|proposal|design|draft|spec|work[- ]item|wi))\s*[:#]?\s*"
    r"(?:`([^`\n]+)`|\"([^\"\n]+)\"|([A-Za-z0-9_./:-]+))"
)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,48}")
_TRAILING_PUNCT = ".,:;)]}'\"`!?"


def _validated_project_id(project_id: str) -> str:
    pid = sanitize_project_id(project_id)
    if not pid or not explicit_project_id_ok(pid):
        raise ValueError(f"invalid project_id for closure brief: {project_id!r}")
    return pid


def _clean_line(text: Any, *, max_chars: int = 200) -> str:
    plain = " ".join(strip_markdown(str(text or "")).split())
    if len(plain) <= max_chars:
        return plain
    return plain[: max(1, max_chars - 1)].rstrip() + "…"


def _dedupe(items: Iterable[str], *, limit: int) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in items:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _flatten_meta_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [s for item in value for s in _flatten_meta_values(item)]
    return [str(value).strip()] if str(value).strip() else []


def _read_existing_frontmatter(path: pathlib.Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            try:
                parsed = yaml.safe_load("".join(lines[1:idx]))
                return parsed if isinstance(parsed, dict) else {}
            except yaml.YAMLError:
                return {}
    return {}


def _read_workpad(store_dir: pathlib.Path) -> str:
    path = store_dir / "workpad.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_journal(store_dir: pathlib.Path) -> List[Dict[str, Any]]:
    path = store_dir / "journal.jsonl"
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and str(row.get("text") or "").strip():
                rows.append(row)
    except OSError:
        return []
    return rows


def _read_knowledge_notes(root: pathlib.Path, store_dir: pathlib.Path, project_id: str) -> List[Dict[str, Any]]:
    shelf = store_dir / "knowledge"
    if not shelf.is_dir():
        return []
    try:
        shelf_resolved = shelf.resolve()
        root_resolved = root.resolve()
        candidates = sorted(shelf.rglob("*.md"))
    except OSError:
        return []
    notes: List[Dict[str, Any]] = []
    for path in candidates:
        if path == shelf / INDEX_FILE or not path.is_file():
            continue
        try:
            topic = sanitize_topic(path.relative_to(shelf).as_posix()[:-3])
            path.resolve().relative_to(shelf_resolved)
            note = read_knowledge_note(KnowledgeAddress(root_resolved, shelf_resolved, topic, project_id))
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        body = (
            note.source.text_at(note.source.body_span).strip()
            if note.source is not None
            else note.text.strip()
        )
        notes.append({
            "topic": topic,
            "title": note.title or topic,
            "summary": note.summary or "",
            "state": note.state,
            "metadata": dict(note.metadata or {}),
            "body": body,
        })
    return notes


def _read_chronicle_records(root: pathlib.Path, chat_id: int) -> List[Dict[str, Any]]:
    if not chat_id:
        return []
    store = ChronicleStore(root)
    if not store.log_path.is_file():
        return []
    try:
        with chronicle_capture(root, tail="none"):
            return list(store.room_records(str(chat_id)))
    except (OSError, ValueError, sqlite3.DatabaseError, TimeoutError) as exc:
        log.debug("project_closure: chronicle read skipped for chat %s: %s", chat_id, exc)
        return []


def _extract_touched_paths(row: Mapping[str, Any], texts: Iterable[str]) -> List[str]:
    found: List[str] = []
    working_dir = str(row.get("working_dir") or "").strip()
    if working_dir:
        found.append(working_dir)
    for res in row.get("resources") or ():
        if isinstance(res, Mapping):
            p = str(res.get("path") or "").strip()
            if p:
                found.append(p)
    try:
        for entry in project_footprint.entries(dict(row)):
            p = str(entry.get("path") or "").strip()
            if p:
                found.append(p)
    except ValueError:
        pass
    for text in texts:
        if not text:
            continue
        for regex in (_ABS_PATH_RE, _REL_PATH_RE):
            for match in regex.finditer(text):
                candidate = match.group(1).rstrip(_TRAILING_PUNCT)
                if candidate and not candidate.endswith("/"):
                    found.append(candidate)
    return _dedupe(found, limit=_MAX_PATHS)


def _valid_commit_sha(token: str, excluded_ids: Set[str]) -> bool:
    low = token.lower()
    if not (7 <= len(low) <= 40) or low in excluded_ids:
        return False
    if low.isdigit() or low in _HEX_ENGLISH_WORDS:
        return False
    if not any(ch in "abcdef" for ch in low):
        return False
    return True


def _extract_commit_shas(
    texts: Iterable[str],
    knowledge_notes: Iterable[Mapping[str, Any]],
    excluded_ids: Set[str],
) -> List[str]:
    found: List[str] = []
    for note in knowledge_notes:
        meta = note.get("metadata") if isinstance(note.get("metadata"), Mapping) else {}
        for key in ("commit_shas", "commits"):
            for raw in _flatten_meta_values(meta.get(key)):
                tok = raw.strip("`").lower()
                if _valid_commit_sha(tok, excluded_ids):
                    found.append(tok)
    for text in texts:
        if not text:
            continue
        for match in _COMMIT_CTX_RE.finditer(text):
            tok = match.group(1).lower()
            if _valid_commit_sha(tok, excluded_ids):
                found.append(tok)
        for match in _COMMIT_LIST_RE.finditer(text):
            for sha_m in _BARE_SHA_RE.finditer(match.group(1)):
                tok = sha_m.group(1).lower()
                if _valid_commit_sha(tok, excluded_ids):
                    found.append(tok)
        for match in _FULL_SHA1_RE.finditer(text):
            tok = match.group(1).lower()
            if _valid_commit_sha(tok, excluded_ids) and any(ch.isdigit() for ch in tok):
                found.append(tok)
    return _dedupe(found, limit=_MAX_SHAS)


def _extract_error_signatures(
    row: Mapping[str, Any],
    journal_rows: Iterable[Mapping[str, Any]],
    texts: Iterable[str],
) -> List[str]:
    found: List[str] = []
    delete_error = _clean_line(row.get("delete_error") or "", max_chars=120)
    if delete_error:
        found.append(f"delete_error: {delete_error}")
    for entry in journal_rows:
        if str(entry.get("kind") or "").strip().lower() == "blocked":
            excerpt = _clean_line(entry.get("text") or "", max_chars=120)
            if excerpt:
                found.append(f"blocked: {excerpt}")
    for text in texts:
        if not text:
            continue
        for match in _STRUCTURED_ERR_RE.finditer(text):
            found.append(match.group(0))
    return _dedupe(found, limit=_MAX_ERRORS)


def _extract_supersedes(
    knowledge_notes: Iterable[Mapping[str, Any]],
    texts: Iterable[str],
) -> List[str]:
    found: List[str] = []
    for note in knowledge_notes:
        meta = note.get("metadata") if isinstance(note.get("metadata"), Mapping) else {}
        for key in ("supersedes", "replaces"):
            found.extend(_flatten_meta_values(meta.get(key)))
    for text in texts:
        if not text:
            continue
        for match in _SUPERSEDES_RE.finditer(text):
            raw_ref = match.group(1) or match.group(2) or match.group(3) or ""
            ref = raw_ref.strip().rstrip(_TRAILING_PUNCT)
            if ref and ref.lower() not in {"the", "a", "an", "this", "that", "it", "prior", "old", "previous"}:
                found.append(ref)
    return _dedupe(found, limit=_MAX_SUPERSEDES)


def _extract_keywords(
    pid: str,
    name: str,
    row: Mapping[str, Any],
    journal_rows: Iterable[Mapping[str, Any]],
    knowledge_notes: Iterable[Mapping[str, Any]],
    chronicle_records: Iterable[Mapping[str, Any]],
) -> List[str]:
    found: List[str] = [pid]
    for tok in _WORD_TOKEN_RE.findall(name):
        low = tok.lower()
        if low not in _STOPWORDS:
            found.append(low)
    last_task = str(row.get("last_task_result_id") or "").strip()
    if last_task:
        found.append(last_task)
    for entry in journal_rows:
        kind = str(entry.get("kind") or "").strip().lower()
        if kind:
            found.append(kind)
        tid = str(entry.get("task_id") or "").strip()
        if tid:
            found.append(tid)
    for note in knowledge_notes:
        topic = str(note.get("topic") or "").strip()
        if topic:
            found.append(topic)
        meta = note.get("metadata") if isinstance(note.get("metadata"), Mapping) else {}
        for key in ("tags", "keywords", "type"):
            found.extend(_flatten_meta_values(meta.get(key)))
    for rec in chronicle_records:
        tid = str(rec.get("task_id") or "").strip()
        if tid:
            found.append(tid)
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), Mapping) else {}
        label = str(meta.get("label") or "").strip()
        if label:
            found.append(label)
    return _dedupe(found, limit=_MAX_KEYWORDS)


def _first_workpad_line(workpad: str) -> str:
    for line in workpad.splitlines():
        cleaned = _clean_line(line, max_chars=180)
        if cleaned:
            return cleaned
    return ""


def _build_title(
    name: str,
    row: Mapping[str, Any],
    workpad: str,
    journal_rows: List[Dict[str, Any]],
) -> str:
    desc = _clean_line(row.get("description") or "", max_chars=90)
    subtitle = desc
    if not subtitle:
        wp_head = _first_workpad_line(workpad)
        if wp_head and wp_head.casefold() != name.casefold():
            subtitle = _clean_line(wp_head, max_chars=80)
    if not subtitle:
        done_rows = [r for r in journal_rows if str(r.get("kind") or "").lower() == "done"]
        if done_rows:
            subtitle = _clean_line(done_rows[-1].get("text") or "", max_chars=80)
    if not subtitle:
        subtitle = "Closure Brief"
    if subtitle.casefold() == name.casefold():
        return _clean_line(name, max_chars=_MAX_TITLE_CHARS)
    if subtitle.casefold().startswith(name.casefold()):
        rest = subtitle[len(name):]
        if not rest or rest[0] in (" ", "—", "-", ":"):
            return _clean_line(subtitle, max_chars=_MAX_TITLE_CHARS)
    return _clean_line(f"{name} — {subtitle}", max_chars=_MAX_TITLE_CHARS)


def _build_summary(
    pid: str,
    name: str,
    row: Mapping[str, Any],
    workpad: str,
    journal_rows: List[Dict[str, Any]],
    knowledge_notes: List[Dict[str, Any]],
    chronicle_records: List[Dict[str, Any]],
) -> str:
    parts: List[str] = []
    seen_norm: Set[str] = set()

    def _add(raw: Any, limit: int = 240) -> None:
        text = _clean_line(raw, max_chars=limit)
        if not text:
            return
        key = text.casefold()
        if key in seen_norm or any(key in prior or prior in key for prior in seen_norm):
            return
        seen_norm.add(key)
        parts.append(text)

    _add(row.get("description") or "", limit=220)
    if not parts and row.get("notes"):
        first_para = str(row["notes"]).strip().split("\n\n", 1)[0]
        _add(first_para, limit=240)

    done_rows = [r for r in journal_rows if str(r.get("kind") or "").lower() == "done"]
    if done_rows:
        _add(done_rows[-1].get("text") or "", limit=240)
    elif journal_rows:
        _add(journal_rows[-1].get("text") or "", limit=220)

    if workpad and len(parts) < 2:
        _add(_first_workpad_line(workpad), limit=200)

    if chronicle_records and len(parts) < 3:
        preferred = [r for r in chronicle_records if r.get("kind") in ("part", "page", "legacy", "note")]
        target = preferred[-1] if preferred else chronicle_records[-1]
        _add(target.get("current_text") or target.get("text") or "", limit=220)

    if not parts and knowledge_notes:
        _add(knowledge_notes[0].get("summary") or knowledge_notes[0].get("title") or "", limit=220)

    if not parts:
        return (
            f"Closed project {name} ({pid}) with no recorded workpad, journal, "
            "knowledge, or chronicle entries."
        )
    joined = " ".join(parts)
    return _clean_line(joined, max_chars=_MAX_SUMMARY_CHARS)


def build_closure_brief_data(drive_root: Any, project_id: str) -> Dict[str, Any]:
    """Assemble structured closure-brief fields from the project's durable records."""
    pid = _validated_project_id(project_id)
    root = pathlib.Path(drive_root)
    row = projects_registry.get_reserved_project(root, pid, strict=False) or {}
    name = project_display_name(row.get("name"), pid) or pid
    chat_id = int(row.get("chat_id") or 0) or int(project_chat_id(pid))
    closed_at = str(
        row.get("tombstoned_at")
        or row.get("admission_closed_at")
        or row.get("last_active_at")
        or utc_now_iso()
    )

    store_dir = root / "projects" / pid
    workpad = _read_workpad(store_dir)
    journal_rows = _read_journal(store_dir)
    knowledge_notes = _read_knowledge_notes(root, store_dir, pid)
    chronicle_records = _read_chronicle_records(root, chat_id)

    all_texts: List[str] = [
        str(row.get("description") or ""),
        str(row.get("notes") or ""),
        workpad,
        *(str(r.get("text") or "") for r in journal_rows),
        *(f"{n['title']}\n{n['summary']}\n{n['body']}" for n in knowledge_notes),
        *(str(rec.get("current_text") or rec.get("text") or "") for rec in chronicle_records),
    ]

    excluded_ids: Set[str] = set()
    for val in (row.get("last_task_result_id"), row.get("routing_incarnation")):
        if isinstance(val, str) and val.strip():
            excluded_ids.add(val.strip().lower())
    for entry in journal_rows:
        tid = str(entry.get("task_id") or "").strip().lower()
        if tid:
            excluded_ids.add(tid)
    for rec in chronicle_records:
        for key in ("id", "task_id"):
            val = str(rec.get(key) or "").strip().lower()
            if val:
                excluded_ids.add(val)

    title = _build_title(name, row, workpad, journal_rows)
    summary = _build_summary(pid, name, row, workpad, journal_rows, knowledge_notes, chronicle_records)
    keywords = _extract_keywords(pid, name, row, journal_rows, knowledge_notes, chronicle_records)
    touched_paths = _extract_touched_paths(row, all_texts)
    commit_shas = _extract_commit_shas(all_texts, knowledge_notes, excluded_ids)
    error_signatures = _extract_error_signatures(row, journal_rows, all_texts)
    supersedes = _extract_supersedes(knowledge_notes, all_texts)

    highlights: List[str] = []
    for entry in journal_rows[-5:]:
        kind = str(entry.get("kind") or "note").strip()
        ts = str(entry.get("ts") or "")[:10]
        excerpt = _clean_line(entry.get("text") or "", max_chars=180)
        if excerpt:
            prefix = f"[{kind}{' ' + ts if ts else ''}]"
            highlights.append(f"{prefix} {excerpt}")
    if not highlights and chronicle_records:
        for rec in chronicle_records[-3:]:
            kind = str(rec.get("kind") or "record")
            excerpt = _clean_line(rec.get("current_text") or rec.get("text") or "", max_chars=180)
            if excerpt:
                highlights.append(f"[{kind}] {excerpt}")

    return {
        "schema_version": CLOSURE_BRIEF_SCHEMA_VERSION,
        "project_id": pid,
        "chat_id": chat_id,
        "closed_at": closed_at,
        "authored_by": CLOSURE_BRIEF_AUTHOR,
        "title": title,
        "summary": summary,
        "keywords": keywords,
        "touched_paths": touched_paths,
        "commit_shas": commit_shas,
        "error_signatures": error_signatures,
        "supersedes": supersedes,
        "highlights": highlights,
        "knowledge_topics": [n["topic"] for n in knowledge_notes],
    }


def render_closure_brief(data: Mapping[str, Any]) -> str:
    """Render closure-brief data as YAML frontmatter followed by structured Markdown."""
    frontmatter: Dict[str, Any] = {
        "schema_version": int(data.get("schema_version") or CLOSURE_BRIEF_SCHEMA_VERSION),
        "project_id": str(data.get("project_id") or ""),
        "chat_id": int(data.get("chat_id") or 0),
        "closed_at": str(data.get("closed_at") or ""),
        "authored_by": str(data.get("authored_by") or CLOSURE_BRIEF_AUTHOR),
        "title": str(data.get("title") or ""),
        "summary": str(data.get("summary") or ""),
        "keywords": [str(x) for x in (data.get("keywords") or ())],
        "touched_paths": [str(x) for x in (data.get("touched_paths") or ())],
        "commit_shas": [str(x) for x in (data.get("commit_shas") or ())],
        "error_signatures": [str(x) for x in (data.get("error_signatures") or ())],
        "supersedes": [str(x) for x in (data.get("supersedes") or ())],
    }
    fm_yaml = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    ).strip()

    lines: List[str] = [
        "---",
        fm_yaml,
        "---",
        "",
        f"# {frontmatter['title'] or frontmatter['project_id'] or 'Closure Brief'}",
        "",
        frontmatter["summary"],
    ]

    def _add_bullets(heading: str, items: Iterable[str], *, code: bool = False) -> None:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            return
        lines.extend(["", f"## {heading}", ""])
        for item in cleaned:
            lines.append(f"- `{item}`" if code else f"- {item}")

    _add_bullets("Touched Paths", frontmatter["touched_paths"], code=True)
    _add_bullets("Commits", frontmatter["commit_shas"], code=True)
    _add_bullets("Error Signatures & Blockers", frontmatter["error_signatures"])
    _add_bullets("Supersedes", frontmatter["supersedes"])
    _add_bullets("Knowledge Topics", data.get("knowledge_topics") or (), code=True)
    _add_bullets("Highlights", data.get("highlights") or ())

    return "\n".join(lines).rstrip() + "\n"


def generate_closure_brief(drive_root: Any, project_id: str) -> str:
    """Build and render a project's closure brief Markdown without writing to disk."""
    return render_closure_brief(build_closure_brief_data(drive_root, project_id))


def _sync_fts_best_effort(root: pathlib.Path) -> bool:
    try:
        memory_index.sync_index(root)
        return True
    except Exception as exc:
        log.warning("project_closure: best-effort memory_index sync failed: %s", exc)
        return False


def write_closure_brief(
    drive_root: Any,
    project_id: str,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    sync_fts: bool = True,
) -> pathlib.Path:
    """Persist `projects/<id>/closure_brief.md` atomically and optionally sync `memory_index.sqlite`.

    When `overwrite=False`:
    - preserves manually authored briefs (`authored_by != "project_close"`);
    - refreshes auto-generated `"project_close"` briefs on re-close unless `skip_existing=True`.
    """
    pid = _validated_project_id(project_id)
    root = pathlib.Path(drive_root)
    path = project_closure_brief_path(pid, drive_root=root)

    should_write = True
    if path.is_file() and not overwrite:
        if skip_existing:
            should_write = False
        else:
            existing_fm = _read_existing_frontmatter(path)
            if str(existing_fm.get("authored_by") or "").strip() != CLOSURE_BRIEF_AUTHOR:
                should_write = False

    if should_write:
        rendered = generate_closure_brief(root, pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, rendered)

    if sync_fts:
        _sync_fts_best_effort(root)
    return path


def backfill_closed_project_briefs(
    drive_root: Any,
    *,
    overwrite: bool = False,
    sync_fts: bool = True,
) -> Dict[str, Any]:
    """Generate `closure_brief.md` for all tombstoned projects missing one (or all if `overwrite=True`)."""
    root = pathlib.Path(drive_root)
    rows = projects_registry.list_reserved_projects(root, strict=False)
    closed_ids = [
        pid
        for row in rows
        if isinstance(row, Mapping)
        and row.get("lifecycle") == projects_registry.PROJECT_TOMBSTONED
        and (pid := sanitize_project_id(row.get("id")))
        and explicit_project_id_ok(pid)
    ]

    written: List[str] = []
    skipped: List[str] = []
    for pid in closed_ids:
        path = project_closure_brief_path(pid, drive_root=root)
        if path.is_file() and not overwrite:
            skipped.append(pid)
            continue
        write_closure_brief(root, pid, overwrite=overwrite, skip_existing=not overwrite, sync_fts=False)
        written.append(pid)

    indexed = _sync_fts_best_effort(root) if sync_fts else False
    return {
        "total_closed": len(closed_ids),
        "written": written,
        "skipped": skipped,
        "indexed": indexed,
    }
