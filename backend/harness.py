from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, AsyncGenerator

from model_adapters import AskResult, build_adapter
from utils import Timer, new_trace_id


import json
import re
import asyncio
import time
import hashlib
from datetime import datetime, timedelta

from search_service import SearchService
from search_authority import apply_authority_ranking
from routing_signals import (
    derive_user_signals,
    merge_signals_into_analysis,
    reasoning_keyword_boost,
)
from search_query_util import soft_degrade_note, validate_search_query
from semantic_utils import (
    is_probably_english,
    ngram_overlap_ratio,
    normalize_text,
    semantic_similarity,
)

from tools.layer import HarnessTools
from search_evidence import search_result_to_evidence
from tools.parsing import next_review_search_action

from chunk_channels import attach_chunk_channel
from runtime_metrics import emit_product_metric, log_runtime_event
from runtime_state import (
    append_search_evidence_rows,
    execution_evidence_context,
    need_search_allowed,
    note_search_consumed,
    runtime_track,
)

from json_utils import extract_balanced_json_object, strip_markdown_json_fence
from stream_chunking import iter_chunk_spans
from refine_shared import Step, _pg, _int_budget, _clean_review_body

from orchestrator_state import (
    apply_capability_planner,
    bootstrap_execution_state,
)
from runtime_executor import collect_sync_response_from_stream


SSE_PROTOCOL_META: Dict[str, Any] = {"protocol_version": 1, "stream_schema": "harness-v1"}

REFINE_REVIEW_RETRY_SUFFIX = (
    "\n\n请结合上述联网信息更新审查结论；若仍需核实请在正文内输出合法 JSON 对象："
    "{\"action\":\"web_search\",\"query\":\"查询词\",\"reason\":\"说明\",\"priority\":\"high\"}。"
)


def _msg_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _format_messages_snippet(messages: Optional[List[Dict[str, Any]]], n: int = 4, max_chars: int = 4000) -> str:
    if not messages:
        return ""
    rows = messages[-n:]
    picked = []
    used = 0
    for msg in reversed(rows):
        line = f"{msg.get('role')}: {_msg_content_to_text(msg.get('content'))}"
        if picked and used + len(line) > max_chars:
            break
        picked.append(line)
        used += len(line)
    return "\n".join(reversed(picked))


def _analyze_step_summary(analysis: Dict[str, Any]) -> str:
    tt = str(analysis.get("task_type") or "通用")
    cx = str(analysis.get("complexity") or "—")
    ri = analysis.get("runtime_intent")
    if isinstance(ri, dict):
        lb = str(ri.get("latency_budget") or "—")
        qs = str(ri.get("search_score") or "—")
        return f"归类「{tt}」、复杂度「{cx}」；RuntimeIntent（latency={lb}，search_score≈{qs}，DAG 执行）。"
    return f"归类「{tt}」、复杂度「{cx}」（Runtime-centric，无互斥轨路由字段）。"


def _build_layer1_prompt(
    prompt: str,
    instruction: str,
    entry_block: str,
    messages: Optional[List[Dict[str, Any]]],
    *,
    max_history_chars: int,
) -> str:
    l1_prompt = f"{instruction.strip()}\n{entry_block}\n\n【原始问题】\n{prompt.strip()}\n"
    if entry_block.strip():
        l1_prompt = (
            "以下搜索摘要为本次信息来源，请优先使用其内容，引用时尽量标注来源序号；"
            "不要超出摘要内容臆测。\n\n" + l1_prompt
        )
    ht = _format_messages_snippet(messages, 4, max_chars=max_history_chars)
    if ht:
        l1_prompt = f"【近期对话上下文参考】\n{ht}\n\n" + l1_prompt
    return l1_prompt


def _build_layer2_prompt(
    prompt: str,
    instruction: str,
    draft_answer: str,
    messages: Optional[List[Dict[str, Any]]],
    *,
    max_history_chars: int,
) -> str:
    l2_prompt = (
        f"{instruction.strip()}\n\n"
        f"【原始问题】\n{prompt.strip()}\n\n"
        f"【初稿答案】\n{draft_answer.strip()}\n"
        "请先识别你自己对初稿仍不确定、需要补证据的地方，再输出修正版答案；"
        "修正版正文中不要保留“问题清单/修正后答案/仍不确定处”等元语言标题。\n"
        "（请勿在答案正文中间单独使用行首「仍不确定处：」作为小节标题，以免后处理误截断。）\n"
        "\n【强制格式】修正版答案正文必须用分隔符包裹：\n"
        "<<<FINAL_ANSWER>>>\n"
        "<修正版答案正文>\n"
        "<<<END_FINAL_ANSWER>>>\n"
        "\n补充证据或实时数据由 Adaptive DAG Runtime 统一调度并行检索；请勿输出 JSON action 或 <<ACTION:…>> 协议。\n"
    )
    ht = _format_messages_snippet(messages, 4, max_chars=max_history_chars)
    if ht:
        l2_prompt = f"【近期对话上下文参考】\n{ht}\n\n" + l2_prompt
    return l2_prompt


def _build_layer3_prompt(prompt: str, instruction: str, review_body: str, messages: Optional[List[Dict[str, Any]]] = None, *, max_history_chars: int = 2000) -> str:
    review_clean = _clean_review_body(review_body)
    base = (
        f"{instruction.strip()}\n\n"
        f"【原始问题】\n{prompt.strip()}\n\n"
        f"【审查层答案】\n{review_clean}\n"
    )
    # 添加历史上下文，提升润色质量
    ht = _format_messages_snippet(messages, 2, max_chars=max_history_chars)
    if ht:
        base = f"【近期对话上下文参考】\n{ht}\n\n" + base
    return base


class ModelRegistry:
    def __init__(self, models_cfg: Dict[str, Any]):
        self.models_cfg = models_cfg or {}
        self._adapters = {}

    def is_registered(self, model_key: str) -> bool:
        return bool(model_key and model_key in self.models_cfg)

    def get(self, model_key: str):
        if model_key not in self._adapters:
            cfg = self.models_cfg.get(model_key)
            if not cfg:
                raise ValueError(
                    f"模型未在 config.yaml 的 models 中配置: {model_key!r}。"
                    "请添加条目：provider 为 openai_compat，并配置 base_url、model、api_key_env（或 api_key_optional）。"
                )
            self._adapters[model_key] = build_adapter(model_key, cfg)
        return self._adapters[model_key]


class DualTrackHarness:
    def __init__(self, cfg: Dict[str, Any], redis_client: Any = None):
        self.cfg = cfg
        self.registry = ModelRegistry(cfg.get("models", {}))
        self.search = SearchService(cfg, redis_client=redis_client)
        self.tools = HarnessTools(self)
        self._redis = redis_client
        self._search_request_cache_prefix = "harness:reqsearch:"
        self._analysis_cache_prefix = "harness:analysis:v1:"

    def _make_tagger(self):
        """每次请求创建独立序号闭包，消除并发竞态。"""
        seq = [0]
        def _tag(phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            seq[0] += 1
            d: Dict[str, Any] = {"pipeline_phase": phase, "pipeline_sequence": seq[0]}
            if extra:
                d.update(extra)
            return d
        return _tag

    def _track_search_overrides(self, track: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        search_cfg = (self.cfg.get("harness") or {}).get("search") or {}
        by_track = search_cfg.get("by_track") or {}
        # 统一 DAG Runtime：优先 harness.search.by_track.dag，其次 refine，最后兼容传入 track
        base = by_track.get("dag") or by_track.get("refine") or by_track.get(track) or {}
        ov = {
            "override_max_results": base.get("max_results"),
            "override_search_depth": base.get("search_depth"),
        }
        # 强时效/强制检索：二次提升深度与条数（效果优先）
        si = str((analysis or {}).get("search_intent") or "none").lower()
        if si in ("freshness_required", "required"):
            try:
                ov["override_max_results"] = max(int(ov.get("override_max_results") or 0), 15)
            except Exception:
                ov["override_max_results"] = 15
            depth = str(ov.get("override_search_depth") or "basic").lower()
            if depth in ("basic", "fast", "ultra-fast"):
                ov["override_search_depth"] = "advanced"
        return ov

    def _normalized_search_key(self, query: str) -> str:
        return normalize_text(query).lower()

    def _messages_signature(self, messages: Optional[List[Dict[str, Any]]]) -> str:
        if not messages:
            return ""
        rows = []
        for msg in messages[-12:]:
            role = str(msg.get("role") or "")
            content = _msg_content_to_text(msg.get("content"))
            rows.append({"role": role, "content": normalize_text(content)[:1200]})
        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _documents_signature(self, documents: Any) -> str:
        if not isinstance(documents, list) or not documents:
            return ""
        rows: List[Dict[str, Any]] = []
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            row: Dict[str, Any] = {
                "name": str(doc.get("name") or ""),
                "ext": str(doc.get("ext") or ""),
                "status": str(doc.get("status") or ""),
                "client_file_id": str(doc.get("client_file_id") or ""),
            }
            chunks = doc.get("chunks")
            if isinstance(chunks, list) and chunks:
                ch_rows = []
                for chunk in chunks[:24]:
                    if not isinstance(chunk, dict):
                        continue
                    content = str(chunk.get("content") or "")
                    ch_rows.append(
                        {
                            "index": chunk.get("index"),
                            "hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                        }
                    )
                row["chunks"] = ch_rows
            else:
                content = str(doc.get("content") or "")
                row["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            rows.append(row)
        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def _prepend_runtime_disclaimer(self, prompt: str, options: Optional[Dict[str, Any]]) -> str:
        """联网不可用且本条曾依赖实时检索时，由 analysis 写入免责声明并注入各生成路径。"""
        d = str((options or {}).get("_runtime_answer_disclaimer") or "").strip()
        if not d:
            return prompt or ""
        p = prompt or ""
        head = d[:48]
        if p.startswith(head[: min(len(head), 12)]):
            return p
        return f"{d}\n{p}"

    def _attach_documents_to_prompt(self, prompt: str, options: Optional[Dict[str, Any]]) -> str:
        prompt = self._prepend_runtime_disclaimer(prompt, options)
        block = str((options or {}).get("_documents_context_block") or "").strip()
        if not block:
            return prompt
        # 文档上下文后置：先锚定用户问题，再提供证据，减少“只看文档忽略问题”的偏航
        return (
            f"{prompt}\n\n"
            "【参考文档】以下为你可引用的文档片段，请优先基于这些信息作答；涉及文档信息时尽量标注来自哪份文档或哪段内容。\n\n"
            f"{block}"
        )

    def _attach_documents_compact_to_prompt(self, prompt: str, options: Optional[Dict[str, Any]]) -> str:
        prompt = self._prepend_runtime_disclaimer(prompt, options)
        block = str((options or {}).get("_documents_context_block_compact") or "").strip()
        if not block:
            return prompt
        return (
            f"{prompt}\n\n"
            "【参考文档指针】以下为可能相关的片段来源与短摘录；仅在需要引用具体证据时使用，并尽量标注 文档名#片段。\n\n"
            f"{block}"
        )

    def _build_refine_layer1_prompt(
        self,
        prompt: str,
        instruction: str,
        entry_block: str,
        messages: Optional[List[Dict[str, Any]]],
        *,
        max_history_chars: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        question = self._attach_documents_to_prompt(prompt, options)
        return _build_layer1_prompt(
            question,
            instruction,
            entry_block,
            messages,
            max_history_chars=max_history_chars,
        )

    def _build_refine_layer2_prompt(
        self,
        question: str,
        instruction: str,
        draft_answer: str,
        messages: Optional[List[Dict[str, Any]]],
        *,
        max_history_chars: int,
        options: Optional[Dict[str, Any]] = None,
        extra_review_context: str = "",
    ) -> str:
        prompt = _build_layer2_prompt(
            # 审查层用“指针版”文档上下文，减少重复注入导致的 token 压力
            self._attach_documents_compact_to_prompt(question, options),
            instruction,
            draft_answer,
            messages,
            max_history_chars=max_history_chars,
        )
        scr = str(extra_review_context or "").strip()
        if scr:
            prompt += (
                "\n\n【自检与不确定性（供审查参考，勿直接当作用户可见正文）】\n"
                f"{scr}\n"
            )
        return prompt

    def _build_refine_layer3_prompt(
        self,
        question: str,
        instruction: str,
        review_body: str,
        *,
        options: Optional[Dict[str, Any]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        max_history_chars: int = 2000,
    ) -> str:
        return _build_layer3_prompt(
            # 润色层同样使用“指针版”，避免再次注入全文档块
            self._attach_documents_compact_to_prompt(question, options),
            instruction,
            _clean_review_body(review_body),
            messages=messages,
            max_history_chars=max_history_chars,
        )

    def _build_refine_layer2_fallback_text(self, draft_answer: str, entry_block: str = "") -> str:
        """审查层失败时，至少回退到 Layer1；若已有入口检索摘要则一并保留。"""
        draft = str(draft_answer or "").strip()
        entry = str(entry_block or "").strip()
        if not entry:
            return draft
        return f"{draft}\n\n{entry}".strip()

    async def _refine_layer2_ask_with_polish_rescue(
        self,
        l2_candidates: List[str],
        l2_prompt: str,
        layer_opts: Dict[str, Any],
        polish_candidates: List[str],
        default_model: str,
    ) -> Tuple[Any, List[Dict[str, Any]], bool, bool]:
        """
        审查层：review 模型池全部失败后，用 polish 模型池同任务补救。
        返回 (result, attempts, recovered_via_polish, attempted_polish_rescue)。
        """
        r2, attempts = await self._ask_with_fallback(l2_candidates, l2_prompt, layer_opts, messages=None)
        if r2.success:
            return r2, attempts, False, False
        polish_pool = [str(m).strip() for m in (polish_candidates or []) if str(m).strip()]
        if not polish_pool:
            polish_pool = [str(default_model).strip() or "gpt-5.5"]
        seen = set(l2_candidates)
        rescue = [m for m in polish_pool if m not in seen]
        if not rescue:
            rescue = polish_pool
        r3, att2 = await self._ask_with_fallback(rescue, l2_prompt, layer_opts, messages=None)
        merged = (attempts or []) + (att2 or [])
        if r3.success:
            return r3, merged, True, True
        return r2, merged, False, True

    def _resolve_agent_model(self, hcfg: Dict[str, Any], analysis: Dict[str, Any], routing_default: str) -> str:
        acfg = hcfg.get("agent") or {}
        base = str(acfg.get("model") or analysis.get("selected_model") or routing_default or "").strip()
        by_tt = acfg.get("model_by_task_type") or {}
        tt = str(analysis.get("task_type") or "").strip().lower()
        if isinstance(by_tt, dict) and tt:
            pick = str(by_tt.get(tt) or "").strip()
            if pick:
                return pick
        return base or routing_default

    def _agent_model_candidates(self, hcfg: Dict[str, Any], analysis: Dict[str, Any], routing_default: str) -> List[str]:
        primary = self._resolve_agent_model(hcfg, analysis, routing_default)
        routing = hcfg.get("routing") or {}
        defaults = routing.get("default_models") or [routing.get("default_model", routing_default)]
        if not isinstance(defaults, list):
            defaults = [primary]
        fb = analysis.get("fallback_models")
        extra: List[str] = list(fb) if isinstance(fb, list) else []
        out: List[str] = []
        for x in [primary] + extra + list(defaults):
            s = str(x or "").strip()
            if s and s not in out:
                out.append(s)
        # 严格过滤未注册模型，避免主循环因配置错误直接崩链
        out = [m for m in out if self.registry.is_registered(m)]
        return out or [routing_default or "gpt-5.5"]

    def _should_force_relevance_filter_sync(self, analysis: Dict[str, Any], options: Dict[str, Any]) -> bool:
        if bool(options.get("relevance_filter_sync", False)):
            return True
        if analysis.get("high_risk_domain"):
            return True
        if analysis.get("entity_confusion_risk"):
            return True
        if analysis.get("numeric_sensitive"):
            return True
        if analysis.get("source_sensitive"):
            return True
        if isinstance(options.get("documents"), list) and options.get("documents"):
            return True
        if str(analysis.get("type") or "").lower() == "document_qa":
            return True
        if str(analysis.get("search_intent") or "").lower() in ("required", "freshness_required"):
            return True
        return False

    def _session_search_cache_key(self, session_id: str, query: str) -> str:
        digest = hashlib.sha256(self._normalized_search_key(query).encode("utf-8")).hexdigest()
        return f"harness:searchkb:{session_id}:{digest}"

    def _build_search_queries(self, prompt: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> List[str]:
        raw_items = analysis.get("search_queries")
        queries: List[str] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                q = str(item or "").strip()
                if q:
                    queries.append(q)
        single = self.build_search_query(prompt, analysis, options)
        if single:
            queries.insert(0, single)
        out: List[str] = []
        seen = set()
        for query in queries:
            key = self._normalized_search_key(query)
            if key and key not in seen:
                seen.add(key)
                out.append(query)
        return out[:4]

    async def _resolve_runtime_context(self, prompt: str, mode: str, options: Dict[str, Any]) -> Dict[str, Any]:
        options = options or {}
        hcfg = self.cfg.get("harness") or {}
        blk, breason = self._web_search_blocked(options, hcfg)
        options["_web_search_blocked"] = blk
        options["_web_search_block_reason"] = breason

        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine", "agent", "dag"):
            mode = "auto"

        cx_cfg = hcfg.get("complexity") or {}
        analyzer_deadline = float(cx_cfg.get("analyzer_total_timeout_s", 45))
        sig_base = str(options.get("search_prompt_base") or prompt or "").strip()
        sr_cfg = hcfg.get("search") or {}
        raw_spec = sr_cfg.get("speculative_markers")
        if isinstance(raw_spec, list) and any(str(x).strip() for x in raw_spec):
            search_markers = tuple(str(m).strip().lower() for m in raw_spec if str(m).strip())
        else:
            search_markers = ("联网", "搜索", "查证", "最新", "今天", "实时", "weather", "news", "stock")
        speculative_search_task = None
        speculative_guess_key = ""
        # 投机预取增加成本评估：过滤命令、示例、过短输入（禁止联网时不启动）
        if (
            not blk
            and any(mark in sig_base.lower() for mark in [m.lower() for m in search_markers])
            and len(sig_base) > 10
            and not sig_base.startswith(('/help', '/config', '/clear', '/'))
            and not any(x in sig_base.lower() for x in ('示例', 'example', 'test', 'demo'))
        ):
            spec_analysis: Dict[str, Any] = {
                "search_query": "",
                "search_queries": [],
                "search_intent": "none",
            }
            guessed_query = (self.build_search_query(sig_base, spec_analysis, options) or "").strip()
            if not guessed_query:
                guessed_query = normalize_text(sig_base)
            if guessed_query:
                speculative_guess_key = self._normalized_search_key(guessed_query)
                # 注意：此处 analysis 尚未产出，只能使用 spec_analysis 作为 intent 参考
                sub_opts = {**options, **{k: v for k, v in self._track_search_overrides("fast", spec_analysis).items() if v is not None}}
                speculative_search_task = asyncio.create_task(self.perform_web_search(guessed_query, sub_opts))
        try:
            analysis = await asyncio.wait_for(
                self.analyze_complexity(prompt, options),
                timeout=analyzer_deadline,
            )
        except asyncio.TimeoutError:
            analysis = self._analyzer_fallback_timeout(prompt)

        analysis = self._postprocess_analysis(sig_base, analysis)
        signals = derive_user_signals(sig_base, options)
        analysis = merge_signals_into_analysis(analysis, signals, sig_base)
        if blk:
            analysis = self._coerce_analysis_when_web_blocked(analysis, options)

        apply_capability_planner(analysis)
        # 执行统一走 DAG：analyzer / planner 仅产出意图与能力信号，不再做 fast|refine|agent 互斥轨决策。
        analysis["route_rule"] = "dag_runtime_only"
        analysis["runtime_execution"] = "dag"
        intended_track = "dag"
        initial_track = "dag"

        entry_search_required, search_reason = self._runtime_need_entry_search(prompt, analysis, options)
        # 路由显式要求：即便检索信号未命中，也允许快轨入口注入一次联网摘要（禁止联网时不生效）
        if bool(analysis.get("force_entry_search")) and not blk:
            entry_search_required = True
            search_reason = search_reason or "force_entry_search"
        search_mandatory = self._search_mandatory(analysis, options)
        if blk:
            search_mandatory = False
        if self._should_force_relevance_filter_sync(analysis, options):
            options["relevance_filter_sync"] = True
        if speculative_search_task:
            if entry_search_required:
                try:
                    prefetched = await speculative_search_task
                    gq = (self.build_search_query(
                        sig_base,
                        {
                            "search_query": "",
                            "search_queries": [],
                            "search_intent": str(analysis.get("search_intent") or "none"),
                        },
                        options,
                    ) or "").strip() or normalize_text(sig_base)
                    cache = options.setdefault("_request_search_cache", {})
                    cache[self._normalized_search_key(gq)] = prefetched
                    if speculative_guess_key:
                        cache[speculative_guess_key] = prefetched
                except Exception:
                    pass
            else:
                speculative_search_task.cancel()
        options["_effective_search_intent"] = str(analysis.get("search_intent") or "none").lower()
        return {
            "hcfg": hcfg,
            "mode": mode,
            "analysis": analysis,
            "sig_base": sig_base,
            "intended_track": intended_track,
            "initial_track": initial_track,
            "entry_search_required": entry_search_required,
            "search_reason": search_reason,
            "search_mandatory": search_mandatory,
        }

    def _resolve_refine_context(self, analysis: Dict[str, Any], hcfg: Dict[str, Any]) -> Dict[str, Any]:
        chain = hcfg.get("refine_chain") or {}
        l1 = chain.get("layer1") or {}
        l2 = chain.get("layer2") or {}
        l3 = chain.get("layer3") or {}
        routing = hcfg.get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")
        refine_models = analysis.get("refine_models") or {}
        return {
            "chain": chain,
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "default_model": default_model,
            "refine_models": refine_models,
        }

    def _keyword_hit(self, text: str, keywords: List[str]) -> List[str]:
        t = (text or "").lower()
        hits = []
        for k in keywords or []:
            if k and k.lower() in t:
                hits.append(k)
        return hits

    def _feature_enabled(self, key: str, default: bool = True) -> bool:
        h = self.cfg.get("harness") or {}
        feat = h.get("features") or {}
        return bool(feat.get(key, default))

    def _normalize_analyzer_llm_dict(self, data: Dict[str, Any], raw_llm_response: str) -> Dict[str, Any]:
        complexity = str(data.get("complexity", "low") or "low").lower()
        if complexity not in ("low", "medium", "high"):
            complexity = "low"
        selected_model = data.get("selected_model", "")
        fallback_models = data.get("fallback_models", [])
        refine_models = data.get("refine_models", {})
        reason = data.get("reason", "")
        task_type = str(data.get("task_type") or "").strip().lower()
        if task_type not in ("conversation", "generation", "reasoning", "code"):
            task_type = ""
        if not task_type:
            task_type = self._infer_task_type_from_json(data)
        try:
            confidence = float(data.get("confidence", 0.85))
        except (TypeError, ValueError):
            confidence = 0.85
        confidence = max(0.0, min(1.0, confidence))
        raw_search_queries = data.get("search_queries")
        search_queries: List[str] = []
        if isinstance(raw_search_queries, list):
            search_queries = [str(item or "").strip() for item in raw_search_queries if str(item or "").strip()]
        elif str(data.get("search_query") or "").strip():
            search_queries = [str(data.get("search_query") or "").strip()]
        freshness_hint = str(data.get("freshness_hint") or "").strip()
        return {
            "reasons": [f"llm_reason: {reason}"],
            "complexity": complexity,
            "type": data.get("type", "general"),
            "task_type": task_type,
            "search_required": bool(data.get("search_required", False)),
            "search_query": str(data.get("search_query") or ""),
            "selected_model": selected_model,
            "fallback_models": fallback_models,
            "refine_models": refine_models,
            "reason": reason,
            "raw_llm_response": raw_llm_response,
            "confidence": confidence,
            "search_queries": search_queries,
            "freshness_hint": freshness_hint,
            "analyzer_schema": "runtime_centric_v1",
        }

    def _analyzer_heuristic_fallback_result(
        self,
        prompt: str,
        reasons: List[str],
        raw_response: Optional[str],
        *,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """预判 JSON 损坏 / LLM 失败时：改进启发式规则，提高置信度。"""
        text = str(prompt or "")
        low = text.lower()
        has_code = "```" in text or any(k in low for k in ("traceback", "stack trace", "debug", "报错", "异常"))
        length = len(text)
        question_count = text.count("?") + text.count("？")

        # 增加更多特征
        entities = len(re.findall(r'[\u4e00-\u9fff]{2,4}(?:公司|大学|医院|先生|女士|教授|博士)', text))
        question_density = question_count / max(1, len(text) / 100)

        deep_kw = any(
            k in text
            for k in ("写一篇", "详细分析", "完整方案", "深入分析", "系统阐述", "逐步分析", "证明", "推导", "定理")
        )

        # 更精细的分类
        short_chat = length < 96 and entities == 0 and question_density < 0.5 and not deep_kw and not has_code
        if short_chat:
            complexity = "low"
            task_type = "conversation"
            conf = 0.68  # 提高置信度
        elif has_code or "def " in low or "class " in low or entities >= 3:
            complexity = "high"
            task_type = "code" if has_code else "reasoning"
            conf = 0.62
        else:
            complexity = "medium"
            task_type = "reasoning" if question_count >= 1 or length > 180 else "generation"
            conf = 0.58
        out = {
            "reasons": list(reasons),
            "complexity": complexity,
            "type": "code" if has_code else "general",
            "task_type": task_type,
            "search_required": False,
            "search_query": "",
            "selected_model": "",
            "fallback_models": [],
            "refine_models": {},
            "reason": "；".join(reasons),
            "raw_llm_response": raw_response,
            "confidence": conf,
            "search_queries": [],
            "analyzer_schema": "runtime_centric_v1",
        }
        if extra_meta:
            out.update(extra_meta)
        return out

    def _parse_analyzer_json_candidates(self, raw_content: str) -> Optional[Dict[str, Any]]:
        candidates: List[str] = []
        stripped = strip_markdown_json_fence(raw_content)
        candidates.append(stripped)
        if stripped != raw_content.strip():
            candidates.append(raw_content.strip())
        bal = extract_balanced_json_object(raw_content)
        if bal:
            candidates.append(bal)
        seen: set[str] = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        return None

    async def _repair_analyzer_json_with_llm(self, broken: str, analyzer_model: str, llm_opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._feature_enabled("analyzer_json_repair", True):
            return None
        snippet = (broken or "")[:6500]
        prompt = (
            "下列文本本应是一段 JSON 意图判定（Runtime-centric，无 fast/refine/agent 互斥轨），但可能损坏。"
            "请只输出一个合法 JSON 对象，键需包含："
            "complexity, task_type, type, search_required, search_query, confidence, "
            "selected_model, fallback_models, refine_models, freshness_hint, reason, search_queries（可选数组）。"
            "不要 markdown 围栏，不要解释。\n\n"
            f"<<<BROKEN>>>\n{snippet}\n<<<END>>>"
        )
        ropts = {**llm_opts, "temperature": 0.0, "max_retries": 1}
        try:
            rt = float(ropts.get("request_timeout_s", 20))
        except (TypeError, ValueError):
            rt = 20.0
        ropts["request_timeout_s"] = min(14.0, max(5.0, rt))
        try:
            adapter = self.registry.get(analyzer_model)
            res = await adapter.ask(prompt, ropts)
            if not res.success or not (res.content or "").strip():
                return None
            return self._parse_analyzer_json_candidates(res.content)
        except Exception:
            return None

    async def analyze_complexity(self, prompt: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        hcfg = (self.cfg.get("harness") or {}).get("complexity") or {}
        manual = hcfg.get("manual_triggers") or []
        opts = dict(options or {})
        norm_prompt = self._norm_cache_prompt(prompt)

        manual_hits = self._keyword_hit(prompt, manual)
        if manual_hits:
            return {
                "reasons": ["manual:" + ",".join(manual_hits[:3])],
                "manual_hits": manual_hits,
                "complexity": "high",
                "type": "general",
                "task_type": "generation",
                "confidence": 1.0,
                "analyzer_schema": "runtime_centric_v1",
            }

        use_llm = bool(hcfg.get("use_llm_analyzer", False))
        if not use_llm:
            length = len(prompt or "")
            return {
                "reasons": [f"length={length}"],
                "complexity": "high" if length > 200 else "low",
                "type": "general",
                "task_type": "generation" if length > 200 else "conversation",
                "confidence": 0.55,
                "analyzer_schema": "runtime_centric_v1",
            }

        analyzer_model = hcfg.get("analyzer_model", "gpt-5.5")
        base_prompt = hcfg.get("analyzer_prompt", "")
        full_prompt = f"{base_prompt}\n\n{prompt}"

        cache_ttl = int(hcfg.get("analysis_cache_ttl_s", 300))
        cache_key = self._analysis_cache_key(norm_prompt, opts, analyzer_model)
        if self._redis:
            try:
                cached = await asyncio.to_thread(self._redis.get, cache_key)
                if cached:
                    data = json.loads(cached)
                    data["analysis_cache_hit"] = True
                    return data
            except Exception:
                pass

        llm_opts = dict(opts)
        llm_opts["temperature"] = 0.0
        llm_opts["max_retries"] = int(hcfg.get("analyzer_max_retries", 1))
        llm_opts["request_timeout_s"] = float(hcfg.get("analyzer_request_timeout_s", 20))

        try:
            adapter = self.registry.get(analyzer_model)
            res = await adapter.ask(full_prompt, llm_opts)
            if not res.success:
                return self._analyzer_heuristic_fallback_result(
                    prompt,
                    ["analyzer_llm_call_failed"],
                    res.content,
                    extra_meta={"analyzer_failure": True},
                )

            data = self._parse_analyzer_json_candidates(res.content or "")
            repair_attempted = False
            if data is None and self._feature_enabled("analyzer_json_repair", True):
                repair_attempted = True
                data = await self._repair_analyzer_json_with_llm(res.content or "", analyzer_model, llm_opts)

            if data is None:
                reasons = ["json_parse_error"]
                if repair_attempted:
                    reasons.append("analyzer_json_repair_failed")
                return self._analyzer_heuristic_fallback_result(
                    prompt,
                    reasons,
                    res.content,
                    extra_meta={"analyzer_json_recover_failed": True},
                )

            result = self._normalize_analyzer_llm_dict(data, res.content)
            if repair_attempted:
                result.setdefault("reasons", []).append("analyzer_json_repair_ok")

            if self._redis:
                try:
                    await asyncio.to_thread(
                        self._redis.set,
                        cache_key,
                        json.dumps(result, ensure_ascii=False),
                        cache_ttl,
                    )
                except Exception:
                    pass
            return result
        except Exception as e:
            return self._analyzer_heuristic_fallback_result(
                prompt,
                [f"llm_analyzer_error: {str(e)}"],
                None,
                extra_meta={"analyzer_exception": True},
            )

    def _analyzer_fallback_timeout(self, prompt: str) -> Dict[str, Any]:
        """LLM 预判整体超时：避免长时间卡住 SSE，按启发规则降级。"""
        text = str(prompt or "")
        length = len(text)
        low = text.lower()
        has_code = "```" in text or any(k in low for k in ("traceback", "stack trace", "debug", "报错", "异常"))
        question_count = text.count("?") + text.count("？")
        deep_kw = any(k in text for k in ("写一篇", "详细分析", "完整方案", "深入分析", "系统阐述", "逐步分析"))
        choose_refine = length > 200 or question_count >= 2 or deep_kw or has_code
        task_type = "reasoning" if has_code else ("generation" if choose_refine else "conversation")
        return {
            "reasons": ["analyzer_total_timeout"],
            "complexity": "high" if choose_refine else "low",
            "type": "code" if has_code else "general",
            "task_type": task_type,
            "reason": "复杂度预判超时，已按多特征启发规则降级并继续处理",
            "analyzer_timed_out": True,
            "confidence": 0.55 if choose_refine else 0.45,
            "analyzer_schema": "runtime_centric_v1",
        }

    def route_fast_model(self, prompt: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        routing = (self.cfg.get("harness") or {}).get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")
        default_models = routing.get("default_models") or [default_model]

        selected = analysis.get("selected_model")
        fallbacks = analysis.get("fallback_models") or []
        
        candidates = []
        if selected:
            candidates.append(selected)
        if isinstance(fallbacks, list):
            candidates.extend(fallbacks)

        # Deduplicate while preserving order
        seen = set()
        unique_candidates = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        def _reg_ok(key: str) -> bool:
            return bool(key and self.registry.is_registered(key))

        unique_candidates = [c for c in unique_candidates if _reg_ok(str(c))]
        default_models = [c for c in default_models if _reg_ok(str(c))]

        if not unique_candidates:
            unique_candidates = default_models
        if not unique_candidates:
            dm = str(default_model or "").strip()
            unique_candidates = [dm] if _reg_ok(dm) else list(self.registry.models_cfg.keys())[:3]

        return {
            "rule": "llm_autonomous_choice",
            "hits": [f"reason:{analysis.get('reason', 'none')}"],
            "candidates": unique_candidates,
            "selected": unique_candidates[0],
        }

    def _max_review_web_rounds(self) -> int:
        h = self.cfg.get("harness") or {}
        rt = h.get("refine_chain_tuning") or {}
        try:
            return max(1, min(8, int(rt.get("max_review_web_rounds", 3))))
        except (TypeError, ValueError):
            return 3

    def _registered_model_list(self, keys: Any) -> List[str]:
        out: List[str] = []
        if not isinstance(keys, list):
            return out
        reg = self.registry.models_cfg or {}
        for k in keys:
            s = str(k or "").strip()
            if s and s in reg:
                out.append(s)
        return out

    def _merge_task_model_templates(self, analysis: Dict[str, Any]) -> None:
        hcfg = self.cfg.get("harness") or {}
        tpl_root = hcfg.get("task_model_templates") or {}
        tt = str(analysis.get("task_type") or "conversation").lower()
        tpl = tpl_root.get(tt) or tpl_root.get("conversation") or {}
        if not tpl:
            return
        sm = str(analysis.get("selected_model") or "").strip()
        if sm and not self.registry.is_registered(sm):
            analysis["selected_model_invalid"] = sm
            analysis["selected_model"] = ""
        if not str(analysis.get("selected_model") or "").strip() and tpl.get("selected_model"):
            cand = str(tpl["selected_model"]).strip()
            if self.registry.is_registered(cand):
                analysis["selected_model"] = cand
        fb = analysis.get("fallback_models")
        if isinstance(fb, list) and fb:
            analysis["fallback_models"] = self._registered_model_list(fb)
        elif not analysis.get("fallback_models") and tpl.get("fallback_models"):
            analysis["fallback_models"] = self._registered_model_list(tpl["fallback_models"])
        rm = analysis.get("refine_models")
        if not isinstance(rm, dict):
            rm = {}
        rtpl = tpl.get("refine_models") or {}
        for k in ("draft", "review", "polish"):
            if rm.get(k):
                rm[k] = self._registered_model_list(rm[k])
            elif rtpl.get(k):
                rm[k] = self._registered_model_list(rtpl[k])
        if rm:
            analysis["refine_models"] = rm

    def _postprocess_analysis(self, prompt: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        out = {**analysis}
        self._merge_task_model_templates(out)
        hit, reason = reasoning_keyword_boost(prompt)
        if hit and str(out.get("complexity") or "low").lower() == "low":
            out["complexity"] = "high"
            out["task_type"] = "reasoning"
            out["reasoning_rule_boost"] = reason
        return out

    def _norm_cache_prompt(self, p: str) -> str:
        return re.sub(r"\s+", " ", (p or "").strip().lower())

    def _analysis_cache_key(self, norm_prompt: str, opts: Dict[str, Any], analyzer_model: str) -> str:
        """Analyzer 结果缓存键：prompt + 历史/文档摘要 + 模型与搜索模式 + system_prompt 版本（对齐文档第十二章）。"""
        h0 = self.cfg.get("harness") or {}
        cx_cfg = h0.get("complexity") or {}
        sys_ver = str(
            h0.get("system_prompt_version")
            or h0.get("prompt_version")
            or cx_cfg.get("analyzer_prompt_version")
            or "1"
        )
        o = opts or {}
        payload = {
            "analyzer_model": str(analyzer_model or "").strip(),
            "documents_digest": str(o.get("_documents_signature") or self._documents_signature(o.get("documents"))),
            "history_digest": str(o.get("_history_signature") or ""),
            "norm_prompt": norm_prompt,
            "search_mode": str(o.get("search_mode") or o.get("search") or "auto").lower(),
            "system_prompt_version": sys_ver,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return self._analysis_cache_prefix + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _layer_opts(self, hcfg: Dict[str, Any], layer_key: str, base: Dict[str, Any]) -> Dict[str, Any]:
        chain = hcfg.get("refine_chain") or {}
        if layer_key == "runtime_repair":
            lay = chain.get("repair") or chain.get("layer2") or {}
        else:
            lay = chain.get(layer_key) or {}
        t = float(lay.get("temperature", 0.2))
        return {**base, "temperature": t}

    def _web_search_blocked(self, options: Dict[str, Any], hcfg: Dict[str, Any]) -> Tuple[bool, str]:
        """用户在对话中选「关闭联网」或配置全局禁止时，禁止一切联网（高于预判/轨道内的联网意图）。"""
        opts = options or {}
        sm = str(opts.get("search_mode") or opts.get("search") or "auto").strip().lower()
        if sm in ("off", "false", "0", "disabled", "none"):
            return True, "用户在对话中选择「关闭联网」，本请求禁止一切联网检索（优先级高于预判与轨道逻辑）。"
        wcfg = hcfg.get("web_search") if isinstance(hcfg.get("web_search"), dict) else {}
        if bool(wcfg.get("globally_disabled")) or bool(hcfg.get("force_disable_web_search")):
            return True, "已在服务器配置中启用「全局禁止联网」（harness.web_search.globally_disabled）。"
        return False, ""

    def _coerce_analysis_when_web_blocked(self, analysis: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
        """清除会导致路由/入口检索的联网信号；保留 limitations 供 Runtime/Critic 感知。"""
        if not options.get("_web_search_blocked"):
            return analysis
        out = dict(analysis)
        orig_si = str(out.get("search_intent") or "none").lower()
        out["web_search_blocked"] = True
        out["search_required"] = False
        out["force_entry_search"] = False
        lims = list(out.get("limitations") or [])
        if "web_search_disabled" not in lims:
            lims.append("web_search_disabled")
        if orig_si in ("required", "freshness_required", "explicit") and "live_fact_verification_unavailable" not in lims:
            lims.append("live_fact_verification_unavailable")
        out["limitations"] = lims
        need_live = (
            orig_si in ("required", "freshness_required", "explicit")
            or bool(out.get("search_required"))
            or str(out.get("type") or "").lower() == "web_search"
        )
        if need_live:
            out["runtime_answer_disclaimer"] = "【系统说明】我无法联网验证最新信息，以下基于已有知识回答。\n\n"
        out["_original_search_intent_before_net_block"] = orig_si
        si = orig_si
        if si in ("explicit", "required", "freshness_required"):
            out["search_intent"] = "none"
            rs = list(out.get("reasons") or [])
            rs.append("search_intent_cleared_due_to_web_block")
            out["reasons"] = rs
        if str(out.get("type") or "").lower() == "web_search":
            out["type"] = "general"
        return out

    def _runtime_need_entry_search(self, prompt: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        opts = options or {}
        if opts.get("_web_search_blocked"):
            return False, str(opts.get("_web_search_block_reason") or "联网已禁用")
        mode = str(opts.get("search_mode") or opts.get("search") or "auto").lower()
        if mode in ("off", "false", "0", "disabled"):
            return False, "手动关闭联网搜索"
        if mode in ("on", "true", "1", "force"):
            return True, "用户手动开启联网搜索"
        si = str(analysis.get("search_intent") or "none").lower()
        if si in ("explicit", "required", "freshness_required"):
            return True, f"search_intent={si}"
        if analysis.get("search_required") or analysis.get("type") == "web_search":
            return True, analysis.get("reason") or "调度模型判断需要实时信息"
        user_face = str(opts.get("search_prompt_base") or prompt or "").strip()
        prompt_lower = user_face.lower()
        manual_markers = ["/search", "联网搜索", "上网查", "实时搜索", "最新", "今天", "最近"]
        hits = [m for m in manual_markers if m.lower() in prompt_lower]
        if hits:
            return True, "命中联网搜索提示：" + ",".join(hits[:3])
        return False, "无需联网搜索"

    def _query_is_related(self, prompt: str, query: str) -> bool:
        pq = (prompt or "").strip()
        qq = (query or "").strip()
        if len(qq) <= 32 and qq and (qq in pq or pq in qq):
            return True
        prompt_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (prompt or "").lower()))
        query_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (query or "").lower()))
        if not prompt_tokens or not query_tokens:
            return True
        return bool(prompt_tokens & query_tokens)

    def build_search_query(self, prompt: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> str:
        opts = options or {}
        user_q = str(opts.get("search_prompt_base") or prompt or "").strip()
        raw_query = str(analysis.get("search_query") or "").strip()

        if raw_query and self._query_is_related(user_q, raw_query):
            base = raw_query
        else:
            base = user_q

        base = re.sub(r"\s+", " ", base).strip()
        if not base:
            base = user_q

        hcfg = self.cfg.get("harness") or {}
        scfg = hcfg.get("search") or {}
        if not bool(scfg.get("query_enrich", True)):
            return base
        # 仅在确有联网/时效信号时做 enrich，避免噪声扩散影响召回
        si = str(analysis.get("search_intent") or opts.get("_effective_search_intent") or "none").lower()
        if si not in ("required", "freshness_required", "explicit"):
            return base

        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        combined = f"{user_q} {base}"
        cl = combined.lower()
        english = is_probably_english(combined)

        extras: List[str] = []

        def add_token(tok: str) -> None:
            t = tok.strip()
            if not t:
                return
            if t.lower() not in cl and t not in base:
                extras.append(t)

        if any(x in combined for x in ("明天", "翌日", "明早", "明晚", "次日")) or any(
            x in cl for x in ("tomorrow", "next day")
        ):
            add_token(str(tomorrow))
        elif any(x in combined for x in ("今天", "今日", "现在", "此刻", "最新", "实时")) or any(
            x in cl for x in ("today", "now", "current", "latest")
        ):
            add_token(str(today))

        if any(x in combined for x in ("东京", "新宿", "涩谷", "大阪", "京都", "日本")):
            if "tokyo" not in cl and "japan" not in cl:
                add_token("Tokyo Japan")

        if any(k in combined for k in ("天气", "气温", "降雨", "下雨", "台风", "暴雨")) or any(
            k in cl for k in ("weather", "forecast", "temperature", "rain")
        ):
            if "weather" not in cl:
                add_token("weather")

        if any(k in combined for k in ("股票", "股价", "汇率", "币价", "基金净值")) or any(
            k in cl for k in ("stock", "share price", "exchange rate", "forex", "crypto price")
        ):
            for tok in (("实时" if not english else "live"), ("今日" if not english else "today")):
                add_token(tok)

        if any(k in combined for k in ("财报", "季报", "年报", "业绩快报", "营收", "净利润", "每股收益")) or any(
            k in cl for k in ("earnings", "quarterly earnings", "annual report", "10-k", "10-q", "investor relations")
        ):
            add_token(str(datetime.now().year) if english else f"{datetime.now().year}年")
            if english:
                add_token("official filing")

        if re.search(r"\b(vue|react|python|node|typescript|java|spring|fastapi|django)\b", cl) and re.search(r"\b\d", cl):
            add_token(str(datetime.now().year) if english else f"{datetime.now().year}年")

        person_suffixes = ("先生", "女士", "老师", "教授", "博士")
        for suffix in person_suffixes:
            if base.endswith(suffix) and len(base) > len(suffix) + 1:
                base = base[: -len(suffix)].strip()
                break

        if extras:
            # 上限：避免过度扩展 query
            base = f"{base} " + " ".join(extras[:2])
        base = base.strip()
        # analyzer 可选提示：freshness_hint（如“2026-05/今天/本周/最新版本号”）→ 强化 query 的时间锚点
        fh = str(analysis.get("freshness_hint") or "").strip()
        if fh and fh.lower() not in base.lower():
            # 仅取前 2 个 token，避免污染
            toks = [t for t in re.split(r"[\s,，;；/\\]+", fh) if t][:2]
            if toks:
                base = (base + " " + " ".join(toks)).strip()
        if str(analysis.get("search_intent") or "").lower() == "freshness_required":
            y = datetime.now().year
            year_tok = str(y) if english else f"{y}年"
            fresh_tokens = ["latest update", "recent", year_tok] if english else [year_tok, "近期", "最新进展"]
            for tok in fresh_tokens:
                if tok not in base and tok.lower() not in base.lower():
                    base = f"{base} {tok}".strip()
                    break
        return base

    def _merge_search_into_prompt(self, prompt: str, search_context: str, options: Optional[Dict[str, Any]]) -> str:
        """合并检索摘要：锚定用户表述，避免检索片段与用户所指实体不一致时被当成事实。"""
        opts = options or {}
        user_anchor = str(opts.get("search_prompt_base") or "").strip()
        anchor_block = f"【用户原话】\n{user_anchor}\n\n" if user_anchor else ""
        # 短答场景减少“强制引用”要求；仅在数值/来源敏感时仍强制
        rs = str(opts.get("response_style") or "normal").strip().lower()
        force_cite = True
        if rs == "short" and not bool(opts.get("numeric_sensitive")) and not bool(opts.get("source_sensitive")):
            force_cite = False
        cite_line = (
            "请在答复中对采用的检索内容标注来源序号（如 [1]），并在文末列出引用链接。\n\n"
            if force_cite
            else "如使用了检索内容，可选地标注来源序号（如 [1]），并在文末附上链接。\n\n"
        )
        return (
            f"{search_context}\n"
            "【要求】只把与用户所指实体/时间范围一致的片段当作证据；不一致则说明不确定。\n\n"
            f"{anchor_block}"
            f"{cite_line}"
            f"【用户问题】\n{prompt}"
        )

    async def _ask_with_fallback(self, model_keys: List[str], prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> Tuple[AskResult, List[Dict[str, Any]]]:
        attempts = []
        last: Optional[AskResult] = None
        for mk in model_keys:
            try:
                adapter = self.registry.get(mk)
            except ValueError as e:
                err = AskResult(
                    success=False,
                    content="",
                    provider="config",
                    model=mk,
                    latency_ms=0,
                    error=str(e),
                )
                attempts.append({"model_key": mk, **err.to_dict()})
                last = err
                continue
            res = await adapter.ask(prompt, options, messages=messages)
            attempts.append({"model_key": mk, **res.to_dict()})
            last = res
            if res.success:
                return res, attempts
        # if everything failed, return last failure
        return last or AskResult(False, "", "unknown", "unknown", 0, error="No models configured"), attempts

    async def _stream_with_fallback(
        self,
        candidates: List[str],
        prompt: str,
        options: Dict[str, Any],
        messages: Optional[List[Dict[str, Any]]] = None,
        *,
        chunk_channel: str = "final",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        last_error = ""
        last_error_code = ""
        filtered = [c for c in (candidates or []) if c]
        attempt_total = max(1, len(filtered))
        for attempt_idx, mk in enumerate(filtered):
            try:
                adapter = self.registry.get(mk)
                yield {
                    "event": "model_start",
                    "model": mk,
                    "provider": adapter.provider,
                    "attempt_index": attempt_idx,
                    "attempt_total": attempt_total,
                    "meta": dict(SSE_PROTOCOL_META),
                    "internal": True,
                }
                started_at = time.perf_counter()
                emitted_chars = 0

                async for chunk in adapter.stream(prompt, options, messages=messages):
                    content = chunk.get("content") or ""
                    reasoning = chunk.get("reasoning_content") or ""
                    # 许多网关/模型在长推理阶段只流式 reasoning_content，delta.content 长时间为空。
                    # 若此处不向 SSE 推送 chunk，前端会一直处于「生成中」但正文空白。
                    stream_text = content if content else reasoning
                    if stream_text:
                        emitted_chars += len(stream_text)
                        step = self._stream_slice_chars(options)
                        stune = (self.cfg.get("harness") or {}).get("stream_tuning") or {}
                        smart = bool(stune.get("smart_chunk_boundary", True))
                        for start, end in iter_chunk_spans(stream_text, step, smart=smart):
                            yield attach_chunk_channel(
                                {"event": "chunk", "data": {"content": stream_text[start:end]}},
                                chunk_channel,
                                options,
                            )
                            await asyncio.sleep(0)

                if emitted_chars <= 0:
                    last_error = f"{mk} stream ended without content"
                    yield {
                        "event": "model_error",
                        "model": mk,
                        "error": last_error,
                        "error_code": "EMPTY_STREAM",
                        "internal": True,
                    }
                    continue

                yield {
                    "event": "model_end",
                    "model": mk,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "chars": emitted_chars,
                    "internal": True,
                }
                return
            except Exception as e:
                last_error = str(e)
                low = last_error.lower()
                if "429" in last_error or "too many requests" in low or "rate limit" in low:
                    last_error_code = "RATE_LIMIT"
                elif "timeout" in low or "timed out" in low:
                    last_error_code = "TIMEOUT"
                elif "502" in last_error or "503" in last_error or "504" in last_error or "bad gateway" in low:
                    last_error_code = "UPSTREAM_5XX"
                elif "401" in last_error or "403" in last_error:
                    last_error_code = "AUTH"
                else:
                    last_error_code = "MODEL_STREAM_ERROR"
                yield {
                    "event": "model_error",
                    "model": mk,
                    "error": last_error,
                    "error_code": last_error_code,
                    "internal": True,
                }
                if attempt_idx + 1 < len(filtered):
                    log_runtime_event(
                        self.cfg.get("harness") or {},
                        {
                            "event": "stream_model_fallback",
                            "trace_id": str((options or {}).get("trace_id") or ""),
                            "from_model": mk,
                            "to_model": filtered[attempt_idx + 1],
                            "reason": last_error_code or "error",
                            "attempt_index": attempt_idx,
                            "attempt_total": attempt_total,
                        },
                    )
                continue

        yield {
            "event": "error",
            "error": f"All fallback models failed in stream. Last error: {last_error or 'unknown'}",
            "error_code": "STREAM_FALLBACK_EXHAUSTED",
            "last_error_code": last_error_code or None,
        }

    def _session_search_redis_ttl(self, options: Dict[str, Any]) -> int:
        h = self.cfg.get("harness") or {}
        scfg = h.get("search") or {}
        base = int(scfg.get("session_cache_ttl_s", 1800))
        fresh_ttl = int(scfg.get("session_cache_ttl_freshness_s", 600))
        req_ttl = int(scfg.get("session_cache_ttl_required_s", base))
        expl_ttl = int(scfg.get("session_cache_ttl_explicit_s", base))
        si = str(options.get("_effective_search_intent") or options.get("search_intent") or "none").lower()
        if si == "freshness_required":
            return max(60, fresh_ttl)
        if si == "required":
            return max(60, req_ttl)
        if si == "explicit":
            return max(60, expl_ttl)
        return max(60, base)

    def _search_mandatory(self, analysis: Dict[str, Any], options: Dict[str, Any]) -> bool:
        if options.get("_web_search_blocked"):
            return False
        si = str(analysis.get("search_intent") or "none").lower()
        if si in ("required", "freshness_required"):
            return True
        if bool(analysis.get("search_required")):
            return True
        sm = str(options.get("search_mode") or "").lower()
        if sm in ("on", "true", "1", "force", "always"):
            return True
        return False

    def _effective_relevance_filter_sync(self, options: Dict[str, Any]) -> bool:
        """是否对本请求同步跑检索相关性过滤（可显式覆盖；否则由 sync_default_mode + 当前轨决定）。"""
        v = options.get("relevance_filter_sync")
        if v is True:
            return True
        if v is False:
            return False
        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        mode = str(rcfg.get("sync_default_mode", "quality_tracks")).strip().lower()
        if mode in ("always", "all", "true", "1"):
            return True
        if mode in ("never", "none", "false", "0"):
            return False
        track = str(options.get("_runtime_track") or "").strip().lower()
        raw = rcfg.get("sync_tracks") or ["refine", "agent"]
        allowed = {str(x).strip().lower() for x in raw if str(x).strip()}
        return track in allowed

    def _relevance_needs_reapply(self, sr: Dict[str, Any]) -> bool:
        """会话/内存缓存中仍为 deferred 或未成功过滤时，需要补跑。"""
        if sr.get("error") or not (sr.get("sources") or []):
            return False
        m = sr.get("relevance_filter_meta")
        if not m:
            return True
        if m.get("deferred"):
            return True
        if m.get("error"):
            return True
        try:
            checked = int(m.get("checked") or 0)
        except (TypeError, ValueError):
            checked = 0
        if checked > 0 or m.get("batches") is not None:
            return False
        return True

    async def _apply_relevance_filter_inplace(self, sr: Dict[str, Any], options: Dict[str, Any]) -> None:
        from search_relevance import filter_sources_by_relevance, rebuild_context_from_sources

        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        uq = str(options.get("search_prompt_base") or "")[:6000]
        mk = str(rcfg.get("model") or "gpt-5.5")
        context_chars = _int_budget(options, "search_context_chars", 6000, minimum=1500, maximum=12000)
        kept, fmeta = await filter_sources_by_relevance(
            self,
            uq,
            list(sr.get("sources") or []),
            model_key=mk,
            options=options,
        )
        if kept and len(kept) != len(sr.get("sources") or []):
            sr["sources"] = kept
            sr["context"] = rebuild_context_from_sources(
                kept, datetime.now().isoformat(timespec="seconds"), max_total_chars=context_chars
            )
        sr["relevance_filter_meta"] = fmeta

    async def _ensure_relevance_filtered(
        self,
        sr: Dict[str, Any],
        options: Dict[str, Any],
        *,
        vq: str,
        cache_key: str,
        request_cache: Dict[str, Any],
    ) -> Dict[str, Any]:
        """对 deferred/未过滤的检索结果补跑相关性过滤，并写回请求内缓存与会话 Redis。"""
        out = dict(sr)
        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        if not rcfg.get("enabled", False):
            return out
        if not self._effective_relevance_filter_sync(options):
            return out
        if not self._relevance_needs_reapply(out):
            return out
        try:
            await self._apply_relevance_filter_inplace(out, options)
        except Exception as e:
            out["relevance_filter_meta"] = {**(out.get("relevance_filter_meta") or {}), "reapply_error": str(e)}
            return out
        request_cache[cache_key] = dict(out)
        session_id = str(options.get("session_id") or "").strip()
        if self._redis and session_id:
            try:
                await asyncio.to_thread(
                    self._redis.set,
                    self._session_search_cache_key(session_id, vq),
                    json.dumps(out, ensure_ascii=False),
                    self._session_search_redis_ttl(options),
                )
            except Exception:
                pass
        return out

    async def perform_web_search(self, query: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        options = options or {}
        if options.get("_web_search_blocked"):
            msg = str(options.get("_web_search_block_reason") or "联网已禁用")
            return {
                "context": "",
                "sources": [],
                "error": msg,
                "failure_code": "WEB_SEARCH_BLOCKED",
                "degraded": False,
                "provider_used": "none",
                "latency_ms": 0,
                "attempts": [],
            }
        vq, fc, reason = validate_search_query((query or "").strip())
        if fc:
            return {
                "context": "",
                "sources": [],
                "error": reason or fc,
                "failure_code": fc,
                "degraded": False,
                "provider_used": "none",
                "latency_ms": 0,
                "attempts": [],
            }
        cache = options.setdefault("_request_search_cache", {})
        cache_key = self._normalized_search_key(vq)
        session_id = str(options.get("session_id") or "").strip()

        if cache_key in cache:
            out = await self._ensure_relevance_filtered(
                dict(cache[cache_key]), options, vq=vq, cache_key=cache_key, request_cache=cache
            )
            self._capture_search_evidence_for_runtime(options, out)
            return out

        if self._redis and session_id:
            try:
                cached = await asyncio.to_thread(self._redis.get, self._session_search_cache_key(session_id, vq))
                if cached:
                    data = json.loads(cached)
                    data["cached"] = True
                    cache[cache_key] = data
                    out = await self._ensure_relevance_filtered(
                        data, options, vq=vq, cache_key=cache_key, request_cache=cache
                    )
                    self._capture_search_evidence_for_runtime(options, out)
                    return out
            except Exception:
                pass

        if not need_search_allowed(options):
            return {
                "context": "",
                "sources": [],
                "error": "搜索预算已用尽",
                "failure_code": "SEARCH_BUDGET_EXHAUSTED",
                "degraded": False,
                "provider_used": "none",
                "latency_ms": 0,
                "attempts": [],
            }
        bud = options.get("_search_budget_remaining")
        if isinstance(bud, int):
            options["_search_budget_remaining"] = bud - 1

        sr = await self.search.search(
            vq,
            override_max_results=options.get("override_max_results"),
            override_search_depth=options.get("override_search_depth"),
        )
        if (sr.get("sources") or []) and not (sr.get("authority_ranking_meta") or {}).get("reordered"):
            hcfg = self.cfg.get("harness") or {}
            sr = apply_authority_ranking(sr, hcfg.get("search") or {})
        if sr.get("error") or not (sr.get("sources") or []):
            cache[cache_key] = dict(sr)
            return sr
        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        if not rcfg.get("enabled", False):
            cache[cache_key] = dict(sr)
            self._capture_search_evidence_for_runtime(options, sr)
            return sr

        if not self._effective_relevance_filter_sync(options):
            sr["relevance_filter_meta"] = {"deferred": True}
            cache[cache_key] = dict(sr)
            if self._redis and session_id:
                try:
                    await asyncio.to_thread(
                        self._redis.set,
                        self._session_search_cache_key(session_id, vq),
                        json.dumps(sr, ensure_ascii=False),
                        self._session_search_redis_ttl(options),
                    )
                except Exception:
                    pass
            self._capture_search_evidence_for_runtime(options, sr)
            return sr

        try:
            await self._apply_relevance_filter_inplace(sr, options)
        except Exception as e:
            sr["relevance_filter_meta"] = {"error": str(e)}
        cache[cache_key] = dict(sr)
        if self._redis and session_id:
            try:
                await asyncio.to_thread(
                    self._redis.set,
                    self._session_search_cache_key(session_id, vq),
                    json.dumps(sr, ensure_ascii=False),
                    self._session_search_redis_ttl(options),
                )
            except Exception:
                pass
        self._capture_search_evidence_for_runtime(options, sr)
        return sr

    async def _iter_refine_review_web_rounds(
        self,
        review_body: str,
        l2_prompt: str,
        l2_candidates: List[str],
        layer_opts: Dict[str, Any],
        options: Dict[str, Any],
        review_search_chars: int,
        track: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """共享：审查层内 JSON web_search 多轮核查的状态机（供 run / run_stream / refine_pipeline 复用）。"""
        extra_ctx = ""
        search_loops = 0
        rb = (review_body or "").strip()
        if options.get("_web_search_blocked"):
            yield {"kind": "complete", "review_body": rb, "search_loops": 0}
            return
        # 智能停止：连续多轮“无新增信息”则提前终止，减少无效联网与重复审查
        stagnant_rounds = 0
        last_sources_sig = ""
        last_snip_head = ""
        overrides = {k: v for k, v in self._track_search_overrides(track).items() if v is not None}
        for _ in range(self._max_review_web_rounds()):
            q, _src = next_review_search_action(rb)
            if not q:
                break
            search_loops += 1
            yield {"kind": "round_start", "loop": search_loops, "query": q}
            vq, vfc, vreason = validate_search_query(q)
            if vfc:
                sr = {
                    "context": "",
                    "sources": [],
                    "error": vreason or vfc,
                    "failure_code": vfc,
                    "provider_used": "none",
                    "latency_ms": 0,
                }
            else:
                sr = await self.perform_web_search(
                    vq, {**options, **overrides}
                )
            snip = (sr.get("context") or "")[:review_search_chars]
            rc = len(sr.get("sources") or [])
            yield {"kind": "after_search", "loop": search_loops, "query": q, "sr": sr, "snip": snip, "result_count": rc}

            # 计算“新增信息”签名：urls + snippet 前缀（轻量）
            try:
                urls = [str(s.get("url") or "") for s in (sr.get("sources") or []) if isinstance(s, dict)]
                urls = [u for u in urls if u.strip()]
                urls_key = "|".join(urls[:10])
            except Exception:
                urls_key = ""
            snip_head = normalize_text(snip)[:800]
            sources_sig = hashlib.sha256((urls_key + "\n" + snip_head).encode("utf-8")).hexdigest()[:12]
            if sources_sig and sources_sig == last_sources_sig:
                stagnant_rounds += 1
            elif last_snip_head and snip_head and ngram_overlap_ratio(last_snip_head, snip_head) >= 0.92:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            last_sources_sig = sources_sig or last_sources_sig
            last_snip_head = snip_head or last_snip_head
            if stagnant_rounds >= 2:
                yield {"kind": "early_stop", "loop": search_loops, "reason": "no_new_information"}
                break

            if sr.get("error"):
                extra_ctx += f"\n\n【联网核查失败】{sr.get('error')}"
            else:
                extra_ctx += f"\n\n【联网核查补充】\n{snip}"
            retry_prompt = l2_prompt + extra_ctx + REFINE_REVIEW_RETRY_SUFFIX
            r2b, _ = await self._ask_with_fallback(l2_candidates, retry_prompt, layer_opts, messages=None)
            if r2b.success:
                rb = _clean_review_body((r2b.content or "").strip())
            else:
                break
        yield {"kind": "complete", "review_body": rb, "search_loops": search_loops}

    def _infer_task_type_from_json(self, data: Dict[str, Any]) -> str:
        """当模型未返回 task_type 时，由 complexity/type 推断。"""
        raw_type = str(data.get("type") or "").lower()
        if raw_type == "code":
            return "code"
        if raw_type in ("math_logic",):
            return "reasoning"
        if raw_type in ("writing", "web_search", "document_qa"):
            return "generation"
        cx = str(data.get("complexity") or "low").lower()
        if cx == "low":
            return "conversation"
        return "generation"

    def _should_skip_refine_draft(self, prompt: str, analysis: Dict[str, Any], options: Dict[str, Any]) -> bool:
        if bool(options.get("skip_draft")):
            return True
        if str(analysis.get("search_intent") or "none").lower() in ("required", "freshness_required"):
            return False
        text = str(options.get("search_prompt_base") or prompt or "").strip()
        markers = ("帮我改进", "润色这段", "优化这段", "修改这段文字", "改写下面", "improve this", "polish this", "rewrite this")
        return any(marker.lower() in text.lower() for marker in markers)

    def _stream_slice_chars(self, options: Dict[str, Any]) -> int:
        hcfg = self.cfg.get("harness") or {}
        default_slice = int(hcfg.get("stream_slice_chars", 72))
        v = int(options.get("stream_slice_chars") or default_slice)
        # 中文更细粒度；英文/代码可更大粒度减少事件数
        probe = str(options.get("search_prompt_base") or options.get("_fast_cache_identity") or "")
        if probe:
            zh = len(re.findall(r"[\u4e00-\u9fff]", probe))
            total = max(1, len(probe))
            ratio = zh / float(total)
            if ratio >= 0.18:
                v = min(v, 64)
            elif ratio <= 0.03 and total >= 120:
                v = max(v, 96)
        return max(1, v)

    async def _emit_text_chunks(
        self, text: str, options: Dict[str, Any], *, channel: str = "final"
    ) -> AsyncGenerator[Dict[str, Any], None]:
        step = self._stream_slice_chars(options)
        stune = (self.cfg.get("harness") or {}).get("stream_tuning") or {}
        smart = bool(stune.get("smart_chunk_boundary", True))
        for start, end in iter_chunk_spans(text, step, smart=smart):
            yield attach_chunk_channel({"event": "chunk", "data": {"content": text[start:end]}}, channel, options)
            await asyncio.sleep(0)

    def _runtime_max_escalations(self, hcfg: Dict[str, Any]) -> int:
        orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
        try:
            return max(1, min(8, int(orch.get("max_escalations", 2))))
        except (TypeError, ValueError):
            return 2

    def _init_runtime_search_budget(self, options: Dict[str, Any], hcfg: Dict[str, Any]) -> None:
        orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
        raw = orch.get("search_budget_per_request")
        if raw is None:
            options["_search_budget_remaining"] = None
            return
        try:
            options["_search_budget_remaining"] = max(0, int(raw))
        except (TypeError, ValueError):
            options["_search_budget_remaining"] = None

    async def _prepare_runtime_execution(
        self,
        prompt: str,
        mode: str,
        options: Dict[str, Any],
        messages: Optional[List[Dict[str, Any]]],
        trace_id: str,
    ) -> Dict[str, Any]:
        """Analyzer → Capability Planner → ExecutionState：sync/stream 共用入口（同一 runtime，无单独降级）。"""
        runtime = await self._resolve_runtime_context(prompt, mode, options)
        hcfg = runtime["hcfg"]
        mode = runtime["mode"]
        analysis = runtime["analysis"]
        # 执行轨统一为 DAG：analyzer / planner 仅产出意图信号，不再决定互斥 fast|refine|agent。
        intended_track = "dag"
        initial_track = "dag"
        entry_search_required = runtime["entry_search_required"]
        search_reason = runtime["search_reason"]
        search_mandatory = runtime["search_mandatory"]

        options["_runtime_track"] = initial_track
        h0 = self.cfg.get("harness") or {}
        options["_fast_cache_model_version"] = str(analysis.get("selected_model") or "")
        options["_fast_cache_prompt_version"] = str(h0.get("system_prompt_version") or h0.get("prompt_version") or "1")
        self._init_runtime_search_budget(options, hcfg)
        options.pop("output_intent", None)
        disc = str(analysis.get("runtime_answer_disclaimer") or "").strip()
        if disc:
            options["_runtime_answer_disclaimer"] = disc
        else:
            options.pop("_runtime_answer_disclaimer", None)
        bootstrap_execution_state(
            trace_id,
            prompt,
            initial_track,
            analysis,
            options,
            max_escalations=self._runtime_max_escalations(hcfg),
            messages=messages,
        )
        options.setdefault("response_style", str(analysis.get("response_style") or "normal"))
        options.setdefault("_chunk_seq", -1)
        return {
            "hcfg": hcfg,
            "mode": mode,
            "analysis": analysis,
            "intended_track": intended_track,
            "initial_track": initial_track,
            "entry_search_required": entry_search_required,
            "search_reason": search_reason,
            "search_mandatory": search_mandatory,
            "runtime_contract": "dag_intent_primary",
        }

    def _ingest_web_search_into_execution_state(self, options: Dict[str, Any], sr: Dict[str, Any]) -> None:
        rows = [e.to_dict() for e in search_result_to_evidence(sr)]
        append_search_evidence_rows(options, rows)

    def _capture_search_evidence_for_runtime(self, options: Dict[str, Any], sr: Dict[str, Any]) -> None:
        if not sr or sr.get("error"):
            return
        if not ((sr.get("sources") or []) or str(sr.get("context") or "").strip()):
            return
        note_search_consumed(options)
        self._ingest_web_search_into_execution_state(options, sr)

    async def _run_agent_stream(
        self,
        prompt: str,
        analysis: Dict[str, Any],
        options: Dict[str, Any],
        messages: Optional[List[Dict[str, Any]]],
        trace_id: str,
        hcfg: Dict[str, Any],
        _tag,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """旧 ReAct / ACTION 循环已移除；遗留调用仅返回错误事件（主路径为 DAG Runtime）。"""
        yield {
            "event": "step",
            "step": {
                "name": "agent_runtime_removed",
                "status": "skipped",
                "meta": _pg(
                    {"reason": "dag_runtime_only"},
                    "reasoning",
                    "互斥 Agent 轨已删除；工具与推理由 DAG 能力与并行节点编排。",
                ),
            },
        }
        yield {"event": "error", "error": "agent_track_removed"}

    async def run_stream(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """流式 SSE 主路径；同步 ``run`` 经 ``runtime_executor.collect_sync_response_from_stream`` 消费同一序列。"""
        options = options or {}
        options.setdefault("_history_signature", self._messages_signature(messages))
        options.setdefault("_documents_signature", self._documents_signature(options.get("documents")))
        if "_fast_cache_identity" not in options:
            options["_fast_cache_identity"] = str(options.get("search_prompt_base") or prompt or "").strip()
        trace_id = options.get("trace_id") or new_trace_id()
        _tag = self._make_tagger()

        yield {"event": "trace", "trace_id": trace_id, "meta": dict(SSE_PROTOCOL_META)}
        yield {"event": "protocol_meta", "meta": dict(SSE_PROTOCOL_META)}
        yield {"event": "status", "phase": "analyze", "message": "正在分析问题…"}

        yield {
            "event": "step",
            "step": {
                "name": "complexity_analyze",
                "status": "running",
                "meta": _pg({}, "intake", "正在调用预判模型分析意图与复杂度…"),
            },
        }
        prep = await self._prepare_runtime_execution(prompt, mode, options, messages, trace_id)
        hcfg = prep["hcfg"]
        mode = prep["mode"]
        analysis = prep["analysis"]

        from runtime.dag_stream import run_dag_runtime_stream

        async for ev in run_dag_runtime_stream(
            self, prompt, mode, options, messages, trace_id, hcfg, prep, _tag
        ):
            yield ev
        return

    async def run(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """与 SSE 共用 ``run_stream`` 运行时；同步响应仅对流事件聚合成最终 JSON。"""
        options = options or {}
        options.setdefault("_history_signature", self._messages_signature(messages))
        options.setdefault("_documents_signature", self._documents_signature(options.get("documents")))
        if "_fast_cache_identity" not in options:
            options["_fast_cache_identity"] = str(options.get("search_prompt_base") or prompt or "").strip()
        if not options.get("trace_id"):
            options["trace_id"] = new_trace_id()
        return await collect_sync_response_from_stream(self, prompt, mode, options, messages)
