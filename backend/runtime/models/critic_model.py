"""Critic 模型键占位：真实候选来自 unified_critic 与 routing 默认池。"""
from __future__ import annotations

from typing import Any, Dict, List


def list_critic_model_keys(harness: Any, hcfg: Dict[str, Any]) -> List[str]:
    del harness, hcfg
    return []
