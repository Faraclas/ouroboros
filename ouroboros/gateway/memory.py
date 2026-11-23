"""Thin read-only gateway search route over the derived FTS5 memory index."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Dict, List, TypedDict

from starlette.requests import Request

from ouroboros import memory_index
from ouroboros.gateway._helpers import UTF8JSONResponse as JSONResponse
from ouroboros.gateway._helpers import request_drive_root

log = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"", "0", "false", "no", "off"})


class MemorySearchHit(TypedDict):
    doc_key: str
    family: str
    kind: str
    status: str
    scope: str
    date_ymd: str
    canonical_address: str
    badge: str
    title: str
    snippet: str
    score: float


class MemorySearchResponse(TypedDict):
    """GET /api/memory/search: ranked FTS5 search over state/memory_index.sqlite."""

    query: str
    scope: str
    include_archived: bool
    limit: int
    hit_count: int
    elapsed_ms: int
    rebuilt: bool
    hits: List[MemorySearchHit]


def _parse_bool_param(raw: Any, *, name: str) -> bool:
    token = str(raw if raw is not None else "").strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"{name} must be true or false")


def _parse_limit_param(raw: Any, *, default: int = 10) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer between 1 and 50") from exc
    if not (1 <= value <= 50):
        raise ValueError("limit must be between 1 and 50")
    return value


def _serialize_hit(hit: memory_index.SearchHit) -> Dict[str, Any]:
    date_label = hit.date_ymd or "unknown"
    badge = f"[{hit.status} | {date_label} | {hit.scope} | {hit.canonical_address}]"
    return {
        "doc_key": hit.doc_key,
        "family": hit.family,
        "kind": hit.kind,
        "status": hit.status,
        "scope": hit.scope,
        "date_ymd": hit.date_ymd,
        "canonical_address": hit.canonical_address,
        "badge": badge,
        "title": hit.title,
        "snippet": hit.snippet,
        "score": round(float(hit.score), 6),
    }


async def api_memory_search(request: Request) -> JSONResponse:
    """GET /api/memory/search — thin read-only ranked FTS5 search over ``state/memory_index.sqlite``."""
    params = request.query_params
    raw_query = str(params.get("query") or params.get("q") or "").strip()
    if not raw_query:
        return JSONResponse({"error": "query parameter is required"}, status_code=400, headers=_NO_STORE)

    raw_scope = str(params.get("scope") or "").strip()
    try:
        scope = memory_index.validate_scope(raw_scope)
        include_archived = _parse_bool_param(params.get("include_archived"), name="include_archived")
        limit = _parse_limit_param(params.get("limit"), default=10)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400, headers=_NO_STORE)

    drive_root = request_drive_root(request)
    try:
        result = await asyncio.to_thread(
            memory_index.search_readonly,
            drive_root,
            raw_query,
            scope=scope,
            include_archived=include_archived,
            limit=limit,
            allowed_project_id=None,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400, headers=_NO_STORE)
    except (OSError, sqlite3.Error) as exc:
        log.warning("api_memory_search failed: %s", exc)
        return JSONResponse({"error": "Memory index unavailable"}, status_code=503, headers=_NO_STORE)
    if result is None:
        return JSONResponse(
            {"error": "Memory index is not built yet.", "reason_code": "index_not_built"},
            status_code=503,
            headers=_NO_STORE,
        )

    hits: List[Dict[str, Any]] = [_serialize_hit(hit) for hit in result.hits]
    payload: Dict[str, Any] = {
        "query": result.query,
        "scope": result.scope,
        "include_archived": result.include_archived,
        "limit": result.limit,
        "hit_count": result.hit_count,
        "elapsed_ms": result.elapsed_ms,
        "rebuilt": result.rebuilt,
        "hits": hits,
    }
    return JSONResponse(payload, headers=_NO_STORE)


__all__ = ["MemorySearchHit", "MemorySearchResponse", "api_memory_search"]
