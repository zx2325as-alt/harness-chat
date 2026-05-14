from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple

from json_utils import extract_balanced_json_object, strip_markdown_json_fence
from unified_critic import evaluate_structured_quality_critic, evaluate_unified_critic

FACET_LAYER_DEFS: List[Tuple[str, str]] = [
    ("coverage", "覆盖度：是否遗漏用户子问题、约束或未回答要点"),
    ("logic", "逻辑：推理链是否断裂、因果是否跳跃"),
    ("evidence", "证据：结论是否有可靠支撑；列出疑似 unsupported 的断言"),
    ("hallucination", "臆测：是否包含未经验证或可能编造的具体事实"),
    ("policy", "策略：是否违反安全、隐私、违法指引或必要拒答场景"),
]


async def run_parallel_critics(
    harness: Any,
    prompt: str,
    draft: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    review_cands: List[str],
    *,
    search_context: str,
    enabled: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Coverage/Policy 偏向 unified general；结构化 JSON 偏向 Logic/Evidence。"""

    async def _uni():
        return await evaluate_unified_critic(
            harness,
            prompt,
            draft,
            analysis,
            options,
            hcfg,
            search_context=search_context,
            mode="general",
        )

    async def _struct():
        return await evaluate_structured_quality_critic(harness, prompt, draft, options, hcfg, review_cands)

    if enabled:
        return await asyncio.gather(_uni(), _struct())
    u = await _uni()
    s = await _struct()
    return u, s


def merge_structured_with_parallel(unified: Dict[str, Any], structured: Dict[str, Any]) -> Dict[str, Any]:
    """合成 Repair 可用的结构化 critic 视图。"""
    logic = list(structured.get("logic_issues") or [])
    for x in list(unified.get("issues") or [])[:12]:
        logic.append(f"[unified] {x}")
    return {
        "missing_points": list(structured.get("missing_points") or []),
        "logic_issues": logic,
        "fact_risks": list(structured.get("fact_risks") or []),
        "unsupported_claims": list(structured.get("unsupported_claims") or []),
        "needs_search": list(structured.get("needs_search") or []),
        "confidence": float(structured.get("confidence") or 0.5),
        "parse_ok": bool(structured.get("parse_ok")) or bool(unified.get("parse_ok")),
        "unified_recommended": str(unified.get("recommended_action") or "accept"),
        "_unified": unified,
        "_structured": structured,
    }


def _parse_facet_json(raw: str) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {"issues": [], "risk_score": 0.35, "suggested_search_queries": [], "parse_ok": False}
    txt = strip_markdown_json_fence(raw or "")
    blob = extract_balanced_json_object(txt) or txt
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return defaults
    issues = data.get("issues")
    out_issues: List[str] = []
    if isinstance(issues, list):
        out_issues = [str(x).strip() for x in issues[:16] if str(x).strip()]
    sq = data.get("suggested_search_queries")
    out_sq: List[str] = []
    if isinstance(sq, list):
        out_sq = [str(x).strip() for x in sq[:8] if str(x).strip()]
    try:
        rs = float(data.get("risk_score", 0.35))
    except (TypeError, ValueError):
        rs = 0.35
    return {
        "issues": out_issues,
        "risk_score": max(0.0, min(1.0, rs)),
        "suggested_search_queries": out_sq,
        "parse_ok": True,
    }


async def _facet_review(
    harness: Any,
    facet_key: str,
    facet_instruction: str,
    prompt: str,
    draft: str,
    ev_text: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    cands: List[str],
) -> Dict[str, Any]:
    pr = (
        f"你是「{facet_key}」维度的审稿器，只输出 JSON。\n"
        f"字段：issues(string[]), risk_score(0-1), suggested_search_queries(string[],可为空)。\n"
        f"审稿维度：{facet_instruction}\n\n"
        f"【用户问题】\n{(prompt or '')[:3500]}\n\n【草案】\n{(draft or '')[:9000]}\n\n"
        f"【证据摘要】\n{(ev_text or '')[:4500]}\n"
    )
    opts = {**options, "temperature": 0.06, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(cands, pr, opts, messages=None)
    if not res or not res.success:
        return {"issues": [], "risk_score": 0.4, "suggested_search_queries": [], "parse_ok": False, "_facet": facet_key}
    parsed = _parse_facet_json(res.content or "")
    parsed["_facet"] = facet_key
    return parsed


async def run_layered_critics_parallel(
    harness: Any,
    prompt: str,
    draft: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    review_cands: List[str],
    ev_text: str,
) -> Dict[str, Dict[str, Any]]:
    tasks = [
        _facet_review(harness, k, instr, prompt, draft, ev_text, options, hcfg, review_cands)
        for k, instr in FACET_LAYER_DEFS
    ]
    rows = await asyncio.gather(*tasks)
    return {str(r.get("_facet") or ""): r for r in rows if r.get("_facet")}


def facets_bundle_to_structured(facets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    cov = facets.get("coverage") or {}
    log = facets.get("logic") or {}
    evi = facets.get("evidence") or {}
    hal = facets.get("hallucination") or {}
    pol = facets.get("policy") or {}
    needs: List[str] = []
    for row in (cov, evi, hal):
        for q in row.get("suggested_search_queries") or []:
            if q and q not in needs:
                needs.append(q)
    risks = [float((facets.get(k) or {}).get("risk_score") or 0.35) for k in ("coverage", "logic", "evidence", "hallucination", "policy")]
    conf = max(0.05, 1.0 - (sum(risks) / max(1, len(risks))))
    return {
        "missing_points": list(cov.get("issues") or []),
        "logic_issues": list(log.get("issues") or []),
        "fact_risks": list(hal.get("issues") or []) + list(pol.get("issues") or []),
        "unsupported_claims": list(evi.get("issues") or []),
        "needs_search": needs[:12],
        "confidence": conf,
        "parse_ok": True,
        "_facets": facets,
    }


async def run_single_facet_review(
    harness: Any,
    facet_key: str,
    facet_instruction: str,
    prompt: str,
    draft: str,
    ev_text: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    review_cands: List[str],
) -> Dict[str, Any]:
    """单层 facet 审稿（供 DAG 多节点并行 gather）。"""
    return await _facet_review(
        harness,
        facet_key,
        facet_instruction,
        prompt,
        draft,
        ev_text,
        options,
        hcfg,
        review_cands,
    )


async def run_unified_with_parallel_facets(
    harness: Any,
    prompt: str,
    draft: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    review_cands: List[str],
    ev_text: str,
    *,
    parallel_facets: bool,
    paired_parallel: bool,
) -> Dict[str, Any]:
    """返回 merge_structured_with_parallel 可直接消费的 merged（内含 _facet_reports）。"""
    async def _uni():
        return await evaluate_unified_critic(
            harness,
            prompt,
            draft,
            analysis,
            options,
            hcfg,
            search_context=ev_text,
            mode="general",
        )

    if parallel_facets:
        uni, facets = await asyncio.gather(
            _uni(),
            run_layered_critics_parallel(harness, prompt, draft, options, hcfg, review_cands, ev_text),
        )
        struct_like = facets_bundle_to_structured(facets)
        merged = merge_structured_with_parallel(uni, struct_like)
        merged["_facet_reports"] = facets
        return merged

    if paired_parallel:
        uni, struct = await asyncio.gather(
            _uni(),
            evaluate_structured_quality_critic(harness, prompt, draft, options, hcfg, review_cands),
        )
        return merge_structured_with_parallel(uni, struct)

    uni = await _uni()
    struct = await evaluate_structured_quality_critic(harness, prompt, draft, options, hcfg, review_cands)
    return merge_structured_with_parallel(uni, struct)
