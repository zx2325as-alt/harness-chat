"""SSE / Runtime 事件形状辅助。"""
from __future__ import annotations

from typing import Any, Dict

from refine_shared import _pg
from runtime.dag_common import user_status


def status_event(message: str, phase: str = "dag") -> Dict[str, Any]:
    return user_status(message, phase=phase)


def step_meta(phase_group: str, summary: str, meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _pg(meta or {}, phase_group, summary)


__all__ = ["status_event", "step_meta", "user_status"]
