"""Parallel Runtime Nodes：由 DAGScheduler 按依赖波次 gather 执行；事件经 ctx.emit 流式输出。"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List

from finalize_format import format_finalize_markdown
from refine_shared import _int_budget, _pg
from runtime.dag_common import build_search_queries, user_status
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.metrics_hooks import record_parallelism
from runtime.nodes import critic_node, repair_node, verify_node
from runtime.nodes.search_pipeline import prepare_parallel_queries, prepare_parallel_queries_llm, rerank_search_evidence
from runtime.quality.critic_engine import (
    FACET_LAYER_DEFS,
    facets_bundle_to_structured,
    merge_structured_with_parallel,
    run_single_facet_review,
)
from runtime.quality.hallucination_detector import hallucination_score_from_critics
from runtime.quality.repair_engine import critic_issue_total
from runtime.parallel.hedged_requests import race_tasks
from runtime.parallel.parallel_search import parallel_web_search
from runtime.state.evidence_state import EvidenceGraph
from runtime.state.semantic_memory import remember_runtime_turn
from runtime_metrics import emit_product_metric, log_runtime_event
from runtime_state import need_search_allowed
from search_evidence import evaluate_search_sufficiency, evidence_bundle_text, search_result_to_evidence
from unified_critic import evaluate_structured_quality_critic, evaluate_unified_critic


_SYNTHESIS_PROMPT = """\
你是一位专业编辑，收到了对同一问题的两个并行草稿（A 稿和 B 稿）。
请从两稿中提取最佳内容，合并为一份完整、准确、结构清晰的终稿。
规则：
1. 保留两稿中质量更高的段落/论据；
2. 消除重复内容；
3. 修正逻辑断层；
4. 保持与原问题相同的语言；
5. 直接输出终稿正文，不要加任何说明文字。

【用户问题】
{prompt}

【A 稿】
{draft_a}

【B 稿】
{draft_b}

【合并终稿】
"""


async def _synthesize_drafts(
    harness: Any,
    prompt: str,
    da: str,
    db: str,
    opts: Dict[str, Any],
    models: List[str],
) -> str:
    """调用 LLM 将 A/B 两稿合并为最优终稿。失败则返回质量分更高的那个。"""
    syn_prompt = _SYNTHESIS_PROMPT.format(
        prompt=prompt[:800],
        draft_a=da[:5000],
        draft_b=db[:5000],
    )
    try:
        r, _ = await harness._ask_with_fallback(models, syn_prompt, opts, messages=None)
        if r and r.success and r.content and len(r.content.strip()) > 80:
            return r.content.strip()
    except Exception:
        pass
    return da if _draft_quality_score(da) >= _draft_quality_score(db) else db


def _draft_quality_score(text: str) -> float:
    """并行草稿选优：结构丰富度（标题/列表/代码块）+ 归一化长度，替代纯字符长度代理。"""
    if not text:
        return 0.0
    t = str(text)
    headers = len(re.findall(r"^#{1,4}\s", t, re.MULTILINE))
    bullets = len(re.findall(r"^[\-\*\+]\s", t, re.MULTILINE))
    numbered = len(re.findall(r"^\d+\.\s", t, re.MULTILINE))
    code = len(re.findall(r"```", t))
    # 结构分（最多 0.4）：每个结构元素贡献细粒度分
    structure = min(0.4, (headers * 0.05 + bullets * 0.02 + numbered * 0.02 + code * 0.04))
    # 长度分（最多 0.6）：目标长度 ~1200 字符时满分，过短惩罚，过长不再加分
    length_score = min(0.6, len(t) / 2000.0)
    return structure + length_score


def _minimal_budget_abort_critic_merged() -> Dict[str, Any]:
    return merge_structured_with_parallel(
        {"issues": [], "parse_ok": False, "recommended_action": "accept"},
        {
            "missing_points": [],
            "logic_issues": [],
            "fact_risks": [],
            "unsupported_claims": [],
            "needs_search": [],
            "confidence": 0.5,
            "parse_ok": False,
        },
    )


def _quality_round_budget_ok(ctx: DAGRuntimeContext, round_idx: int) -> bool:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return False
    intent = ctx.intent
    budget = ctx.budget
    t0 = ctx.t0
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id
    elapsed_loop = time.perf_counter() - t0
    if budget.latency_exceeded(intent.latency_budget, elapsed_loop):
        emit_product_metric(
            hcfg,
            "latency_breakdown",
            trace_id=trace_id,
            reason="latency_budget_short_circuit",
            round=round_idx,
        )
        return False
    if budget.token_budget_exceeded(intent.latency_budget):
        emit_product_metric(
            hcfg,
            "latency_breakdown",
            trace_id=trace_id,
            reason="token_budget_short_circuit",
            round=round_idx,
        )
        return False
    return True


async def _run_search_followup_block(ctx: DAGRuntimeContext, round_idx: int, merged: Dict[str, Any]) -> None:
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    trace_id = ctx.trace_id
    hcfg = ctx.hcfg
    blocked = ctx.blocked
    overrides = ctx.overrides
    evidence_objs = ctx.evidence_objs
    ev_text = ctx.ev_text

    extra_q = [str(x).strip() for x in (merged.get("needs_search") or [])[:3] if str(x).strip()]
    if not extra_q or blocked or not need_search_allowed(opt):
        ctx.evidence_objs = evidence_objs
        ctx.ev_text = ev_text
        return
    ctx.enable_capability("search", reason="critic_needs_search")
    emit_product_metric(hcfg, "escalation_rate", trace_id=trace_id, capability="search", reason="critic_needs_search")
    emit_product_metric(hcfg, "dag_parallelism", trace_id=trace_id, phase="search_followup", width=len(extra_q))
    record_parallelism(opt, len(extra_q))
    pairs2 = await parallel_web_search(h, extra_q, {**opt, **overrides}, overrides=None)
    if st:
        for q in extra_q:
            if q and q not in st.search_history:
                st.search_history.append(str(q)[:500])
    ctx.max_wave_parallel = max(ctx.max_wave_parallel, len(pairs2))
    evidence_objs.extend([e for _, sr in pairs2 for e in search_result_to_evidence(sr)])
    evidence_objs = rerank_search_evidence(evidence_objs, top_k=72)
    for _, sr in pairs2:
        h._capture_search_evidence_for_runtime(opt, sr if isinstance(sr, dict) else {})
    eg_follow = EvidenceGraph.from_search_evidence(evidence_objs)
    eg_follow.apply_freshness_heuristic_from_year_tokens()
    ctx.evidence_graph = eg_follow
    if st:
        snap_f = eg_follow.to_dict()
        snap_f["freshness"] = eg_follow.freshness_analysis()
        snap_f["source_weights"] = eg_follow.source_weighting_summary()
        st.evidence_graph_snapshot = snap_f
        for hnt in eg_follow.contradiction_hints():
            st.contradictions.append(hnt)
    ev_text = evidence_bundle_text(evidence_objs)
    ctx.ev_text = ev_text
    ctx.evidence_objs = evidence_objs
    if ev_text.strip():
        suff = await evaluate_search_sufficiency(h, ctx.prompt, ev_text, opt, hcfg)
        log_runtime_event(hcfg, {"event": "search_sufficiency", "trace_id": trace_id, "result": suff})
        if not bool(suff.get("sufficient", True)):
            emit_product_metric(hcfg, "search_sufficiency_fail", trace_id=trace_id, result=suff)
        else:
            emit_product_metric(hcfg, "search_sufficiency_rate", trace_id=trace_id, sufficient=True)


async def _apply_verify_round_outcome(ctx: DAGRuntimeContext, round_idx: int, draft: str, uc: Dict[str, Any]) -> None:
    st = ctx.st
    plan = ctx.plan
    opt = ctx.options
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id
    blocked = ctx.blocked
    ra = str(uc.get("recommended_action") or "accept").lower()
    if ra in ("accept", "reject"):
        if ra == "reject":
            emit_product_metric(hcfg, "verify_failure_rate", trace_id=trace_id, action=ra)
        ctx._quality_done = True
        if ra == "accept":
            snap = draft.strip()
            if snap:
                await ctx.emit(
                    {
                        "event": "chunk",
                        "data": {
                            "content": snap[:520] + ("…" if len(snap) > 520 else ""),
                            "channel": "partial_final",
                            "round": round_idx,
                        },
                    }
                )
    elif ra == "search_more" and not blocked and need_search_allowed(opt) and round_idx + 1 < plan.repair_rounds_max:
        ctx.enable_capability("search", reason="verify_search_more")
        emit_product_metric(hcfg, "escalation_rate", trace_id=trace_id, capability="search", reason="verify_search_more")


async def _publish_critic_merge_metadata(ctx: DAGRuntimeContext, round_idx: int, merged: Dict[str, Any]) -> None:
    plan = ctx.plan
    st = ctx.st
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id
    opt = ctx.options
    uni_m = merged.get("_unified") or {}
    struct_m = merged.get("_structured") or {}
    if plan.layered_critics:
        ctx.max_wave_parallel = max(ctx.max_wave_parallel, 6)
        record_parallelism(opt, 6)
    elif plan.parallel_critics:
        ctx.max_wave_parallel = max(ctx.max_wave_parallel, 2)
        record_parallelism(opt, 2)
    if st:
        st.critic_reports.append({"round": round_idx, "merged": merged})
        st.hallucination_risk = hallucination_score_from_critics(uni_m, struct_m)
        emit_product_metric(
            hcfg,
            "hallucination_rate",
            trace_id=trace_id,
            risk=float(st.hallucination_risk),
            layered=plan.layered_critics,
            dag_wave=True,
        )
        ctrs = (merged.get("_structured") or {}).get("fact_risks") or []
        if isinstance(ctrs, list):
            st.contradictions.extend([str(x) for x in ctrs[:6]])
    _issue_n = critic_issue_total(merged)
    _ra = str(merged.get("recommended_action") or "accept").lower()
    _needs_repair = _ra in ("repair", "search_more") or _issue_n > 0
    _struct = merged.get("_structured") or {}
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_parallel_critic",
                "status": "ok",
                "meta": _pg(
                    {
                        "round": round_idx,
                        "issue_total": _issue_n,
                        "needs_repair": _needs_repair,
                        "recommended_action": _ra,
                        "layered_critics": plan.layered_critics,
                        "paired_parallel": plan.parallel_critics and not plan.layered_critics,
                        "facets": ["coverage", "logic", "evidence", "hallucination", "policy"],
                        "missing_points": (_struct.get("missing_points") or [])[:8],
                        "logic_issues": (_struct.get("logic_issues") or [])[:6],
                        "fact_risks": (_struct.get("fact_risks") or [])[:6],
                        "unsupported_claims": (_struct.get("unsupported_claims") or [])[:6],
                        "scheduler_gather_nodes": True,
                    },
                    "evaluate",
                    "并行批评完成（DAG 多节点 gather）。",
                ),
            },
        }
    )


def _critic_cache_key(ctx: DAGRuntimeContext, round_idx: int) -> str:
    draft = ctx.draft
    ev_text = ctx.ev_text
    return f"{hash(draft[:2000])}:{round_idx}:{hash(ev_text[:1200])}"


async def node_parallel_search(ctx: DAGRuntimeContext) -> None:
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    plan = ctx.plan
    trace_id = ctx.trace_id
    hcfg = ctx.hcfg

    ctx.search_pairs = []
    if plan.parallel_searches > 0 and not ctx.blocked and need_search_allowed(opt):
        dgc = hcfg.get("dag_runtime") if isinstance(hcfg.get("dag_runtime"), dict) else {}
        use_llm_rewrite = bool(dgc.get("llm_query_rewrite", True))
        if use_llm_rewrite:
            queries = await prepare_parallel_queries_llm(
                ctx.prompt,
                ctx.analysis,
                h,
                opt,
                hcfg,
                n=plan.parallel_searches,
                entry_search_required=ctx.entry_search_required,
                search_reason=ctx.search_reason or "",
                seed_builder=build_search_queries,
            )
        else:
            queries = prepare_parallel_queries(
                ctx.prompt,
                ctx.analysis,
                n=plan.parallel_searches,
                entry_search_required=ctx.entry_search_required,
                search_reason=ctx.search_reason or "",
                seed_builder=build_search_queries,
            )
        if queries:
            if st:
                for q in queries:
                    if q and q not in st.search_history:
                        st.search_history.append(str(q)[:500])

            # EvidenceCache 读：相同查询集命中则跳过网络检索
            ev_cache_key = "|".join(sorted(q[:200] for q in queries))
            ev_cache_hit = ctx.caches.evidence.get(ev_cache_key) if ctx.caches else None
            if ev_cache_hit and isinstance(ev_cache_hit, dict):
                ctx.evidence_objs = ev_cache_hit.get("evidence_objs") or []
                ctx.ev_text = ev_cache_hit.get("ev_text") or ""
                ctx.search_pairs = ev_cache_hit.get("search_pairs") or []
                if st:
                    st.runtime_memory.append({"phase": "parallel_search", "evidence_cache_hit": True,
                                              "evidence_nodes": len(ctx.evidence_objs)})
                await ctx.emit({"event": "step", "step": {
                    "name": "dag_parallel_search", "status": "ok",
                    "meta": _pg({"evidence_cache_hit": True, "evidence_nodes": len(ctx.evidence_objs)},
                                "search", "EvidenceCache 命中，跳过网络检索。"),
                }})
            else:
                emit_product_metric(hcfg, "dag_parallelism", trace_id=trace_id, phase="search", width=len(queries))
                record_parallelism(opt, len(queries))
                await ctx.emit(user_status("并行检索证据…", phase="search"))
                await ctx.emit(
                    {
                        "event": "step",
                        "step": {
                            "name": "dag_parallel_search",
                            "status": "running",
                            "meta": _pg({"queries": queries, "n": len(queries)}, "search", "DAG：并行 Search Nodes"),
                        },
                    }
                )
                ctx.search_pairs = await parallel_web_search(h, queries, opt, overrides=ctx.overrides)
                for _q, sr in ctx.search_pairs:
                    h._capture_search_evidence_for_runtime(opt, sr if isinstance(sr, dict) else {})
                await ctx.emit(
                    {
                        "event": "step",
                        "step": {
                            "name": "dag_parallel_search",
                            "status": "ok",
                            "meta": _pg({"completed": len(ctx.search_pairs)}, "search", "并行检索完成。"),
                        },
                    }
                )

                # EvidenceCache 写（组装前先存 search_pairs）
                _ev_objs_tmp = []
                for _, sr in ctx.search_pairs:
                    if isinstance(sr, dict):
                        _ev_objs_tmp.extend(search_result_to_evidence(sr))
                _ev_objs_tmp = rerank_search_evidence(_ev_objs_tmp, top_k=64)
                _ev_text_tmp = evidence_bundle_text(_ev_objs_tmp) if _ev_objs_tmp else ""
                if ctx.caches and _ev_objs_tmp:
                    ctx.caches.evidence.put(ev_cache_key, {
                        "evidence_objs": _ev_objs_tmp,
                        "ev_text": _ev_text_tmp,
                        "search_pairs": ctx.search_pairs,
                    })
                ctx.evidence_objs = _ev_objs_tmp
                ctx.ev_text = _ev_text_tmp
            _ev_assembled = True
        else:
            _ev_assembled = False
    else:
        _ev_assembled = False

    if not _ev_assembled:
        # 无 EvidenceCache 命中且无查询时：从 search_pairs 原始组装（或生成空集）
        ctx.evidence_objs = []
        for _, sr in ctx.search_pairs:
            if isinstance(sr, dict):
                ctx.evidence_objs.extend(search_result_to_evidence(sr))
        ctx.evidence_objs = rerank_search_evidence(ctx.evidence_objs, top_k=64)
        ctx.ev_text = evidence_bundle_text(ctx.evidence_objs) if ctx.evidence_objs else ""

    eg = EvidenceGraph.from_search_evidence(ctx.evidence_objs)
    eg.apply_freshness_heuristic_from_year_tokens()
    ctx.evidence_graph = eg
    if st:
        st.evidence_graph_summary = ctx.ev_text[:4000]
        st.evidence_nodes = [e.to_dict() for e in ctx.evidence_objs[-64:]]
        snap = eg.to_dict()
        snap["freshness"] = eg.freshness_analysis()
        snap["source_weights"] = eg.source_weighting_summary()
        st.evidence_graph_snapshot = snap
        hints = eg.contradiction_hints()
        if hints:
            st.contradictions.extend(hints)
        st.runtime_memory.append({"phase": "parallel_search", "evidence_nodes": len(eg.nodes), "rerank": True})
    if len(ctx.evidence_objs) > 2 and ctx.intent is not None:
        try:
            ctx.intent.search_score = min(1.0, float(ctx.intent.search_score) + 0.05)
            if st:
                st.runtime_memory.append({"phase": "post_search_intent_refresh", "search_score": ctx.intent.search_score})
        except (TypeError, ValueError):
            pass


async def node_parallel_draft(ctx: DAGRuntimeContext) -> None:
    if ctx.should_stop:
        return
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    plan = ctx.plan
    intent = ctx.intent
    trace_id = ctx.trace_id
    hcfg = ctx.hcfg
    messages = ctx.messages

    ctx.prompt_disc = h._prepend_runtime_disclaimer(h._attach_documents_to_prompt(ctx.prompt, opt), opt)
    ctx.quality_ctx = h._resolve_quality_context(ctx.analysis, hcfg)
    ctx.chain_on = bool(ctx.quality_ctx["chain"].get("enabled", True))
    ctx.default_model = ctx.quality_ctx["default_model"]
    ctx.quality_models = ctx.quality_ctx["quality_models"]
    ctx.review_cands = ctx.quality_models.get("review") or [ctx.default_model]
    ctx.repair_pool = ctx.quality_models.get("draft") or ctx.review_cands
    ctx.history_chars = _int_budget(opt, "history_context_chars", 4000, minimum=800, maximum=12000)

    ctx.use_quality_layers = ctx.chain_on and intent.quality_requirement == "high"
    if ctx.use_quality_layers:
        l1 = ctx.quality_ctx["l1"]
        ctx.base_prompt = h._build_quality_layer1_prompt(
            ctx.prompt,
            l1.get("instruction", ""),
            ctx.ev_text[:6000],
            messages,
            max_history_chars=ctx.history_chars,
            options=opt,
        )
        ctx.draft_candidates = ctx.quality_models.get("draft") or [ctx.default_model]
        if opt.get("_dag_cost_efficient") and isinstance(ctx.draft_candidates, list) and len(ctx.draft_candidates) > 1:
            ctx.draft_candidates = ctx.draft_candidates[:1]
        ctx.draft_messages = None
    else:
        ctx.base_prompt = ctx.prompt_disc
        route_fm = h.resolve_model_route(ctx.prompt, ctx.analysis)
        ctx.draft_candidates = route_fm.get("candidates") or [route_fm.get("selected")]
        if opt.get("_dag_cost_efficient") and isinstance(ctx.draft_candidates, list) and len(ctx.draft_candidates) > 1:
            ctx.draft_candidates = ctx.draft_candidates[:1]
        ctx.draft_messages = messages

    await ctx.emit(user_status("并行起草生成…", phase="draft"))
    await ctx.emit(
        {"event": "stream_start", "runtime": "adaptive_dag_v3", "phase": "draft", "trace_id": trace_id, "meta": {"stream_phase": "dag_draft", "runtime": "adaptive_dag_v3", "phase": "draft"}}
    )
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_draft",
                "status": "running",
                "meta": _pg(
                    {
                        "quality": intent.quality_requirement,
                        "chain": ctx.chain_on,
                        "parallel_drafts": plan.parallel_drafts,
                        "hedge_ms": plan.hedge_draft_delay_ms,
                    },
                    "draft",
                    "DAG：Draft Node（可多稿并行 / Hedged）",
                ),
            },
        }
    )

    buf: List[str] = []
    l1_stream_meta: Dict[str, Any] = {"model": None, "provider": None, "latency_ms": 0}
    draft = ""
    dgc = hcfg.get("dag_runtime") if isinstance(hcfg.get("dag_runtime"), dict) else {}
    if bool(dgc.get("semantic_cache_short_circuit")) and ctx.caches and isinstance(ctx.intent_dict, dict) and ctx.intent_dict:
        ib = tuple(sorted((str(k), str(v)[:240]) for k, v in ctx.intent_dict.items()))
        sig = f"{opt.get('_history_signature', '')}|{opt.get('_documents_signature', '')}"[:500]
        hit = ctx.caches.semantic.get(ib, sig)
        if isinstance(hit, str) and len(hit.strip()) > 80:
            draft = hit.strip()
            l1_stream_meta = {"model": "semantic_cache", "provider": None, "latency_ms": 0}
            await ctx.emit(
                {
                    "event": "step",
                    "step": {
                        "name": "dag_draft",
                        "status": "ok",
                        "meta": _pg({"semantic_cache_hit": True, "chars": len(draft)}, "draft", "语义缓存命中，跳过起草 LLM。"),
                    },
                }
            )
            async for s_event in h._emit_text_chunks(draft, opt, channel="draft"):
                await ctx.emit(s_event)
            ctx.draft = draft
            if st:
                st.draft_answer = draft
            if st:
                st.runtime_memory.append({"phase": "draft", "ok": True, "chars": len(draft), "semantic_cache": True})
            if ctx.caches and draft:
                dk = f"{hash(ctx.ev_text[:900])}:{hash(ctx.base_prompt[:700])}"
                ctx.caches.draft.put(dk, draft)
            h0 = h.cfg.get("harness") or {}
            stune = h0.get("stream_tuning") or {}
            snap = draft.strip()
            if snap:
                await ctx.emit(
                    {
                        "event": "chunk",
                        "data": {"content": snap[:420] + ("…" if len(snap) > 420 else ""), "channel": "preliminary"},
                    }
                )
            if bool(stune.get("emit_content_reset", True)):
                await ctx.emit({"event": "content_reset", "reason": "dag_draft_to_critic", "draft_snapshot": draft[:200]})
            ctx.max_wave_parallel = max(1, len(ctx.search_pairs) if ctx.search_pairs else 1)
            return

    cost_skip_parallel = ctx.budget.cost_pause_parallel_draft(intent.latency_budget, intent.quality_requirement)
    if plan.parallel_drafts and ctx.use_quality_layers and not cost_skip_parallel:
        opts_pd = h._layer_opts(hcfg, "layer1", opt)

        async def _pd(px: str):
            r, _att = await h._ask_with_fallback(ctx.draft_candidates, px, opts_pd, messages=None)
            return ((r.content or "").strip() if r and r.success else "", r)

        alt_px = ctx.base_prompt + "\n\n【并行稿 B】请采用不同的章节结构与论证顺序，补充可比视角；避免与 A 稿句式雷同。"
        (da, ra), (db, rb) = await asyncio.gather(_pd(ctx.base_prompt), _pd(alt_px))
        emit_product_metric(hcfg, "dag_parallelism", trace_id=trace_id, phase="parallel_draft", width=2)
        record_parallelism(opt, 2)
        _tokens_in = (getattr(ra, "tokens_in", 0) or 0) + (getattr(rb, "tokens_in", 0) or 0)
        _tokens_out_ab = (getattr(ra, "tokens_out", 0) or 0) + (getattr(rb, "tokens_out", 0) or 0)
        # max_quality_mode: 调 LLM 合并两稿; 否则质量评分选优
        _dgci = hcfg.get("dag_runtime") if isinstance(hcfg.get("dag_runtime"), dict) else {}
        do_synthesis = bool(_dgci.get("max_quality_mode")) and bool(da) and bool(db)
        if do_synthesis:
            draft = await _synthesize_drafts(h, ctx.prompt, da, db,
                                             h._layer_opts(hcfg, "layer1", opt), ctx.draft_candidates)
            ctx.budget.note_llm_cost(tokens_in=_tokens_in,
                                     tokens_out=_tokens_out_ab + max(len(draft) // 4, 0))
        else:
            draft = da if _draft_quality_score(da) >= _draft_quality_score(db) else db
            ctx.budget.note_llm_cost(tokens_in=_tokens_in,
                                     tokens_out=_tokens_out_ab or max(len(draft) // 4, 0))
        async for s_event in h._emit_text_chunks(draft, opt, channel="draft"):
            await ctx.emit(s_event)
        # synthesis produces new text ≠ da or db; don't use pick for synthesis path
        pick = None if do_synthesis else (ra if draft == da else rb)
        l1_stream_meta["model"] = ("synthesis" if do_synthesis
                                   else (getattr(pick, "model", None) if pick else "parallel_draft"))
        l1_stream_meta["provider"] = getattr(pick, "provider", None) if pick else None
        l1_stream_meta["latency_ms"] = int(getattr(pick, "latency_ms", 0) or 0) if pick else 0
        l1_stream_meta["tokens_in"] = _tokens_in
        l1_stream_meta["tokens_out"] = _tokens_out_ab or max(len(draft) // 4, 0)
        l1_stream_meta["parallel_mode"] = True
        l1_stream_meta["synthesized"] = do_synthesis
        l1_stream_meta["draft_quality_score"] = round(_draft_quality_score(draft), 4)
    elif plan.hedge_draft_delay_ms > 0:
        opts_hd = h._layer_opts(hcfg, "layer1", opt)
        route_a = h.resolve_model_route(ctx.prompt, ctx.analysis)
        pri = route_a.get("candidates") or [route_a.get("selected")]
        if opt.get("_dag_cost_efficient") and isinstance(pri, list) and len(pri) > 1:
            pri = pri[:1]
        sec = ctx.quality_models.get("draft") or pri
        if sec == pri:
            sec = pri

        async def _primary():
            r, _ = await h._ask_with_fallback(pri, ctx.base_prompt, opts_hd, messages=ctx.draft_messages)
            return r

        async def _backup():
            r, _ = await h._ask_with_fallback(sec, ctx.base_prompt, opts_hd, messages=ctx.draft_messages)
            return r

        win = await race_tasks(_primary(), _backup(), delay_s=plan.hedge_draft_delay_ms / 1000.0)
        draft = (win.content or "").strip() if win and getattr(win, "success", False) else ""
        # 真实 token 计数
        _tok_in = getattr(win, "tokens_in", 0) or 0
        _tok_out = getattr(win, "tokens_out", 0) or 0
        ctx.budget.note_llm_cost(tokens_in=_tok_in, tokens_out=_tok_out or max(len(draft) // 4, 0))
        async for s_event in h._emit_text_chunks(draft, opt, channel="draft"):
            await ctx.emit(s_event)
        l1_stream_meta["model"] = getattr(win, "model", None) if win else None
        l1_stream_meta["provider"] = getattr(win, "provider", None) if win else None
        l1_stream_meta["latency_ms"] = int(getattr(win, "latency_ms", 0) or 0) if win else 0
        l1_stream_meta["tokens_in"] = _tok_in
        l1_stream_meta["tokens_out"] = _tok_out or max(len(draft) // 4, 0)
        l1_stream_meta["parallel_mode"] = False
        l1_stream_meta["synthesized"] = False
        l1_stream_meta["draft_quality_score"] = round(_draft_quality_score(draft), 4)
    else:
        async for s_event in h._stream_with_fallback(
            ctx.draft_candidates,
            ctx.base_prompt,
            h._layer_opts(hcfg, "layer1", opt),
            messages=ctx.draft_messages,
            chunk_channel="draft",
        ):
            await ctx.emit(s_event)
            if s_event.get("event") == "chunk":
                buf.append(str((s_event.get("data") or {}).get("content") or ""))
            elif s_event.get("event") == "model_start":
                l1_stream_meta["model"] = s_event.get("model")
                l1_stream_meta["provider"] = s_event.get("provider")
            elif s_event.get("event") == "model_end":
                l1_stream_meta["latency_ms"] = s_event.get("latency_ms", 0)
        draft = "".join(buf).strip()

    ctx.draft = draft
    if st:
        st.draft_answer = draft

    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_draft",
                "status": "ok" if draft else "error",
                "provider": l1_stream_meta.get("provider"),
                "model": l1_stream_meta.get("model"),
                "latency_ms": int(l1_stream_meta.get("latency_ms") or 0),
                "meta": _pg(
                    {
                        "chars": len(draft),
                        "draft_len": len(draft),
                        "model": l1_stream_meta.get("model"),
                        "provider": l1_stream_meta.get("provider"),
                        "tokens_in": l1_stream_meta.get("tokens_in"),
                        "tokens_out": l1_stream_meta.get("tokens_out"),
                        "draft_quality_score": l1_stream_meta.get("draft_quality_score"),
                        "parallel_mode": l1_stream_meta.get("parallel_mode"),
                        "synthesized": l1_stream_meta.get("synthesized"),
                    },
                    "draft",
                    "起草完成。",
                ),
                "error": None if draft else "empty_draft",
            },
        }
    )
    if not draft:
        emit_product_metric(hcfg, "draft_generation_fail", trace_id=trace_id, phase="dag_draft_empty")
        if st:
            st.failed_attempts.append("dag_draft_empty")
            st.runtime_memory.append({"phase": "draft", "ok": False})
        await ctx.emit({"event": "error", "error": "dag_draft_failed"})
        remember_runtime_turn(opt, intent=ctx.intent_dict, ok=False)
        ctx.should_stop = True
        return

    if st:
        st.runtime_memory.append({"phase": "draft", "ok": True, "chars": len(draft)})
    if ctx.caches and draft and isinstance(ctx.intent_dict, dict) and ctx.intent_dict:
        ib = tuple(sorted((str(k), str(v)[:240]) for k, v in ctx.intent_dict.items()))
        sig = f"{opt.get('_history_signature', '')}|{opt.get('_documents_signature', '')}"[:500]
        ctx.caches.semantic.put(ib, sig, draft[:12000])
    if ctx.caches and draft:
        dk = f"{hash(ctx.ev_text[:900])}:{hash(ctx.base_prompt[:700])}"
        ctx.caches.draft.put(dk, draft)

    h0 = h.cfg.get("harness") or {}
    stune = h0.get("stream_tuning") or {}
    snap = draft.strip()
    if snap:
        await ctx.emit(
            {
                "event": "chunk",
                "data": {"content": snap[:420] + ("…" if len(snap) > 420 else ""), "channel": "preliminary"},
            }
        )
    if bool(stune.get("emit_content_reset", True)):
        await ctx.emit({"event": "content_reset", "reason": "dag_draft_to_critic", "draft_snapshot": draft[:200]})

    ctx.max_wave_parallel = max(1, len(ctx.search_pairs) if ctx.search_pairs else 1)


async def node_tool_capability_gate(ctx: DAGRuntimeContext) -> None:
    """Planner 启用 tool 门控时插入：显式 tool_use 能力（§一 Tool Nodes）。"""
    from runtime.nodes import tool_node

    await tool_node.execute_tool_gate(ctx)


async def node_goal_capability_gate(ctx: DAGRuntimeContext) -> None:
    """Planner 启用 goal_subgraph 时插入：目标门控。"""
    from runtime.nodes import agent_node

    await agent_node.execute_optional(ctx)


def make_quality_round_runner(round_idx: int):
    """供 DAG 动态节点绑定单轮 quality（scheduler 波次间 barrier）。"""

    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_quality_round(ctx, round_idx)

    _run.__name__ = f"quality_round_{round_idx}"
    return _run


async def node_quality_round(ctx: DAGRuntimeContext, round_idx: int) -> None:
    """单轮：critic →（可选追加检索）→ repair → verify；提前验收则标记 _quality_done。"""
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    evidence_objs = ctx.evidence_objs
    ev_text = ctx.ev_text
    draft = ctx.draft

    if not _quality_round_budget_ok(ctx, round_idx):
        ctx._quality_done = True
        ctx.evidence_objs = evidence_objs
        ctx.ev_text = ev_text
        return

    merged = await critic_node.execute_round(ctx, draft, ev_text, round_idx)
    await _run_search_followup_block(ctx, round_idx, merged)
    ev_text = ctx.ev_text

    _r_rep, repaired, _guard = await repair_node.execute_round(ctx, draft, merged, ev_text, round_idx)
    draft = repaired
    ctx.draft = draft

    uc = await verify_node.execute_round(ctx, draft, ev_text, round_idx)
    await _apply_verify_round_outcome(ctx, round_idx, draft, uc)


async def node_quality_prelude(ctx: DAGRuntimeContext, round_idx: int) -> None:
    """预算门控 + critic 缓存短路（后续 critic 节点遇 skip 则 no-op）。"""
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    if not _quality_round_budget_ok(ctx, round_idx):
        ctx._quality_done = True
        ctx._skip_critic_wave[round_idx] = True
        ctx._critic_merged_by_round[round_idx] = _minimal_budget_abort_critic_merged()
        return
    crit_key = _critic_cache_key(ctx, round_idx)
    if ctx.caches:
        hit = ctx.caches.critic.get(crit_key)
        if hit:
            ctx._critic_merged_by_round[round_idx] = hit
            ctx._skip_critic_wave[round_idx] = True
            if ctx.st:
                ctx.st.runtime_memory.append({"phase": "critic_cache_hit", "round": round_idx})


async def node_critic_unified_layer(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False) or ctx._skip_critic_wave.get(round_idx):
        return
    h, opt, hcfg = ctx.harness, ctx.options, ctx.hcfg
    uni = await evaluate_unified_critic(
        h,
        ctx.prompt,
        ctx.draft,
        ctx.analysis,
        opt,
        hcfg,
        search_context=ctx.ev_text,
        mode="general",
    )
    ctx._critic_unified_by_round[round_idx] = uni


async def node_critic_facet_layer(ctx: DAGRuntimeContext, round_idx: int, facet_key: str) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False) or ctx._skip_critic_wave.get(round_idx):
        return
    instr = ""
    for k, ins in FACET_LAYER_DEFS:
        if k == facet_key:
            instr = ins
            break
    row = await run_single_facet_review(
        ctx.harness,
        facet_key,
        instr,
        ctx.prompt,
        ctx.draft,
        ctx.ev_text,
        ctx.options,
        ctx.hcfg,
        ctx.review_cands,
    )
    ctx._critic_facets_by_round.setdefault(round_idx, {})[facet_key] = row


async def node_critic_merge_layered(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    plan = ctx.plan
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id
    emit_product_metric(hcfg, "critic_trigger_rate", trace_id=trace_id, round=round_idx, dag=True)
    await ctx.emit(user_status(f"并行批评（第 {round_idx + 1}/{plan.repair_rounds_max} 轮）…", phase="evaluate"))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_parallel_critic",
                "status": "running",
                "meta": _pg({"round": round_idx, "gather_mode": "layered_unified_plus_5_facets"}, "evaluate", "DAG：layered critic gather"),
            },
        }
    )
    if ctx._skip_critic_wave.get(round_idx):
        merged = ctx._critic_merged_by_round[round_idx]
    else:
        uni = ctx._critic_unified_by_round.get(round_idx) or {"issues": [], "parse_ok": False, "recommended_action": "accept"}
        facets = ctx._critic_facets_by_round.get(round_idx, {})
        struct_like = facets_bundle_to_structured(facets)
        merged = merge_structured_with_parallel(uni, struct_like)
        merged["_facet_reports"] = facets
        if ctx.caches:
            ctx.caches.critic.put(_critic_cache_key(ctx, round_idx), merged)
    ctx._critic_merged_by_round[round_idx] = merged
    await _publish_critic_merge_metadata(ctx, round_idx, merged)


async def node_critic_paired_unified(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False) or ctx._skip_critic_wave.get(round_idx):
        return
    h, opt, hcfg = ctx.harness, ctx.options, ctx.hcfg
    uni = await evaluate_unified_critic(
        h,
        ctx.prompt,
        ctx.draft,
        ctx.analysis,
        opt,
        hcfg,
        search_context=ctx.ev_text,
        mode="general",
    )
    ctx._critic_unified_by_round[round_idx] = uni


async def node_critic_paired_structured(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False) or ctx._skip_critic_wave.get(round_idx):
        return
    struct = await evaluate_structured_quality_critic(ctx.harness, ctx.prompt, ctx.draft, ctx.options, ctx.hcfg, ctx.review_cands)
    ctx._critic_struct_by_round[round_idx] = struct


async def node_critic_merge_paired(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    plan = ctx.plan
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id
    emit_product_metric(hcfg, "critic_trigger_rate", trace_id=trace_id, round=round_idx, dag=True)
    await ctx.emit(user_status(f"并行批评（第 {round_idx + 1}/{plan.repair_rounds_max} 轮）…", phase="evaluate"))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_parallel_critic",
                "status": "running",
                "meta": _pg({"round": round_idx, "gather_mode": "paired_unified_plus_structured"}, "evaluate", "DAG：paired critic gather"),
            },
        }
    )
    if ctx._skip_critic_wave.get(round_idx):
        merged = ctx._critic_merged_by_round[round_idx]
    else:
        uni = ctx._critic_unified_by_round.get(round_idx) or {}
        struct = ctx._critic_struct_by_round.get(round_idx) or {}
        merged = merge_structured_with_parallel(uni, struct)
        if ctx.caches:
            ctx.caches.critic.put(_critic_cache_key(ctx, round_idx), merged)
    ctx._critic_merged_by_round[round_idx] = merged
    await _publish_critic_merge_metadata(ctx, round_idx, merged)


async def node_search_followup_dag(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    if ctx.st:
        ctx.st.set_phase("search", node=f"search_followup_{round_idx}", round=round_idx)
    merged = ctx._critic_merged_by_round.get(round_idx)
    if not merged:
        return
    await _run_search_followup_block(ctx, round_idx, merged)


async def node_repair_round_dag(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    if ctx.st:
        ctx.st.set_phase("repair", node=f"repair_round_{round_idx}", round=round_idx)
        ctx.st.note_repair_round(round_idx, node=f"repair_round_{round_idx}", status="running")
    merged = ctx._critic_merged_by_round.get(round_idx)
    if not merged:
        return
    _r_rep, repaired, _guard = await repair_node.execute_round(ctx, ctx.draft, merged, ctx.ev_text, round_idx)
    ctx.draft = repaired


async def node_verify_round_dag(ctx: DAGRuntimeContext, round_idx: int) -> None:
    if ctx.should_stop or getattr(ctx, "_quality_done", False):
        return
    if ctx.st:
        ctx.st.set_phase("verify", node=f"verify_round_{round_idx}", round=round_idx)
    uc = await verify_node.execute_round(ctx, ctx.draft, ctx.ev_text, round_idx)
    await _apply_verify_round_outcome(ctx, round_idx, ctx.draft, uc)


def make_prelude_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_quality_prelude(ctx, round_idx)

    _run.__name__ = f"quality_prelude_{round_idx}"
    return _run


def make_critic_unified_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_unified_layer(ctx, round_idx)

    _run.__name__ = f"critic_unified_{round_idx}"
    return _run


def make_critic_facet_runner(round_idx: int, facet_key: str):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_facet_layer(ctx, round_idx, facet_key)

    _run.__name__ = f"critic_facet_{round_idx}_{facet_key}"
    return _run


def make_critic_merge_layered_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_merge_layered(ctx, round_idx)

    _run.__name__ = f"critic_merge_{round_idx}"
    return _run


def make_critic_paired_unified_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_paired_unified(ctx, round_idx)

    _run.__name__ = f"critic_unified_{round_idx}"
    return _run


def make_critic_paired_structured_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_paired_structured(ctx, round_idx)

    _run.__name__ = f"critic_structured_{round_idx}"
    return _run


def make_critic_merge_paired_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_critic_merge_paired(ctx, round_idx)

    _run.__name__ = f"critic_merge_{round_idx}"
    return _run


def make_search_followup_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_search_followup_dag(ctx, round_idx)

    _run.__name__ = f"search_followup_{round_idx}"
    return _run


def make_repair_round_dag_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_repair_round_dag(ctx, round_idx)

    _run.__name__ = f"repair_round_{round_idx}"
    return _run


def make_verify_round_dag_runner(round_idx: int):
    async def _run(ctx: DAGRuntimeContext) -> None:
        await node_verify_round_dag(ctx, round_idx)

    _run.__name__ = f"verify_round_{round_idx}"
    return _run


async def node_finalize_output(ctx: DAGRuntimeContext) -> None:
    if ctx.should_stop:
        return
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    trace_id = ctx.trace_id
    hcfg = ctx.hcfg
    t0 = ctx.t0
    draft = ctx.draft
    intent_dict = ctx.intent_dict

    final_text = format_finalize_markdown(draft)
    if st:
        st.final_answer = final_text
        if st.verification_reports:
            last_v = st.verification_reports[-1].get("verify") or {}
            st.quality_score = float(last_v.get("quality_score") or st.quality_score or 0.0)
            st.confidence_score = max(st.confidence_score, float(last_v.get("completeness") or 0.0))
        acc = opt.get("_dag_cost_accum") or {}
        try:
            ti, to = int(acc.get("tokens_in") or 0), int(acc.get("tokens_out") or 0)
            st.runtime_cost_estimate = round((ti + to) / 1000.0, 4)
        except (TypeError, ValueError):
            pass

    elapsed = time.perf_counter() - t0
    emit_product_metric(
        hcfg,
        "latency_breakdown",
        trace_id=trace_id,
        dag_total_s=round(elapsed, 3),
        avg_parallelism_hint=ctx.max_wave_parallel,
    )
    emit_product_metric(hcfg, "avg_parallelism", trace_id=trace_id, width=int(ctx.max_wave_parallel))
    log_runtime_event(
        hcfg,
        {"event": "dag_runtime_complete", "trace_id": trace_id, "elapsed_s": elapsed, "intent": intent_dict},
    )

    await ctx.emit(user_status("输出终稿…", phase="finalize"))
    if st:
        st.set_phase("finalize", node="finalize_output")
    await ctx.emit(
        {"event": "stream_start", "runtime": "adaptive_dag_v3", "phase": "finalize", "trace_id": trace_id, "meta": {"stream_phase": "dag_finalize", "runtime": "adaptive_dag_v3", "phase": "finalize"}}
    )
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_finalize",
                "status": "running",
                "meta": _pg({"mode": "deterministic"}, "finalize", "确定性 Finalize（无模型）"),
            },
        }
    )
    async for ev in h._emit_text_chunks(final_text, opt, channel="final"):
        await ctx.emit(ev)
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_finalize",
                "status": "ok",
                "meta": _pg(
                    {
                        "chars": len(final_text),
                        "final_len": len(final_text),
                        "total_repair_rounds": (st.repair_round if st else 0),
                        "elapsed_s": round(elapsed, 2),
                    },
                    "finalize",
                    "Adaptive DAG Runtime 完成。",
                ),
            },
        }
    )

    remember_runtime_turn(opt, intent=intent_dict, ok=True)
    if st:
        st.runtime_memory.append({"phase": "finalize", "chars": len(final_text), "scheduler": "dag_wave_gather"})
