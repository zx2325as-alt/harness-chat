"""Router 视图占位：模型路由委托 RuntimeHarness.resolve_model_route。"""
from __future__ import annotations

from typing import Any, Dict


def snapshot_model_route(harness: Any, prompt: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
    return harness.resolve_model_route(prompt, analysis)
