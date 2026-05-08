"""Refine 可选「批评→注入润色」阶段：在 legacy L3 之前追加结构化修复要点（ critic_runtime 模式）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from json_utils import extract_balanced_json_object, strip_markdown_json_fence


def _orch(hcfg: Dict[str, Any]) -> Dict[str, Any]:
    return hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}


def refine_critic_mode(hcfg: Dict[str, Any]) -> str:
    orch = _orch(hcfg)
    stages = orch.get("rollout_stages") if isinstance(orch.get("rollout_stages"), dict) else {}
    if stages.get("refine_critic_runtime") is False:
        return "legacy"
    pipe = orch.get("refine_pipeline") if isinstance(orch.get("refine_pipeline"), dict) else {}
    m = str(pipe.get("mode") or "legacy").strip().lower()
    return m if m in ("legacy", "critic_runtime") else "legacy"


def _critic_models(harness: Any, hcfg: Dict[str, Any]) -> List[str]:
    orch = _orch(hcfg)
    pipe = orch.get("refine_pipeline") if isinstance(orch.get("refine_pipeline"), dict) else {}
    key = str(pipe.get("critic_model_key") or "").strip()
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


async def build_refine_critic_injection(
    harness: Any,
    question: str,
    draft_excerpt: str,
    review_body: str,
    hcfg: Dict[str, Any],
    options: Dict[str, Any],
) -> str:
    """返回附加到 L3 prompt 末尾的块；legacy 模式返回空串。"""
    if refine_critic_mode(hcfg) != "critic_runtime":
        return ""
    cands = _critic_models(harness, hcfg)
    if not cands:
        return ""

    from refine_shared import _clean_review_body

    rb = _clean_review_body(review_body)
    critic_prompt = (
        "你是精化流水线的结构化批评者，只输出 JSON，不要 markdown 围栏。\n"
        "根据用户问题、初稿摘录与审查结论文本，输出："
        "repair_bullets(string 数组，3-8 条可执行修复要点), risk_flags(string 数组), ready_for_polish(bool)。\n"
        "repair_bullets 用简练中文，供下游润色模型落实。\n\n"
        f"【用户问题】\n{(question or '')[:4000]}\n\n"
        f"【初稿摘录】\n{(draft_excerpt or '')[:6000]}\n\n"
        f"【审查结论】\n{rb[:12000]}\n"
    )
    opts = {**options, "temperature": 0.08, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(cands, critic_prompt, opts, messages=None)
    if not res or not res.success:
        return ""

    raw = strip_markdown_json_fence(res.content or "")
    blob = extract_balanced_json_object(raw) or raw
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return ""

    bullets = data.get("repair_bullets")
    risks = data.get("risk_flags")
    if not isinstance(bullets, list):
        bullets = []
    if not isinstance(risks, list):
        risks = []
    lines: List[str] = []
    if bullets:
        lines.append("修复要点：")
        for b in bullets[:10]:
            t = str(b).strip()
            if t:
                lines.append(f"- {t}")
    if risks:
        lines.append("风险标记：" + "；".join(str(x).strip() for x in risks[:8] if str(x).strip()))
    if not lines:
        return ""
    return "\n\n【结构化批评意见（润色层必须落实）】\n" + "\n".join(lines) + "\n"
