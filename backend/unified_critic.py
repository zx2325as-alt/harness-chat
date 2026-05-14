"""统一 Quality Critic：只评估、不生成；输出 recommended_action。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from json_utils import extract_balanced_json_object, strip_markdown_json_fence

RECOMMENDED_ACTIONS = frozenset({"accept", "repair", "search_more", "tool_use", "reject"})


def _orch(hcfg: Dict[str, Any]) -> Dict[str, Any]:
    return hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}


def _critic_models(harness: Any, hcfg: Dict[str, Any]) -> List[str]:
    orch = _orch(hcfg)
    uni = orch.get("unified_critic") if isinstance(orch.get("unified_critic"), dict) else {}
    key = str(uni.get("model_key") or "").strip()
    routing = hcfg.get("routing") or {}
    out: List[str] = []
    if key:
        out.append(key)
    dm = str(routing.get("default_model") or "").strip()
    if dm:
        out.append(dm)
    for m in routing.get("default_models") or []:
        s = str(m or "").strip()
        if s and s not in out:
            out.append(s)
    reg = getattr(harness, "registry", None)
    is_reg = getattr(reg, "is_registered", lambda x: False)
    return [m for m in out if is_reg(m)]


def _clamp01(x: Any, d: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = d
    return max(0.0, min(1.0, v))


def normalize_recommended(raw: Any) -> str:
    r = str(raw or "accept").strip().lower()
    return r if r in RECOMMENDED_ACTIONS else "accept"


async def evaluate_unified_critic(
    harness: Any,
    prompt: str,
    draft: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    search_context: str = "",
    mode: str = "general",
) -> Dict[str, Any]:
    """
    mode: general | verify  （verify 时强调「是否完整回答问题、是否与证据冲突」）
    """
    defaults: Dict[str, Any] = {
        "quality_score": 7.5,
        "completeness": 0.75,
        "factuality": 0.8,
        "clarity": 0.75,
        "hallucination_risk": 0.2,
        "missing_constraints": [],
        "issues": [],
        "recommended_action": "accept",
        "parse_ok": False,
    }
    orch_cfg = _orch(hcfg)
    critic_cfg = orch_cfg.get("unified_critic") if isinstance(orch_cfg.get("unified_critic"), dict) else {}
    if critic_cfg.get("enabled") is False:
        return {**defaults, "issues": ["unified_critic_disabled"], "parse_ok": False}

    cands = _critic_models(harness, hcfg)
    if not cands:
        return {**defaults, "issues": ["no_critic_model"]}

    lims = analysis.get("limitations") or []
    lim_note = ""
    if isinstance(lims, list) and lims:
        lim_note = "\n【系统限制】" + "；".join(str(x) for x in lims[:8])

    plan = analysis.get("capability_plan") if isinstance(analysis.get("capability_plan"), dict) else {}
    ctx = (search_context or "").strip()
    ctx_blk = f"\n【检索摘要】\n{ctx[:8000]}\n" if ctx else ""

    if mode == "verify":
        instruction = (
            "你是答案验证器，只输出 JSON。检查：是否回答全部子问题、是否遗漏用户约束、是否与检索摘要冲突、是否存在臆测。\n"
            "字段：quality_score(0-10), completeness(0-1), factuality(0-1), clarity(0-1), hallucination_risk(0-1), "
            "missing_constraints(string[]), issues(string[]), recommended_action。\n"
            "recommended_action 只能是：accept | repair | search_more | tool_use | reject。\n"
            "若不通过验证应优先 repair；缺证据用 search_more；需启用工具能力或外部执行用 tool_use；严重违规用 reject。\n"
        )
    else:
        instruction = (
            "你是统一质量评估器，只输出 JSON，不要 markdown 围栏。\n"
            "字段：quality_score(0-10), completeness(0-1), factuality(0-1), clarity(0-1), hallucination_risk(0-1), "
            "missing_constraints(string[]), issues(string[]), recommended_action。\n"
            "recommended_action 只能是：accept | repair | search_more | tool_use | reject。\n"
            "明显遗漏、论证不足、事实风险高、与用户约束冲突时应 repair；缺实时证据 search_more；需要工具能力或外部执行时 tool_use。\n"
        )

    critic_prompt = (
        f"{instruction}"
        f"{lim_note}\n能力规划：{json.dumps(plan, ensure_ascii=False)[:500]}\n"
        f"{ctx_blk}\n【用户问题】\n{(prompt or '')[:6000]}\n\n【候选答案】\n{(draft or '')[:14000]}\n"
    )

    opts = {**options, "temperature": 0.05, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(cands, critic_prompt, opts, messages=None)
    if not res or not res.success:
        return {**defaults, "issues": ["unified_critic_failed"], "recommended_action": "repair"}

    raw_txt = strip_markdown_json_fence(res.content or "")
    blob = extract_balanced_json_object(raw_txt) or raw_txt
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return {**defaults, "issues": ["unified_critic_json_invalid"], "recommended_action": "repair"}

    def _str_list(k: str) -> List[str]:
        v = data.get(k)
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v[:24] if str(x).strip()]

    out = {
        "quality_score": float(data.get("quality_score", defaults["quality_score"]) or defaults["quality_score"]),
        "completeness": _clamp01(data.get("completeness"), defaults["completeness"]),
        "factuality": _clamp01(data.get("factuality"), defaults["factuality"]),
        "clarity": _clamp01(data.get("clarity"), defaults["clarity"]),
        "hallucination_risk": _clamp01(data.get("hallucination_risk"), defaults["hallucination_risk"]),
        "missing_constraints": _str_list("missing_constraints"),
        "issues": _str_list("issues"),
        "recommended_action": normalize_recommended(data.get("recommended_action")),
        "parse_ok": True,
        "critic_model": res.model,
        "latency_ms": res.latency_ms,
    }
    return out



async def evaluate_structured_quality_critic(
    harness: Any,
    question: str,
    draft: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    candidates: List[str],
) -> Dict[str, Any]:
    """结构化质量批评（事实与逻辑），供 Repair 使用。"""
    defaults = {
        "missing_points": [],
        "logic_issues": [],
        "fact_risks": [],
        "unsupported_claims": [],
        "needs_search": [],
        "confidence": 0.5,
        "parse_ok": False,
    }
    if not candidates:
        return defaults
    prompt = (
        "你是结构化审稿器，只输出 JSON。对照用户问题与草案，列出可修复问题。\n"
        "字段：missing_points(string[]), logic_issues(string[]), fact_risks(string[]), "
        "unsupported_claims(string[]), needs_search(string[]，每条为建议检索查询), confidence(0-1)。\n\n"
        f"【用户问题】\n{(question or '')[:4000]}\n\n【草案】\n{(draft or '')[:12000]}\n"
    )
    opts = {**options, "temperature": 0.08, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(candidates, prompt, opts, messages=None)
    if not res or not res.success:
        return defaults

    raw_txt = strip_markdown_json_fence(res.content or "")
    blob = extract_balanced_json_object(raw_txt) or raw_txt
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return defaults

    def sl(k: str) -> List[str]:
        v = data.get(k)
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v[:16] if str(x).strip()]

    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return {
        "missing_points": sl("missing_points"),
        "logic_issues": sl("logic_issues"),
        "fact_risks": sl("fact_risks"),
        "unsupported_claims": sl("unsupported_claims"),
        "needs_search": sl("needs_search"),
        "confidence": max(0.0, min(1.0, conf)),
        "parse_ok": True,
    }


async def verify_answer(
    harness: Any,
    prompt: str,
    candidate_answer: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    search_context: str = "",
) -> Dict[str, Any]:
    """文档第五章 Verify：与 evaluate_unified_critic(mode=\"verify\") 等价。"""
    return await evaluate_unified_critic(
        harness,
        prompt,
        candidate_answer,
        analysis,
        options,
        hcfg,
        search_context=search_context,
        mode="verify",
    )
