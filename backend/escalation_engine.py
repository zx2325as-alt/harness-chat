"""统一升级决策：解析 unified critic 的 recommended_action（文档第九章）。"""
from __future__ import annotations

from typing import Any, Dict

from orchestrator_state import record_track_escalation
from runtime_metrics import log_runtime_event


def escalate(
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    from_track: str,
    to_track: str,
    *,
    trace_id: str = "",
    reason: str = "",
) -> bool:
    """唯一入口：单调升级时更新 ExecutionState.current_track 并打观测；降级/平级则拒绝。"""
    if not record_track_escalation(options, from_track, to_track):
        return False
    log_runtime_event(
        hcfg,
        {
            "event": "track_escalation",
            "trace_id": trace_id,
            "from_track": from_track,
            "to_track": to_track,
            "reason": reason,
        },
    )
    return True


def recommended_action(result: Dict[str, Any]) -> str:
    return str(result.get("recommended_action") or "accept").strip().lower()


def should_accept(result: Dict[str, Any]) -> bool:
    return recommended_action(result) == "accept"


def should_escalate_refine(result: Dict[str, Any]) -> bool:
    return recommended_action(result) in ("refine", "search_more", "reject")


def should_agent_recover(result: Dict[str, Any]) -> bool:
    return recommended_action(result) == "agent_recover"


def merge_issues_into_execution_state(options: Dict[str, Any], result: Dict[str, Any]) -> None:
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
    mc = result.get("missing_constraints")
    if isinstance(mc, list):
        for x in mc[:8]:
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
