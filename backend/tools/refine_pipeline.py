"""Refine 答案流水线：跳过 Draft，从给定草稿进入 Review（含可选联网核查）+ Polish 流式输出。"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

from search_query_util import validate_search_query
from tools.parsing import RE_AGENT_WS


async def stream_refine_from_draft(
    harness: Any,
    question: str,
    draft_text: str,
    options: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]],
    trace_id: str,
    hcfg: Dict[str, Any],
    analysis: Dict[str, Any],
    *,
    meta_extra: Optional[Dict[str, Any]] = None,
    extra_review_context: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    harness: DualTrackHarness 实例（避免循环导入不写类型注解）。
    meta_extra: 写入各 step.meta，用于区分 agent 工具调用 / 兜底等。
    extra_review_context: 可选自检文本，仅注入审查层 prompt，不进入最终用户可见草稿。
    """
    from harness import Step, _pg  # 运行时导入，避免与 harness 顶层循环依赖

    extra_meta = dict(meta_extra or {})
    chain = hcfg.get("refine_chain") or {}
    l2 = chain.get("layer2") or {}
    l3 = chain.get("layer3") or {}
    routing = hcfg.get("routing") or {}
    default_model = routing.get("default_model", "gpt-5.5")
    refine_models = analysis.get("refine_models") or {}
    l2_prompt = (
        f"{l2.get('instruction','').strip()}\n\n【原始问题】\n{question.strip()}\n\n【初稿答案】\n{draft_text.strip()}\n"
    )
    scr = (extra_review_context or "").strip()
    if scr:
        l2_prompt += (
            "\n\n【自检与不确定性（供审查参考，勿直接当作用户可见正文）】\n"
            f"{scr}\n"
        )
    l2_candidates = refine_models.get("review") or [default_model]
    opts_l2 = {**options, "temperature": float(l2.get("temperature", 0.1))}
    r2, a2 = await harness._ask_with_fallback(l2_candidates, l2_prompt, opts_l2, messages=None)
    step_l2 = Step(
        name="refine_layer2_review",
        status="ok" if r2.success else "error",
        provider=r2.provider,
        model=r2.model,
        latency_ms=r2.latency_ms,
        input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
        output=r2.content if r2.success else None,
        error=r2.error if not r2.success else None,
        meta=_pg(
            {
                **extra_meta,
                "attempts": a2,
                "candidates": l2_candidates,
                "from_agent": True,
                "pipeline_phase": "review",
            },
            "polishing",
            "审查层：对照草稿与自检要点核对事实与结构…",
        ),
    )
    yield {"event": "step", "step": step_l2.to_dict()}
    if not r2.success:
        yield {"event": "error", "error": "审查层失败"}
        return
    review_body = r2.content or ""
    extra_ctx = ""
    review_snip_loop = 0
    for _round in range(3):
        wm = RE_AGENT_WS.search(review_body)
        if not wm:
            break
        review_snip_loop += 1
        q = wm.group(1).strip()
        yield {
            "event": "step",
            "step": {
                "name": "review_web_search",
                "status": "running",
                "meta": _pg(
                    {
                        **extra_meta,
                        "query": q,
                        "review_round": review_snip_loop,
                        "phase": "审查内按需检索",
                        "pipeline_phase": "retrieval",
                    },
                    "polishing",
                    f"审查中联网：第 {review_snip_loop} 轮「{q[:60]}{'…' if len(q) > 60 else ''}」…",
                ),
            },
        }
        vq, vfc, vreason = validate_search_query(q)
        if vfc:
            sr = {
                "context": "",
                "sources": [],
                "error": vreason or vfc,
                "failure_code": vfc,
            }
        else:
            sr = await harness.perform_web_search(vq, options)
        snip = (sr.get("context") or "")[:8000]
        rc = len(sr.get("sources") or [])
        yield {
            "event": "step",
            "step": {
                "name": "review_web_search",
                "status": "error" if sr.get("error") else "ok",
                "meta": _pg(
                    {
                        **extra_meta,
                        "query": q,
                        "review_round": review_snip_loop,
                        "phase": "审查内按需检索",
                        "result_count": rc,
                        "sources": sr.get("sources") or [],
                        "pipeline_phase": "retrieval",
                    },
                    "polishing",
                    (
                        f"审查检索完成，约 {rc} 条来源。"
                        if not sr.get("error")
                        else f"审查检索失败：{sr.get('error') or 'error'}"
                    ),
                ),
                "error": sr.get("error"),
            },
        }
        if sr.get("error"):
            extra_ctx += f"\n\n【联网核查失败】{sr.get('error')}"
        else:
            extra_ctx += f"\n\n【联网核查补充】\n{snip}"
        retry_prompt = (
            l2_prompt
            + extra_ctx
            + "\n\n请结合上述联网信息更新审查结论，若仍需核实可再次输出 <<ACTION: web_search(\"查询词\")>>。"
        )
        r2b, _ = await harness._ask_with_fallback(l2_candidates, retry_prompt, opts_l2, messages=None)
        if r2b.success:
            review_body = r2b.content
        else:
            break
    l3_prompt = (
        f"{l3.get('instruction','').strip()}\n\n【原始问题】\n{question.strip()}\n\n【审查层答案】\n{review_body.strip()}\n"
    )
    l3_candidates = refine_models.get("polish") or [default_model]
    opts_l3 = {**options, "temperature": float(l3.get("temperature", 0.3))}
    yield {
        "event": "step",
        "step": {
            "name": "refine_layer3_polish",
            "status": "running",
            "meta": _pg(
                {**extra_meta, "from_agent": True, "pipeline_phase": "polish"},
                "polishing",
                "润色层：流式生成对用户可见的最终答复…",
            ),
        },
    }
    async for s_event in harness._stream_with_fallback(l3_candidates, l3_prompt, opts_l3, messages=messages):
        yield s_event

    # 与 harness.run_stream 一致：必须补发 ok，否则前端 upsert 后「润色层」永远停在 running，
    # 而后面的 agent_refine_answer ok / 合并后处理卡片会显示已完成，造成状态矛盾。
    yield {
        "event": "step",
        "step": {
            "name": "refine_layer3_polish",
            "status": "ok",
            "meta": _pg(
                {**extra_meta, "from_agent": True, "pipeline_phase": "polish"},
                "polishing",
                "润色层流式输出已完成。",
            ),
        },
    }


def compile_agent_fallback_draft(conv: List[Dict[str, Any]], user_prompt: str, max_chars: int = 12000) -> str:
    """Agent 迭代用尽时：把最近若干轮 assistant 摘要拼成「草稿」供 Refine 兜底。"""
    chunks: List[str] = []
    for m in reversed(conv):
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            chunks.append(c.strip())
        if len(chunks) >= 4:
            break
    body = "\n\n---\n\n".join(reversed(chunks))
    if not body.strip():
        body = "（智能体未产出有效结论，请仅依据用户问题尽力作答。）"
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（已截断）"
    return (
        "【说明】以下为 Agent 多轮推理过程中的助手输出摘录，可能不完整；"
        "请综合审查并给出对用户可直接使用的最终答复。\n\n"
        f"【用户原问题】\n{user_prompt.strip()}\n\n"
        f"【摘录】\n{body}"
    )
