"""Passive room activity reads; live controls stay on the existing work projection."""
from __future__ import annotations
import asyncio
from starlette.responses import JSONResponse
from ouroboros.activity_history import room_activity_page
from ouroboros.gateway._helpers import request_drive_root
from ouroboros.store import StoreError


async def api_room_activity(request):
    try:
        room = int(request.path_params["chat_id"])
        limit = max(1, min(50, int(request.query_params.get("limit", 30))))
        page = await asyncio.to_thread(room_activity_page, request_drive_root(request), room,
                                       limit=limit, cursor=request.query_params.get("cursor", ""))
        return JSONResponse(page, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        code = "activity_view_changed" if str(exc) == "activity_view_changed" else "invalid_activity_request"
        return JSONResponse({"error": str(exc), "code": code}, status_code=409 if code == "activity_view_changed" else 400)
    except StoreError:
        return JSONResponse({"error": "Activity records are unavailable; retry this window", "code": "activity_unavailable"},
                            status_code=503, headers={"Retry-After": "1", "Cache-Control": "no-store"})
