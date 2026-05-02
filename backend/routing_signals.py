"""从用户文案与 options 推导 search_intent / output_intent，供选轨与联网策略使用。"""
from __future__ import annotations

from typing import Any, Dict


def _norm_si(v: str) -> str:
    v = (v or "").strip().lower()
    if v in ("none", "optional", "explicit", "required", "freshness_required"):
        return v
    return ""


def derive_user_signals(prompt: str, options: Dict[str, Any]) -> Dict[str, Any]:
    text = (prompt or "").strip()
    low = text.lower()

    si = _norm_si(str(options.get("search_intent") or ""))
    if not si:
        # 强联网 / 禁止编造
        if any(k in text for k in ("必须联网", "一定要查", "务必查证", "禁止编造", "不能瞎编", "必须查证", "必须检索")):
            si = "required"
        elif any(k in text for k in ("实时", "此刻", "今天的新闻", "最新股价", "最新汇率")) or any(
            k in low for k in ("real-time", "breaking news", "latest price")
        ):
            si = "freshness_required"
        elif any(
            k in text
            for k in ("联网", "查证", "核实", "最新", "检索", "搜索", "查一下", "上网查", "网上查")
        ) or any(k in low for k in ("web search", "search the web", "look up online")):
            si = "explicit"
        elif str(options.get("search_mode") or "").lower() in ("on", "true", "1", "force", "always"):
            si = "explicit"
        else:
            si = "none"

    oi = str(options.get("output_intent") or "").strip().lower()
    if oi not in ("neutral", "fast", "deep"):
        oi = "neutral"
    if oi == "neutral":
        if any(k in text for k in ("快速回答", "简单说", "一句话", "尽量短", "简要", "短答")):
            oi = "fast"
        if any(
            k in text
            for k in ("深入分析", "严谨审查", "正式输出", "详细论证", "完整推导", "逐步分析", "长篇", "系统阐述")
        ):
            oi = "deep"
        # 若同时命中，深优先（审查类任务更重要）
        if oi == "fast" and any(
            k in text for k in ("深入分析", "严谨审查", "正式输出", "详细论证", "完整推导", "逐步分析")
        ):
            oi = "deep"

    return {
        "search_intent": si,
        "output_intent": oi,
    }


def merge_signals_into_analysis(analysis: Dict[str, Any], signals: Dict[str, Any]) -> Dict[str, Any]:
    out = {**analysis}
    out["search_intent"] = signals.get("search_intent", "none")
    out["output_intent"] = signals.get("output_intent", "neutral")
    out["_routing_signals"] = signals
    return out
