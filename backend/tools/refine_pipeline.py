"""从给定草稿进入文档对齐 Refine Runtime（Critic→Repair→Verify→Finalize）。"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

from refine_runtime_pipeline import iter_refine_runtime_stream


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
    harness: DualTrackHarness 实例。
    extra_review_context: 注入批评上下文（如 Agent 自检），非用户可见正文。
    """
    _tag = harness._make_tagger()
    async for ev in iter_refine_runtime_stream(
        harness,
        question,
        analysis,
        options,
        messages,
        trace_id,
        hcfg,
        _tag,
        entry_block="",
        skip_draft=False,
        prefilled_draft=draft_text,
        critic_hint=extra_review_context or "",
    ):
        yield ev


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
            has_action = 1 if '"action"' in st else 0
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
