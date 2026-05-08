"""
Fast 轨后置质量门开关与缓存策略（评分逻辑见 unified_critic.evaluate_fast_answer）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _orch_cfg(hcfg: Dict[str, Any]) -> Dict[str, Any]:
    return (hcfg.get("runtime_orchestrator") or {}) if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}


def runtime_orchestrator_enabled(hcfg: Dict[str, Any]) -> bool:
    cfg = _orch_cfg(hcfg)
    return bool(cfg.get("enabled", True))


def fast_gate_enabled(hcfg: Dict[str, Any]) -> bool:
    cfg = _orch_cfg(hcfg)
    stages = cfg.get("rollout_stages") if isinstance(cfg.get("rollout_stages"), dict) else {}
    if stages.get("fast_quality_gate") is False:
        return False
    gate = cfg.get("fast_quality_gate") if isinstance(cfg.get("fast_quality_gate"), dict) else {}
    return bool(gate.get("enabled", True)) and runtime_orchestrator_enabled(hcfg)


def should_skip_fast_cache_store(
    analysis: Dict[str, Any],
    hcfg: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> bool:
    """命中下列情形时：既不写入也不读取快轨答案缓存（与文档「禁止缓存」一致）。"""
    opts = options or {}
    orch = _orch_cfg(hcfg)
    gate = orch.get("fast_quality_gate") if isinstance(orch.get("fast_quality_gate"), dict) else {}
    if not bool(gate.get("disable_cache_on_high_risk", True)):
        return False
    if str(opts.get("_runtime_track") or "").lower() == "agent":
        return True
    if str(analysis.get("decision") or "").lower() == "agent":
        return True
    si = str(analysis.get("search_intent") or "none").lower()
    if si == "freshness_required":
        return True
    if analysis.get("high_risk_domain"):
        return True
    return False
