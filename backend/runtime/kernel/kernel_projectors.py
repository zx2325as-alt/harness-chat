from __future__ import annotations

from typing import Any, Dict, Optional

from runtime.kernel.kernel_models import KernelEvent


def event_to_public_event(event: KernelEvent) -> Optional[Dict[str, Any]]:
    if isinstance(event.public_event, dict):
        out = dict(event.public_event)
        out.setdefault("run_id", event.run_id)
        out.setdefault("event_seq", int(event.seq or 0))
        return out

    if event.event_type == "run_completed":
        return {"event": "kernel_run_completed", "run_id": event.run_id, "event_seq": int(event.seq or 0)}
    if event.event_type == "run_failed":
        return {
            "event": "error_terminal",
            "error": str((event.payload or {}).get("error") or "runtime_failed"),
            "run_id": event.run_id,
            "trace_id": event.trace_id,
            "event_seq": int(event.seq or 0),
        }
    if event.event_type == "run_cancelled":
        return {
            "event": "error_terminal",
            "error": str((event.payload or {}).get("error") or "runtime_cancelled"),
            "run_id": event.run_id,
            "trace_id": event.trace_id,
            "event_seq": int(event.seq or 0),
        }
    return None


def public_event_from_legacy(ev: Dict[str, Any], run_id: str, seq: int) -> Dict[str, Any]:
    out = dict(ev)
    out.setdefault("run_id", run_id)
    out.setdefault("event_seq", seq)
    return out
