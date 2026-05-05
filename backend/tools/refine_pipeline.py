"""Refine 答案流水线：跳过 Draft，从给定草稿进入 Review（含可选联网核查）+ Polish 流式输出。"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

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
    from refine_shared import Step, _clean_review_body, _int_budget, _pg

    extra_meta = dict(meta_extra or {})
    chain = hcfg.get("refine_chain") or {}
    l2 = chain.get("layer2") or {}
    l3 = chain.get("layer3") or {}
    routing = hcfg.get("routing") or {}
    default_model = routing.get("default_model", "gpt-5.5")
    refine_models = analysis.get("refine_models") or {}
    history_chars = _int_budget(options, "history_context_chars", 4000, minimum=800, maximum=12000)
    review_search_chars = _int_budget(options, "review_search_context_chars", 6000, minimum=1500, maximum=12000)
    l2_prompt = harness._build_refine_layer2_prompt(
        question,
        l2.get("instruction", ""),
        draft_text,
        messages,
        max_history_chars=history_chars,
        options=options,
        extra_review_context=extra_review_context,
    )
    l2_candidates = refine_models.get("review") or [default_model]
    polish_pool = refine_models.get("polish") or [default_model]
    opts_l2 = {**options, "temperature": float(l2.get("temperature", 0.1))}
    r2, a2, polish_ok, polish_tried = await harness._refine_layer2_ask_with_polish_rescue(
        l2_candidates,
        l2_prompt,
        opts_l2,
        polish_pool,
        default_model,
    )
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
                "polish_rescue_attempted": polish_tried,
                "polish_rescue_recovered": polish_ok,
            },
            "polishing",
            "审查层：对照草稿与自检要点核对事实与结构…",
        ),
    )
    yield {"event": "step", "step": step_l2.to_dict()}
    if polish_ok:
        yield {
            "event": "step",
            "step": {
                "name": "refine_layer2_polish_rescue",
                "status": "ok",
                "meta": _pg(
                    {
                        **extra_meta,
                        "model": r2.model,
                        "pipeline_phase": "review",
                        "from_agent": True,
                    },
                    "polishing",
                    "审查模型池失败，已由润色模型池完成同任务补救。",
                ),
            },
        }
    if not r2.success:
        fallback_text = harness._build_refine_layer2_fallback_text(draft_text, "")
        yield {"event": "status", "phase": "fallback", "message": "审查层失败，已回退到草稿结果…"}
        yield {
            "event": "step",
            "step": Step(
                name="refine_degrade_to_layer1",
                status="ok",
                output=fallback_text,
                meta=_pg(
                    {
                        **extra_meta,
                        "reason": "layer2_failed",
                        "from_agent": True,
                        "pipeline_phase": "review",
                    },
                    "polishing",
                    "Agent 审查层失败，已回退到草稿结果继续输出。",
                ),
            ).to_dict(),
        }
        async for s_event in harness._emit_text_chunks(fallback_text, options):
            yield s_event
        return
    review_body = _clean_review_body(r2.content or "")
    async for ev in harness._iter_refine_review_web_rounds(
        review_body,
        l2_prompt,
        l2_candidates,
        opts_l2,
        options,
        review_search_chars,
        "refine",
    ):
        if ev["kind"] == "round_start":
            review_snip_loop = ev["loop"]
            q = ev["query"]
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
        elif ev["kind"] == "after_search":
            review_snip_loop = ev["loop"]
            q = ev["query"]
            sr = ev["sr"]
            rc = ev["result_count"]
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
        elif ev["kind"] == "complete":
            review_body = ev["review_body"]
    l3_prompt = harness._build_refine_layer3_prompt(
        question,
        l3.get("instruction", ""),
        review_body,
        options=options,
        messages=messages,
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
    l3_ok = True
    async for s_event in harness._stream_with_fallback(l3_candidates, l3_prompt, opts_l3, messages=None):
        yield s_event
        if s_event.get("event") == "error":
            l3_ok = False

    # 与 harness.run_stream 一致：必须补发 ok，否则前端 upsert 后「润色层」永远停在 running，
    # 而后面的 agent_refine_answer ok / 合并后处理卡片会显示已完成，造成状态矛盾。
    yield {
        "event": "step",
        "step": {
            "name": "refine_layer3_polish",
            "status": "ok" if l3_ok else "error",
            "meta": _pg(
                {**extra_meta, "from_agent": True, "pipeline_phase": "polish"},
                "polishing",
                "润色层流式输出已完成。" if l3_ok else "润色层流式输出失败，请查看错误事件。",
            ),
            "error": None if l3_ok else "Layer 3 stream failed",
        },
    }


def compile_agent_fallback_draft(conv: List[Dict[str, Any]], user_prompt: str, max_chars: int = 12000) -> str:
    """Agent 迭代用尽时：把最近若干轮 assistant 摘要拼成「草稿」供 Refine 兜底。"""
    last_obs_idx = -1
    for i, m in enumerate(conv):
        if m.get("role") != "user":
            continue
        uc = str(m.get("content") or "")
        if "【观察】" in uc or "联网摘要" in uc:
            last_obs_idx = i

    chunks: List[str] = []
    if last_obs_idx >= 0:
        for m in conv[last_obs_idx + 1 :]:
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                chunks.append(c.strip())
        if len(chunks) > 4:
            chunks = chunks[-4:]
    if not chunks:
        scored: List[tuple] = []
        for i, m in enumerate(conv):
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if not isinstance(c, str) or not c.strip():
                continue
            st = c.strip()
            has_action = 1 if ("<<ACTION" in st or "ACTION:" in st) else 0
            scored.append((has_action, i, st))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = scored[:4]
        top.sort(key=lambda x: x[1])
        chunks = [t[2] for t in top]
    if not chunks:
        for m in reversed(conv):
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                chunks.append(c.strip())
            if len(chunks) >= 4:
                break
        chunks = list(reversed(chunks))
    body = "\n\n---\n\n".join(chunks)
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
