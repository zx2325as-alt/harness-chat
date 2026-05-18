"""规格路径导入：实现仍在根目录 unified_critic.py（历史遗留；后续可原地迁移）。"""
from __future__ import annotations

from unified_critic import (  # noqa: F401
    RECOMMENDED_ACTIONS,
    evaluate_unified_critic,
    evaluate_structured_quality_critic,
    normalize_recommended,
    verify_answer,
)

__all__ = [
    "RECOMMENDED_ACTIONS",
    "evaluate_unified_critic",
    "evaluate_structured_quality_critic",
    "normalize_recommended",
    "verify_answer",
]
