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
    """issue-targeted repair：返回 (AskResult|None, repaired_text, guard_reverted)。"""
    issue_n = critic_issue_total(crit)
    needs_q = [str(x).strip() for x in (crit.get("needs_search") or []) if str(x).strip()]
    if issue_n == 0 and not needs_q:
        return None, draft, False

    plan = build_repair_plan_from_crit(crit)
    repair_prompt = (
        "你是修订编辑。下面给出用户问题、当前草案、结构化批评要点与可选检索证据。\n"
        "要求：按 RepairPlan 定点修改——修复列出的断言/逻辑问题、删除或改写臆测句、在需要处引用证据；"
        "不要重写未被点名的正确段落；保持与原问题语言一致；输出完整修订稿正文。\n\n"
        f"【用户问题】\n{(prompt or '')[:4000]}\n\n【当前草案】\n{draft[:10000]}\n\n"
        f"【RepairPlan】\n"
        f"fix_claims: {json.dumps(plan.fix_claims, ensure_ascii=False)[:6000]}\n"
        f"add_evidence: {json.dumps(plan.add_evidence, ensure_ascii=False)[:4000]}\n"
        f"remove_hallucinations: {json.dumps(plan.remove_hallucinations, ensure_ascii=False)[:4000]}\n\n"
        f"【结构化批评 JSON】\n{json.dumps(crit, ensure_ascii=False)[:12000]}\n\n"
        f"【检索证据摘录】\n{ev_text[:8000]}\n"
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
