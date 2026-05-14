from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from runtime.kernel.kernel_models import CheckpointRef, KernelEvent, now_ts_ms
from runtime.kernel.kernel_projectors import event_to_public_event
from runtime.kernel.kernel_store import KernelStore, json_ready

_RUNS: Dict[str, Dict[str, Any]] = {}
_EVENTS: Dict[str, List[KernelEvent]] = {}
_CHECKPOINTS: Dict[str, List[Dict[str, Any]]] = {}


class InMemoryKernelStore(KernelStore):
    async def upsert_run(self, run_id: str, payload: Dict[str, Any]) -> None:
        _RUNS[run_id] = copy.deepcopy(json_ready(payload))

    async def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = _RUNS.get(run_id)
        return copy.deepcopy(row) if row is not None else None

    async def append_event(self, event: KernelEvent) -> int:
        rows = _EVENTS.setdefault(event.run_id, [])
        if int(event.seq or 0) <= 0:
            event.seq = len(rows) + 1
        rows.append(copy.deepcopy(event))
        return event.seq

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> List[KernelEvent]:
        rows = _EVENTS.get(run_id, [])
        return [copy.deepcopy(ev) for ev in rows if int(ev.seq or 0) > int(after_seq or 0)]

    async def load_public_events(self, run_id: str, *, after_seq: int = 0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ev in _EVENTS.get(run_id, []):
            if int(ev.seq or 0) <= int(after_seq or 0):
                continue
            public_event = event_to_public_event(ev)
            if isinstance(public_event, dict):
                out.append(copy.deepcopy(json_ready(public_event)))
        return out

    async def save_checkpoint(
        self,
        run_id: str,
        *,
        seq: int,
        node_id: str,
        label: str,
        state: Dict[str, Any],
    ) -> CheckpointRef:
        rows = _CHECKPOINTS.setdefault(run_id, [])
        ref = CheckpointRef(
            run_id=run_id,
            checkpoint_id=f"mem:{run_id}:{len(rows) + 1}",
            seq=int(seq or 0),
            ts_ms=now_ts_ms(),
            node_id=str(node_id or ""),
            label=str(label or "checkpoint"),
        )
        rows.append({"ref": ref.to_dict(), "state": copy.deepcopy(json_ready(state))})
        return ref

    async def load_latest_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = _CHECKPOINTS.get(run_id) or []
        if not rows:
            return None
        return copy.deepcopy(rows[-1])
