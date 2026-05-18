"""规格路径导入：实现仍在根目录 routing_signals.py（历史遗留；后续可原地迁移）。"""
from __future__ import annotations

from routing_signals import (  # noqa: F401
    detect_high_risk_domain,
    detect_search_sensitivity,
    derive_user_signals,
    merge_signals_into_analysis,
    reasoning_keyword_boost,
    FAST_PHRASES,
    DEEP_PHRASES,
)

__all__ = [
    "detect_high_risk_domain",
    "detect_search_sensitivity",
    "derive_user_signals",
    "merge_signals_into_analysis",
    "reasoning_keyword_boost",
    "FAST_PHRASES",
    "DEEP_PHRASES",
]
