"""从用户文案与 options 推导 search_intent / output_intent，并与分析器 suggested_track 对齐。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

FAST_PHRASES = (
    "快速回答",
    "简单说",
    "一句话",
    "尽量短",
    "简要",
    "短答",
    "用简短的几句话",
    "别写太多",
    "直接给结论",
    "不要太长",
    "简明扼要",
)
DEEP_PHRASES = (
    "深入分析",
    "严谨审查",
    "正式输出",
    "详细论证",
    "完整推导",
    "逐步分析",
    "长篇",
    "系统阐述",
    "严谨分析",
)
REQUIRED_SEARCH_PHRASES = (
    "必须联网",
    "一定要查",
    "务必查证",
    "禁止编造",
    "不能瞎编",
    "必须查证",
    "必须检索",
    "你不能瞎编",
    "必须给出准确数字",
    "不要猜测",
    "查官方数据",
    "以官方为准",
    "不要编造",
    "不得臆测",
    "需要权威来源",
)
EXPLICIT_SEARCH_PHRASES = (
    "联网",
    "查证",
    "核实",
    "最新",
    "检索",
    "搜索",
    "查一下",
    "上网查",
    "网上查",
)
FRESHNESS_PHRASES = (
    "实时",
    "此刻",
    "今天的新闻",
    "最新股价",
    "最新汇率",
)
MEDICAL_KW = ("症状", "诊断", "处方", "用药", "治疗", "疾病", "医院", "临床")
LEGAL_KW = ("法律依据", "诉讼", "合同效力", "律师", "违法", "刑法", "民法", "合规")
FINANCE_KW = ("投资建议", "买入", "卖出", "股票推荐", "理财建议", "基金", "杠杆", "融资融券")
ENTITY_CONFUSION_KW = (
    "是不是同一个",
    "是不是一个人",
    "是不是一家",
    "是否同一",
    "区别",
    "对比",
    "vs",
    "对照",
    "还是",
)
NUMERIC_SENSITIVE_KW = (
    "具体数字",
    "准确数字",
    "数据是多少",
    "数字是多少",
    "具体数据",
    "最新数据",
    "最新数字",
)
SOURCE_SENSITIVE_KW = (
    "来源",
    "出处",
    "引用",
    "官网",
    "官方",
    "权威",
    "证据",
    "依据",
    "数据源",
    "原文",
    "链接",
)
ENTITY_JOINER_KW = ("和", "与", "跟")
ENTITY_WH_KW = ("谁", "哪位", "哪个", "哪家", "哪个人", "哪个公司")
NUMERIC_METRIC_KW = (
    "营收",
    "收入",
    "利润",
    "市值",
    "排名",
    "票房",
    "用户数",
    "下载量",
    "销量",
    "股价",
    "汇率",
    "百分比",
    "比例",
    "数量",
    "数据",
)
NUMERIC_WEAK_QUERY_KW = ("几", "多少", "多大", "多高", "多低")
NUMERIC_TIME_KW = ("最新", "当前", "今年", "去年", "本月", "本周", "最近", "实时")


def _norm_si(v: str) -> str:
    v = (v or "").strip().lower()
    if v in ("none", "optional", "explicit", "required", "freshness_required"):
        return v
    return ""


def norm_suggested_track(v: str) -> str:
    x = (v or "").strip().lower()
    if x in ("fast", "refine", "agent"):
        return x
    return ""


def detect_high_risk_domain(prompt: str) -> Tuple[bool, List[str]]:
    t = (prompt or "").strip()
    hits: List[str] = []
    for k in MEDICAL_KW:
        if k in t:
            hits.append(f"medical:{k}")
    for k in LEGAL_KW:
        if k in t:
            hits.append(f"legal:{k}")
    for k in FINANCE_KW:
        if k in t:
            hits.append(f"finance:{k}")
    return bool(hits), hits[:8]


def detect_search_sensitivity(prompt: str) -> Dict[str, Any]:
    t = (prompt or "").strip()
    low = t.lower()
    entity_hits: List[str] = []
    numeric_hits: List[str] = []
    source_hits: List[str] = []
    strong_entity_hits = [k for k in ENTITY_CONFUSION_KW if ((k in low) if k == "vs" else (k in t))]
    if strong_entity_hits:
        entity_hits.extend(strong_entity_hits)
    elif any(word in t for word in ENTITY_WH_KW) and any(token in t for token in ENTITY_JOINER_KW + ("还是",)):
        entity_hits.append("wh_compare")
    elif any(joiner in t for joiner in ENTITY_JOINER_KW) and any(token in t for token in ("同一个", "同一", "区别", "对比")):
        entity_hits.append("joiner_compare")

    strong_numeric_hits = [k for k in NUMERIC_SENSITIVE_KW if k in t]
    if strong_numeric_hits:
        numeric_hits.extend(strong_numeric_hits)
    elif any(k in t for k in NUMERIC_WEAK_QUERY_KW) and any(metric in t for metric in NUMERIC_METRIC_KW):
        numeric_hits.append("weak_query+metric")
    elif re.search(r"\d", t) and any(metric in t for metric in NUMERIC_METRIC_KW):
        numeric_hits.append("number+metric")
    elif any(metric in t for metric in NUMERIC_METRIC_KW) and any(flag in t for flag in NUMERIC_TIME_KW):
        numeric_hits.append("metric+time")
    for k in SOURCE_SENSITIVE_KW:
        if k in t:
            source_hits.append(k)
    return {
        "entity_confusion_risk": bool(entity_hits),
        "entity_confusion_hits": entity_hits[:8],
        "numeric_sensitive": bool(numeric_hits),
        "numeric_sensitive_hits": numeric_hits[:8],
        "source_sensitive": bool(source_hits),
        "source_sensitive_hits": source_hits[:8],
    }


def derive_user_signals(prompt: str, options: Dict[str, Any]) -> Dict[str, Any]:
    text = (prompt or "").strip()
    low = text.lower()

    si = _norm_si(str(options.get("search_intent") or ""))
    if not si:
        if any(k in text for k in REQUIRED_SEARCH_PHRASES):
            si = "required"
        elif any(k in text for k in FRESHNESS_PHRASES) or any(
            k in low for k in ("real-time", "breaking news", "latest price")
        ):
            si = "freshness_required"
        elif any(k in text for k in EXPLICIT_SEARCH_PHRASES) or any(
            k in low for k in ("web search", "search the web", "look up online")
        ):
            si = "explicit"
        elif str(options.get("search_mode") or "").lower() in ("on", "true", "1", "force", "always"):
            si = "explicit"
        else:
            si = "none"

    oi = str(options.get("output_intent") or "").strip().lower()
    if oi not in ("neutral", "fast", "deep"):
        oi = "neutral"
    fast_hit = any(k in text for k in FAST_PHRASES)
    deep_hit = any(k in text for k in DEEP_PHRASES)
    intent_conflict = fast_hit and deep_hit
    if oi == "neutral":
        if fast_hit and not deep_hit:
            oi = "fast"
        elif deep_hit and not fast_hit:
            oi = "deep"
        elif deep_hit:
            oi = "deep"

    return {
        "search_intent": si,
        "output_intent": oi,
        "intent_conflict": intent_conflict,
    }


def apply_suggested_track_on_conflict(signals: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    """当快速/深入表述冲突时，以分析器 suggested_track 覆盖 output_intent。"""
    out = dict(signals)
    if not signals.get("intent_conflict"):
        return out
    st = norm_suggested_track(str(analysis.get("suggested_track") or ""))
    if st == "fast":
        out["output_intent"] = "fast"
        out["output_intent_source"] = "analyzer_suggested_track"
    elif st in ("refine", "agent"):
        out["output_intent"] = "deep"
        out["output_intent_source"] = "analyzer_suggested_track"
    return out


def merge_signals_into_analysis(
    analysis: Dict[str, Any], signals: Dict[str, Any], user_prompt: str = ""
) -> Dict[str, Any]:
    sig2 = apply_suggested_track_on_conflict(signals, analysis)
    out = {**analysis}
    out["search_intent"] = sig2.get("search_intent", "none")
    out["output_intent"] = sig2.get("output_intent", "neutral")
    out["_routing_signals"] = sig2
    p = (user_prompt or str(analysis.get("search_prompt_base") or "")).strip()
    hr, hits = detect_high_risk_domain(p)
    sensitivity = detect_search_sensitivity(p)
    out["high_risk_domain"] = hr
    out["high_risk_hits"] = hits
    out.update(sensitivity)
    cur_si = str(out.get("search_intent") or "none").lower()
    if (out.get("numeric_sensitive") or out.get("source_sensitive")) and cur_si in (
        "none",
        "optional",
        "",
    ):
        out["search_intent"] = "required"
    return out


def reasoning_keyword_boost(prompt: str) -> Tuple[bool, str]:
    """
    预判为低复杂度时的二次规则：数学/证明/逻辑结构 → 视为 reasoning + high。
    """
    t = (prompt or "").strip()
    if not t:
        return False, ""
    if re.search(r"[∑∫∂∇≤≥≠±×÷√]", t):
        return True, "math_symbols"
    if re.search(r"\$[^$]+\$|\\\(|\\\)|\\\[|\\\]", t):
        return True, "latex"
    if "```" in t:
        return True, "code_block"
    logic_kw = ("证明", "推导", "如果", "那么", "充要条件", "归纳法", "反证法", "蕴含", "当且仅当")
    if any(k in t for k in logic_kw):
        return True, "logic_keywords"
    debug_kw = (
        "debug",
        "traceback",
        "stack trace",
        "报错",
        "报什么错",
        "报异常",
        "异常",
        "错误日志",
        "修复bug",
        "定位问题",
    )
    low = t.lower()
    if any(k in low for k in debug_kw):
        return True, "debug_keywords"
    if re.search(r"\d+\s*[\+\-\*/]\s*\d+", t):
        return True, "arithmetic_expr"
    return False, ""
