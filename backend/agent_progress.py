"""Agent 轮次进度评估：识别「措辞不同但无实质进展」，为空转中止的主依据。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from json_utils import extract_balanced_json_object, strip_markdown_json_fence


def _progress_cfg(hcfg: Dict[str, Any]) -> Dict[str, Any]:
    ag = hcfg.get("agent_tuning") if isinstance(hcfg.get("agent_tuning"), dict) else {}
    pe = ag.get("progress_eval")
    return pe if isinstance(pe, dict) else {}


def _agent_models(harness: Any, hcfg: Dict[str, Any]) -> List[str]:
    pe = _progress_cfg(hcfg)
    key = str(pe.get("model_key") or "").strip()
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


async def evaluate_agent_progress(
    harness: Any,
    user_prompt: str,
    previous_turn_text: str,
    current_turn_text: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
) -> Dict[str, Any]:
    """返回 progress_score(0-1)、delta_vs_previous(0-1)；失败时保守返回中等分数。"""
    defaults = {"progress_score": 0.55, "delta_vs_previous": 0.35, "parse_ok": False, "notes": []}
    cands = _agent_models(harness, hcfg)
    if not cands:
        return {**defaults, "notes": ["no_progress_model"]}

    prompt = (
        "你是对话进度评估器，只输出 JSON，不要 markdown 围栏。\n"
        "比较智能体相邻两轮面向用户的可见输出（不含系统注入），判断是否在推进任务。\n"
        "字段：progress_score(0-1，越大表示相对用户目标越有实质推进), "
        "delta_vs_previous(0-1，相对上一轮新增有效信息程度), notes(string 数组，简短)。\n"
        "若只是在换说法、重复要点或空洞扩展，应给低分。\n\n"
        f"【用户问题】\n{(user_prompt or '')[:4000]}\n\n"
        f"【上一轮助手输出】\n{(previous_turn_text or '')[:3500]}\n\n"
        f"【当前轮助手输出】\n{(current_turn_text or '')[:3500]}\n"
    )
    opts = {**options, "temperature": 0.05, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(cands, prompt, opts, messages=None)
    if not res or not res.success:
        return {**defaults, "notes": ["progress_call_failed"]}

    raw = strip_markdown_json_fence(res.content or "")
    blob = extract_balanced_json_object(raw) or raw
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return {**defaults, "notes": ["progress_json_invalid"]}

    def _f(k: str, d: float) -> float:
        try:
            return float(data.get(k, d))
        except (TypeError, ValueError):
            return d

    notes = data.get("notes")
    if not isinstance(notes, list):
        notes = []
    return {
        "progress_score": max(0.0, min(1.0, _f("progress_score", defaults["progress_score"]))),
        "delta_vs_previous": max(0.0, min(1.0, _f("delta_vs_previous", defaults["delta_vs_previous"]))),
        "parse_ok": True,
        "notes": [str(x) for x in notes[:6]],
    }
