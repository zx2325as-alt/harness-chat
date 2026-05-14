"""对外 SSE：保持 step 结构稳定，不再做 legacy refine/layer 名称映射。"""
from __future__ import annotations

from typing import Any, Dict


def normalize_step_for_client(step: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(step, dict):
        return step
    return dict(step)


def normalize_stream_event_step(event: Dict[str, Any]) -> Dict[str, Any]:
    if str(event.get("event") or "") != "step":
        return event
    step = event.get("step")
    if isinstance(step, dict):
        return {**event, "step": normalize_step_for_client(dict(step))}
    return event
