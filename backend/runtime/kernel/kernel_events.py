from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from runtime.kernel.kernel_models import KernelEvent, now_ts_ms


class KernelEventPublisher:
    def __init__(self) -> None:
        self._subs: List[asyncio.Queue] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    async def publish(self, event: Any) -> None:
        if isinstance(event, KernelEvent):
            event.ts_ms = int(event.ts_ms or now_ts_ms())
        stale: List[asyncio.Queue] = []
        for q in self._subs:
            try:
                await q.put(event)
            except RuntimeError:
                stale.append(q)
        if stale:
            self._subs = [q for q in self._subs if q not in stale]

    async def close(self, sentinel: Any) -> None:
        if self._closed:
            return
        self._closed = True
        await self.publish(sentinel)


def make_kernel_event(
    *,
    run_id: str,
    trace_id: str,
    event_type: str,
    node_id: str = "",
    phase: str = "",
    attempt: int = 0,
    payload: Optional[Dict[str, Any]] = None,
    public_event: Optional[Dict[str, Any]] = None,
) -> KernelEvent:
    return KernelEvent(
        run_id=run_id,
        trace_id=trace_id,
        event_type=event_type,
        node_id=str(node_id or ""),
        phase=str(phase or ""),
        attempt=max(0, int(attempt or 0)),
        payload=dict(payload or {}),
        public_event=dict(public_event) if isinstance(public_event, dict) else None,
    )
