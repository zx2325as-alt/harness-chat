from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from runtime.kernel.kernel_checkpoints import snapshot_execution_state
from runtime.kernel.kernel_events import make_kernel_event
from runtime.kernel.kernel_models import RunStatus
from runtime.kernel.kernel_projectors import public_event_from_legacy


class KernelRunner:
    def __init__(self, *, store: Any, publisher: Any) -> None:
        self.store = store
        self.publisher = publisher
        self._seq_by_run: Dict[str, int] = {}

    def next_seq(self, run_id: str) -> int:
        seq = int(self._seq_by_run.get(run_id, 0)) + 1
        self._seq_by_run[run_id] = seq
        return seq

    async def publish_event(
        self,
        *,
        run_id: str,
        trace_id: str,
        event_type: str,
        node_id: str = "",
        phase: str = "",
        attempt: int = 0,
        payload: Optional[Dict[str, Any]] = None,
        public_event: Optional[Dict[str, Any]] = None,
    ) -> int:
        ev = make_kernel_event(
            run_id=run_id,
            trace_id=trace_id,
            event_type=event_type,
            node_id=node_id,
            phase=phase,
            attempt=attempt,
            payload=payload,
            public_event=public_event,
        )
        ev.seq = self.next_seq(run_id)
        await self.store.append_event(ev)
        await self.publisher.publish(ev)
        return ev.seq

    async def publish_legacy_public_event(self, *, run_id: str, trace_id: str, event: Dict[str, Any], phase: str = "") -> int:
        seq = self.next_seq(run_id)
        ev = make_kernel_event(
            run_id=run_id,
            trace_id=trace_id,
            event_type=f"public.{str(event.get('event') or 'unknown')}",
            phase=phase,
            payload={"legacy_event": dict(event)},
            public_event=public_event_from_legacy(event, run_id, seq),
        )
        ev.seq = seq
        await self.store.append_event(ev)
        await self.publisher.publish(ev)
        return seq

    async def start_run(self, *, run_id: str, trace_id: str, payload: Dict[str, Any]) -> None:
        await self.store.upsert_run(run_id, {"run_id": run_id, "trace_id": trace_id, "status": RunStatus.RUNNING, **dict(payload or {})})
        await self.publish_event(run_id=run_id, trace_id=trace_id, event_type="run_started", payload=payload)

    async def complete_run(self, *, run_id: str, trace_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        current = await self.store.load_run(run_id) or {}
        current["status"] = RunStatus.COMPLETED
        current.update(dict(payload or {}))
        await self.store.upsert_run(run_id, current)
        await self.publish_event(run_id=run_id, trace_id=trace_id, event_type="run_completed", payload=payload or {})

    async def fail_run(self, *, run_id: str, trace_id: str, error: str, payload: Optional[Dict[str, Any]] = None) -> None:
        current = await self.store.load_run(run_id) or {}
        current["status"] = RunStatus.FAILED
        current["error"] = str(error or "runtime_failed")
        if payload:
            current.update(dict(payload))
        await self.store.upsert_run(run_id, current)
        await self.publish_event(
            run_id=run_id,
            trace_id=trace_id,
            event_type="run_failed",
            payload={"error": str(error or "runtime_failed"), **dict(payload or {})},
        )

    async def cancel_run(self, *, run_id: str, trace_id: str, error: str = "runtime_cancelled") -> None:
        current = await self.store.load_run(run_id) or {}
        current["status"] = RunStatus.CANCELLED
        current["error"] = str(error or "runtime_cancelled")
        await self.store.upsert_run(run_id, current)
        await self.publish_event(run_id=run_id, trace_id=trace_id, event_type="run_cancelled", payload={"error": current["error"]})

    async def checkpoint(self, *, run_id: str, trace_id: str, ctx: Any, node_id: str, label: str) -> Dict[str, Any]:
        seq = int(self._seq_by_run.get(run_id, 0))
        snap = snapshot_execution_state(ctx)
        ref = await self.store.save_checkpoint(run_id, seq=seq, node_id=node_id, label=label, state=snap)
        await self.publish_event(
            run_id=run_id,
            trace_id=trace_id,
            event_type="checkpoint_written",
            node_id=node_id,
            payload={"checkpoint": ref.to_dict(), "label": label},
        )
        return ref.to_dict()
