"""对外 SSE：隐藏 layer2 等内部命名，保持前端协议稳定。"""
from __future__ import annotations

from typing import Any, Dict

_STEP_NAME_PUBLIC: Dict[str, str] = {
    "refine_layer2_review": "refine_quality_review",
    "refine_layer1_draft": "refine_draft",
    "refine_layer3_polish": "refine_finalize",
}


def normalize_step_for_client(step: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(step, dict):
        return step
    out = dict(step)
    name = str(out.get("name") or "")
    pub = _STEP_NAME_PUBLIC.get(name)
    if pub:
        out["internal_step_name"] = name
        out["name"] = pub
    elif "layer2" in name.lower():
        out["internal_step_name"] = name
        out["name"] = name.lower().replace("layer2", "runtime_stage")
    return out


def normalize_stream_event_step(event: Dict[str, Any]) -> Dict[str, Any]:
    if str(event.get("event") or "") != "step":
        return event
    step = event.get("step")
    if isinstance(step, dict):
        return {**event, "step": normalize_step_for_client(dict(step))}
    return event
