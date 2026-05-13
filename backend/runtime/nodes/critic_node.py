"""Critic Runtime Node：分层 + 并行 critic（Coverage/Logic/Evidence/Hallucination/Policy 由 critic_engine 编排）。"""
from __future__ import annotations

from typing import Any, Dict

from refine_shared import _pg
from runtime.dag_common import user_status
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.metrics_hooks import record_parallelism
from runtime.quality.critic_engine import run_unified_with_layered_facets
from runtime.quality.hallucination_detector import hallucination_score_from_critics
from runtime.quality.repair_engine import critic_issue_total
from runtime_metrics import emit_product_metric


async def execute_round(
    ctx: DAGRuntimeContext,
    draft: str,
    ev_text: str,
    round_idx: int,
) -> Dict[str, Any]:
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    plan = ctx.plan
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id

    emit_product_metric(hcfg, "critic_trigger_rate", trace_id=trace_id, round=round_idx, dag=True)

    crit_key = f"{hash(draft[:2000])}:{round_idx}:{hash(ev_text[:1200])}"
    if ctx.caches:
        hit = ctx.caches.critic.get(crit_key)
        if hit:
            if st:
                st.runtime_memory.append({"phase": "critic_cache_hit", "round": round_idx})
            merged = hit
        else:
            merged = None
    else:
        merged = None

    await ctx.emit(user_status(f"并行批评（第 {round_idx + 1}/{plan.repair_rounds_max} 轮）…", phase="critic"))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_parallel_critic",
                "status": "running",
                "meta": _pg({"round": round_idx}, "review", "DAG：并行 Critic Nodes"),
            },
        }
    )

    if merged is None:
        merged = await run_unified_with_layered_facets(
            h,
            ctx.prompt,
            draft,
            ctx.analysis,
            opt,
            hcfg,
            ctx.review_cands,
            ev_text,
            layered=plan.layered_critics,
            legacy_parallel=plan.parallel_critics and not plan.layered_critics,
        )
        if ctx.caches:
            ctx.caches.critic.put(crit_key, merged)

    if plan.layered_critics:
        ctx.max_wave_parallel = max(ctx.max_wave_parallel, 6)
        record_parallelism(opt, 6)
    elif plan.parallel_critics:
        ctx.max_wave_parallel = max(ctx.max_wave_parallel, 2)
        record_parallelism(opt, 2)

    uni_m = merged.get("_unified") or {}
    struct_m = merged.get("_structured") or {}
    if st:
        st.critic_reports.append({"round": round_idx, "merged": merged})
        st.hallucination_risk = hallucination_score_from_critics(uni_m, struct_m)
        emit_product_metric(
            hcfg,
            "hallucination_rate",
            trace_id=trace_id,
            risk=float(st.hallucination_risk),
            layered=plan.layered_critics,
        )
        ctrs = (merged.get("_structured") or {}).get("fact_risks") or []
        if isinstance(ctrs, list):
            st.contradictions.extend([str(x) for x in ctrs[:6]])

    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_parallel_critic",
                "status": "ok",
                "meta": _pg(
                    {
                        "issue_total": critic_issue_total(merged),
                        "layered_critics": plan.layered_critics,
                        "legacy_parallel": plan.parallel_critics and not plan.layered_critics,
                        "facets": ["coverage", "logic", "evidence", "hallucination", "policy"],
                    },
                    "review",
                    "并行批评完成。",
                ),
            },
        }
    )
    return merged
