"""
文档对齐：Refine = Generate → Critic(JSON) → Repair → Verify → Finalize。
保留 SSE 事件形状（step/chunk/stream_start/status），步骤名对用户认知友好。
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from refine_shared import _int_budget, _pg

from escalation_engine import merge_issues_into_execution_state
from finalize_format import format_finalize_markdown
from runtime_metrics import emit_product_metric, log_runtime_event
from runtime_state import get_execution_state
from search_evidence import evidence_bundle_text, evaluate_search_sufficiency, search_result_to_evidence
from semantic_utils import ngram_overlap_ratio
from unified_critic import evaluate_structured_refine_critic, verify_answer


def _user_status(message: str, phase: str = "refine") -> Dict[str, Any]:
    return {"event": "status", "phase": phase, "message": message, "user_cognitive": True}


def _critic_issue_total(crit: Dict[str, Any]) -> int:
    return sum(
        len(crit.get(k) or [])
        for k in ("missing_points", "logic_issues", "fact_risks", "unsupported_claims")
    )


async def iter_refine_runtime_stream(
    harness: Any,
    prompt: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]],
    trace_id: str,
    hcfg: Dict[str, Any],
    _tag: Callable[..., Dict[str, Any]],
    *,
    entry_block: str = "",
    skip_draft: bool = False,
    prefilled_draft: Optional[str] = None,
    critic_hint: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    refine_ctx = harness._resolve_refine_context(analysis, hcfg)
    chain = refine_ctx["chain"]
    if not chain.get("enabled", True):
        yield {"event": "error", "error": "refine_chain_disabled"}
        return

    l1 = refine_ctx["l1"]
    _l2 = refine_ctx["l2"]
    _l3 = refine_ctx["l3"]
    default_model = refine_ctx["default_model"]
    refine_models = refine_ctx["refine_models"]
    history_chars = _int_budget(options, "history_context_chars", 4000, minimum=800, maximum=12000)

    prompt = harness._prepend_runtime_disclaimer(prompt, options)

    l1_candidates = refine_models.get("draft") or [default_model]
    l1_stream_meta: Dict[str, Any] = {"model": None, "provider": None, "latency_ms": 0}
    l1_failed = False
    draft = ""

    if prefilled_draft is not None:
        draft = prefilled_draft.strip()
        l1_stream_meta["model"] = "prefilled_draft"
        l1_stream_meta["provider"] = "pipeline"
        yield _user_status("已接收上游草稿，跳过重新生成…")
        yield {
            "event": "step",
            "step": {
                "name": "refine_runtime_generate",
                "status": "skipped",
                "meta": _pg({"reason": "prefilled_draft"}, "refine", "使用 Agent / 快轨升级传入的草案。"),
            },
        }
    else:
        yield _user_status("正在生成覆盖问题的初稿…")
        yield {
            "event": "stream_start",
            "track": "refine",
            "trace_id": trace_id,
            "meta": {"stream_phase": "runtime_generate"},
        }
        yield {
            "event": "step",
            "step": {
                "name": "refine_runtime_generate",
                "status": "running",
                "meta": _pg({"phase": "Generate"}, "refine", "生成阶段：优先覆盖要点，不求文采…"),
            },
        }

        if skip_draft:
            draft = str(options.get("search_prompt_base") or prompt or "").strip()
            l1_stream_meta["model"] = "user_draft"
            l1_stream_meta["provider"] = "local"
        else:
            l1_prompt = harness._build_refine_layer1_prompt(
                prompt,
                l1.get("instruction", ""),
                entry_block,
                messages,
                max_history_chars=history_chars,
                options=options,
            )
            buf: List[str] = []
            async for s_event in harness._stream_with_fallback(
                l1_candidates,
                l1_prompt,
                harness._layer_opts(hcfg, "layer1", options),
                messages=None,
                chunk_channel="draft",
            ):
                yield s_event
                if s_event.get("event") == "chunk":
                    buf.append(str((s_event.get("data") or {}).get("content") or ""))
                elif s_event.get("event") == "model_start":
                    l1_stream_meta["model"] = s_event.get("model")
                    l1_stream_meta["provider"] = s_event.get("provider")
                elif s_event.get("event") == "model_end":
                    l1_stream_meta["latency_ms"] = s_event.get("latency_ms", 0)
                elif s_event.get("event") == "error":
                    l1_failed = True
            draft = "".join(buf).strip()

    st = get_execution_state(options)
    if st:
        st.draft_answer = draft

    if prefilled_draft is None:
        yield {
            "event": "step",
            "step": {
                "name": "refine_runtime_generate",
                "status": "ok" if draft and not l1_failed else "error",
                "provider": l1_stream_meta.get("provider"),
                "model": l1_stream_meta.get("model"),
                "latency_ms": int(l1_stream_meta.get("latency_ms") or 0),
                "meta": _pg({"chars": len(draft)}, "refine", "生成阶段完成。"),
                "error": None if draft else "empty_draft",
            },
        }
    if not draft or l1_failed:
        yield {"event": "error", "error": "refine_runtime_generate_failed"}
        return

    stune = (harness.cfg.get("harness") or {}).get("stream_tuning") or {}
    if bool(stune.get("emit_content_reset", True)):
        yield {
            "event": "content_reset",
            "reason": "draft_to_critic",
            "draft_snapshot": draft[:200],
        }

    yield _user_status("正在结构化审查草案…")
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_critic",
            "status": "running",
            "meta": _pg({}, "refine", "批评阶段：列出遗漏、逻辑与事实风险…"),
        },
    }
    review_cands = refine_models.get("review") or [default_model]
    draft_for_critic = draft
    ch = (critic_hint or "").strip()
    if ch:
        draft_for_critic = f"【审查提示（非用户可见正文）】\n{ch[:4000]}\n\n【草案】\n{draft}"
    crit = await evaluate_structured_refine_critic(harness, prompt, draft_for_critic, options, hcfg, review_cands)
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_critic",
            "status": "ok" if crit.get("parse_ok") else "error",
            "meta": _pg({"critic": crit}, "refine", "结构化批评完成。"),
        },
    }

    evidence_serialized: List[Dict[str, Any]] = []
    evidence_objs = []
    if crit.get("needs_search"):
        yield _user_status("正在按需补充检索证据…")
        overrides = {k: v for k, v in harness._track_search_overrides("refine", analysis).items() if v is not None}
        for q in list(crit.get("needs_search") or [])[:3]:
            q = str(q).strip()
            if not q:
                continue
            sr = await harness.perform_web_search(q, {**options, **overrides})
            evs = search_result_to_evidence(sr)
            for e in evs:
                evidence_objs.append(e)
                evidence_serialized.append(e.to_dict())

    ev_text = evidence_bundle_text(evidence_objs)
    if ev_text.strip():
        suff = await evaluate_search_sufficiency(harness, prompt, ev_text, options, hcfg)
        log_runtime_event(
            hcfg,
            {"event": "search_sufficiency", "trace_id": trace_id, "result": suff},
        )
        if not bool(suff.get("sufficient", True)):
            emit_product_metric(hcfg, "search_sufficiency_fail", trace_id=trace_id, result=suff)
        ctab = suff.get("contradictions")
        if isinstance(ctab, list) and ctab:
            emit_product_metric(hcfg, "search_contradiction_rate", trace_id=trace_id, count=len(ctab))

    yield _user_status("正在按批评要点定点修复…")
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_repair",
            "status": "running",
            "meta": _pg({}, "refine", "修复阶段：只改问题句段，不重写全文…"),
        },
    }
    issue_n = _critic_issue_total(crit)
    needs_q = [str(x).strip() for x in (crit.get("needs_search") or []) if str(x).strip()]
    repair_pool = refine_models.get("draft") or review_cands
    r_rep = None
    repaired = draft
    repair_skipped = issue_n == 0 and not needs_q
    if repair_skipped:
        yield {
            "event": "step",
            "step": {
                "name": "refine_runtime_repair",
                "status": "skipped",
                "meta": _pg({"reason": "no_critic_issues"}, "refine", "批评未列出可修复项且无补充检索需求，跳过模型修订。"),
            },
        }
    else:
        repair_prompt = (
            "你是修订编辑。下面给出用户问题、当前草案、结构化批评要点与可选检索证据。\n"
            "要求：仅修复批评中明确列出的问题；不要重写未被点名的正确段落；保持与原问题语言一致；输出完整修订稿正文。\n\n"
            f"【用户问题】\n{(prompt or '')[:4000]}\n\n【当前草案】\n{draft[:10000]}\n\n"
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
        yield {
            "event": "step",
            "step": {
                "name": "refine_runtime_repair",
                "status": "ok" if (r_rep.success if r_rep else True) else "error",
                "provider": r_rep.provider if r_rep else None,
                "model": r_rep.model if r_rep else None,
                "latency_ms": r_rep.latency_ms if r_rep else 0,
                "meta": _pg({"guard_reverted": guard_reverted}, "refine", "修复阶段完成。"),
                "error": r_rep.error if r_rep and not r_rep.success else None,
            },
        }

    yield _user_status("正在验证答案完整性…")
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_verify",
            "status": "running",
            "meta": _pg({}, "refine", "验证阶段：对照问题与证据检查遗漏与冲突…"),
        },
    }
    st_ver = get_execution_state(options)
    before_verify = float(st_ver.quality_score) if st_ver else 0.0
    uc = await verify_answer(
        harness,
        prompt,
        repaired,
        analysis,
        options,
        hcfg,
        search_context=ev_text,
    )
    merge_issues_into_execution_state(options, uc)
    try:
        after_verify = float(uc.get("quality_score") or 0.0)
        dq = after_verify - before_verify
        emit_product_metric(
            hcfg,
            "refine_quality_delta",
            trace_id=trace_id,
            before_verify=before_verify,
            after_verify=after_verify,
            delta=dq,
        )
        if dq >= 0.15:
            emit_product_metric(hcfg, "refine_improved_score", trace_id=trace_id, delta=dq)
        elif dq <= -0.15:
            emit_product_metric(hcfg, "refine_regression_rate", trace_id=trace_id, delta=dq)
    except (TypeError, ValueError):
        pass
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_verify",
            "status": "ok",
            "meta": _pg({"unified_critic": uc}, "refine", "验证评估完成。"),
        },
    }

    yield _user_status("正在最终排版与结构化输出…")
    yield {
        "event": "stream_start",
        "track": "refine",
        "trace_id": trace_id,
        "meta": {"stream_phase": "runtime_finalize"},
    }
    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_finalize",
            "status": "running",
            "meta": _pg({"mode": "deterministic"}, "refine", "终稿阶段：确定性 Markdown 整理（无模型调用），不改事实。"),
        },
    }

    final_text = format_finalize_markdown(repaired)
    st_fin = get_execution_state(options)
    if st_fin:
        st_fin.final_answer = final_text

    l3_ok = True
    async for s_event in harness._emit_text_chunks(final_text, options, channel="final"):
        yield s_event

    yield {
        "event": "step",
        "step": {
            "name": "refine_runtime_finalize",
            "status": "ok" if l3_ok else "error",
            "meta": _pg({"mode": "deterministic_markdown"}, "refine", "终稿输出完成（确定性排版）。"),
            "error": None if l3_ok else "finalize_stream_failed",
        },
    }
    log_runtime_event(hcfg, {"event": "refine_runtime_complete", "trace_id": trace_id})
    emit_product_metric(hcfg, "refine_success_rate", trace_id=trace_id, ok=bool(l3_ok))
