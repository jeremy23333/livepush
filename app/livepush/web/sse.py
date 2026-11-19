"""SSE：把状态变化实时推给浏览器。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..monitor.events import bus
from .auth import require_user

router = APIRouter(prefix="/api", tags=["stream"], dependencies=[Depends(require_user)])


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    async def generator():
        yield "retry: 3000\n\n"
        async with bus.subscribe() as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
