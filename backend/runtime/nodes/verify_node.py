"""Verification Runtime Node：claim extraction → evidence mapping → verify（委托 verification_engine）。"""
from __future__ import annotations

from typing import Any, Dict

from refine_shared import _pg
from runtime.dag_common import user_status
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.quality.verification_engine import run_verify_with_evidence_mapping
from runtime_metrics import emit_product_metric


def _merge_verify_result_into_execution_state(options: Dict[str, Any], result: Dict[str, Any]) -> None:
    from runtime_state import get_execution_state

    st = get_execution_state(options)
    if not st:
        return
    issues = result.get("issues")
    if isinstance(issues, list):
        for x in issues[:16]:
            s = str(x).strip()
            if s and s not in st.critic_issues:
                st.critic_issues.append(s)
    missing_constraints = result.get("missing_constraints")
    if isinstance(missing_constraints, list):
        for x in missing_constraints[:8]:
            s = str(x).strip()
            if s:
                st.critic_issues.append(f"constraint:{s}")
    try:
        st.quality_score = float(result.get("quality_score") or st.quality_score)
        st.hallucination_risk = float(result.get("hallucination_risk") or st.hallucination_risk)
        st.completeness_score = float(result.get("completeness") or st.completeness_score)
        st.confidence_score = float(result.get("factuality") or st.confidence_score)
    except (TypeError, ValueError):
        pass


async def execute_round(
    ctx: DAGRuntimeContext,
    draft: str,
    ev_text: str,
    round_idx: int,
) -> Dict[str, Any]:
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id

    await ctx.emit(user_status("验证答案…", phase="verify"))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_verify",
                "status": "running",
                "meta": _pg({"round": round_idx}, "verify", "DAG：Verify Node"),
            },
        }
    )
    before_q = float(st.quality_score) if st else 0.0
    uc = await run_verify_with_evidence_mapping(
        h,
        ctx.prompt,
        draft,
        ctx.analysis,
        opt,
        hcfg,
        search_context=ev_text,
    )
    _merge_verify_result_into_execution_state(opt, uc)
    if st:
        st.verification_reports.append({"round": round_idx, "verify": uc})
    try:
        after_q = float(uc.get("quality_score") or 0.0)
        emit_product_metric(
            hcfg,
            "verify_quality_delta",
            trace_id=trace_id,
            before_verify=before_q,
            after_verify=after_q,
            delta=after_q - before_q,
        )
    except (TypeError, ValueError):
        pass

    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_verify",
                "status": "ok",
                "meta": _pg({"unified_critic": uc}, "verify", "验证完成。"),
            },
        }
    )
    bits = [f"action={uc.get('recommended_action') or ''}"]
    ch = uc.get("contradiction_heuristic") if isinstance(uc, dict) else None
    if isinstance(ch, dict) and ch.get("hit"):
        bits.append("contradiction_hint")
    uns = uc.get("unsupported_claims_heuristic") if isinstance(uc, dict) else None
    if isinstance(uns, list) and uns:
        bits.append(f"unsupported≈{len(uns)}")
    line = " · ".join(bits).strip()
    if line:
        await ctx.emit(
            {
                "event": "chunk",
                "data": {"content": line[:420], "channel": "stream_verify", "round": round_idx},
            }
        )
    return uc
