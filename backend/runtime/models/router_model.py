"""Router 视图占位：模型路由委托 DualTrackHarness.route_fast_model / routing 模板。"""
from __future__ import annotations

from typing import Any, Dict


def snapshot_route_fast(harness: Any, prompt: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
    return harness.route_fast_model(prompt, analysis)
