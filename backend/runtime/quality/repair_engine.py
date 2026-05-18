from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from semantic_utils import ngram_overlap_ratio


@dataclass
class RepairPlan:
    fix_claims: List[str] = field(default_factory=list)
    add_evidence: List[str] = field(default_factory=list)
    remove_hallucinations: List[str] = field(default_factory=list)


def build_repair_plan_from_crit(crit: Dict[str, Any]) -> RepairPlan:
    mp = list(crit.get("missing_points") or [])
    li = list(crit.get("logic_issues") or [])
    fr = list(crit.get("fact_risks") or [])
    uc = list(crit.get("unsupported_claims") or [])
    return RepairPlan(
        fix_claims=[str(x) for x in (mp + li + uc)[:24] if str(x).strip()],
        add_evidence=[str(x) for x in (crit.get("needs_search") or [])[:12] if str(x).strip()],
        remove_hallucinations=[str(x) for x in fr[:16] if str(x).strip()],
    )


def critic_issue_total(crit: Dict[str, Any]) -> int:
    return sum(len(crit.get(k) or []) for k in ("missing_points", "logic_issues", "fact_risks", "unsupported_claims"))


def _find_top_evidence_for_claim(claim: str, ev_text: str, *, top_k: int = 3) -> str:
    """从 ev_text 中用 ngram_overlap_ratio 为 claim 找最相关的 top_k 段证据，拼接返回。"""
    ev = str(ev_text or "")
    if not ev.strip():
        return ""
    window, step = 400, 100
    segments: List[Tuple[float, str]] = []
    for j in range(0, min(len(ev), 12000), step):
        seg = ev[j : j + window]
        if seg.strip():
            score = ngram_overlap_ratio(claim.lower()[:300], seg.lower())
            segments.append((score, seg))
    segments.sort(key=lambda x: -x[0])
    top = [s for _, s in segments[:top_k] if s.strip()]
    return "\n---\n".join(top)


def _build_targeted_evidence_block(
    unsupported_claims: List[str],
    ev_text: str,
    *,
    max_claims: int = 8,
    top_k_per_claim: int = 2,
) -> str:
    """为每个 unsupported claim 单独匹配最相关证据，构造 per-claim 证据块。"""
    if not unsupported_claims or not ev_text.strip():
        return ev_text[:6000]
    parts: List[str] = []
    for claim in unsupported_claims[:max_claims]:
        snippets = _find_top_evidence_for_claim(claim, ev_text, top_k=top_k_per_claim)
        if snippets:
            parts.append(f"[断言] {claim[:200]}\n[相关证据]\n{snippets[:800]}")
    targeted = "\n\n".join(parts)
    # 附加全量证据兜底（截短），保证修复 LLM 有完整上下文
    tail = ev_text[:4000] if len(ev_text) > 4000 else ev_text
    return f"{targeted}\n\n[全量证据摘录]\n{tail}" if targeted else tail


async def targeted_repair(
    harness: Any,
    prompt: str,
    draft: str,
    crit: Dict[str, Any],
    ev_text: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    repair_pool: List[str],
) -> Tuple[Any, str, bool]:
    """issue-targeted repair：定向证据注入 + guard；返回 (AskResult|None, repaired_text, guard_reverted)。"""
    issue_n = critic_issue_total(crit)
    needs_q = [str(x).strip() for x in (crit.get("needs_search") or []) if str(x).strip()]
    if issue_n == 0 and not needs_q:
        return None, draft, False

    plan = build_repair_plan_from_crit(crit)

    # 定向证据块：按 unsupported_claims 匹配最相关证据片段，比盲注全量 ev_text 更精准
    unsupported = [str(x).strip() for x in (crit.get("unsupported_claims") or []) if str(x).strip()]
    targeted_ev = _build_targeted_evidence_block(unsupported, ev_text)

    repair_prompt = (
        "你是修订编辑。下面给出用户问题、当前草案、结构化批评要点与按断言匹配的定向证据。\n"
        "要求：按 RepairPlan 定点修改——修复列出的断言/逻辑问题、删除或改写臆测句、"
        "用【定向证据】支撑对应断言；不要重写未被点名的正确段落；"
        "保持与原问题语言一致；输出完整修订稿正文。\n\n"
        f"【用户问题】\n{(prompt or '')[:4000]}\n\n"
        f"【当前草案】\n{draft[:10000]}\n\n"
        f"【RepairPlan】\n"
        f"fix_claims: {json.dumps(plan.fix_claims, ensure_ascii=False)[:4000]}\n"
        f"add_evidence: {json.dumps(plan.add_evidence, ensure_ascii=False)[:2000]}\n"
        f"remove_hallucinations: {json.dumps(plan.remove_hallucinations, ensure_ascii=False)[:2000]}\n\n"
        f"【结构化批评 JSON】\n{json.dumps(crit, ensure_ascii=False)[:8000]}\n\n"
        f"【定向证据（按断言匹配）】\n{targeted_ev[:10000]}\n"
    )
    opts_r = harness._layer_opts(hcfg, "runtime_repair", options)
    r_rep, _ = await harness._ask_with_fallback(repair_pool, repair_prompt, opts_r, messages=None)
    repaired = (r_rep.content or "").strip() if r_rep and r_rep.success else draft
    if not repaired.strip():
        repaired = draft
    guard_reverted = False
    if r_rep and r_rep.success and repaired != draft:
        ov = ngram_overlap_ratio(draft[:12000], repaired[:12000])
        short_frac = len(repaired) < max(40, int(len(draft) * 0.35))
        if (issue_n <= 3 and ov < 0.1) or (issue_n <= 2 and short_frac and len(draft) > 80):
            repaired = draft
            guard_reverted = True
    return r_rep, repaired, guard_reverted
