"""Server-Sent Events stream for live dashboard updates."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory event bus — recent events for new subscribers + live push
_subscribers: list[asyncio.Queue] = []
_recent: deque = deque(maxlen=50)


def publish(event_type: str, data: dict) -> None:
    """Push an event to all SSE subscribers."""
    event = {"type": event_type, **data}
    _recent.append(event)
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


async def _event_generator(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events."""
    # Send recent events as catchup
    for event in _recent:
        yield f"data: {json.dumps(event)}\n\n"

    # Stream live events
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=30)
            yield f"data: {json.dumps(event)}\n\n"
        except asyncio.TimeoutError:
            # Send keepalive
            yield ": keepalive\n\n"
        except asyncio.CancelledError:
            break


@router.get("/api/stream")
async def stream():
    """SSE endpoint for live container status updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(queue)

    async def cleanup_generator():
        try:
            async for chunk in _event_generator(queue):
                yield chunk
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        cleanup_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
