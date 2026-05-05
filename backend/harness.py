from __future__ import annotations

from dataclasses import dataclass
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
from routing_signals import (
    derive_user_signals,
    merge_signals_into_analysis,
    norm_suggested_track,
    reasoning_keyword_boost,
)
from search_query_util import soft_degrade_note, validate_search_query
from semantic_utils import is_probably_english, ngram_overlap_ratio, normalize_text, semantic_similarity

from tools.layer import HarnessTools
from tools.parsing import RE_AGENT_REFINE, RE_AGENT_WS, parse_agent_action
from tools.refine_pipeline import compile_agent_fallback_draft, stream_refine_from_draft


REFINE_REVIEW_RETRY_SUFFIX = (
    "\n\n请结合上述联网信息更新审查结论；若仍需核实可再次输出 <<ACTION: web_search(\"查询词\")>>。"
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


def _pg(meta: Optional[Dict[str, Any]], phase_group: str, event_summary: str) -> Dict[str, Any]:
    """为步骤 meta 注入前端分组与叙事摘要。"""
    m = dict(meta or {})
    m["phase_group"] = phase_group
    m["event_summary"] = event_summary
    return m


def _analyze_step_summary(analysis: Dict[str, Any]) -> str:
    tt = str(analysis.get("task_type") or "通用")
    cx = str(analysis.get("complexity") or "—")
    dec = str(analysis.get("decision") or "").strip()
    tail = f"；调度倾向：{dec}" if dec else ""
    return f"归类「{tt}」、复杂度「{cx}」{tail}。"


def _track_select_summary(chosen: str, intended: str, analysis: Dict[str, Any]) -> str:
    names = {"fast": "快速答复", "refine": "草稿→审查→润色", "agent": "Agent 多轮推理"}
    base = names.get(chosen, chosen or "—")
    sm = str(analysis.get("selected_model") or "").strip()
    extra = f" 主选模型：{sm}。" if sm else ""
    if intended and str(intended) != str(chosen):
        return f"原计划「{names.get(intended, intended)}」，实际执行「{base}」。{extra}".strip()
    return f"执行路径：「{base}」。{extra}".strip()


def _int_budget(options: Optional[Dict[str, Any]], key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int((options or {}).get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


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
        "\n如需核实实时数据，可在审查结论中单行输出：<<ACTION: web_search(\"查询词\")>>\n"
    )
    ht = _format_messages_snippet(messages, 4, max_chars=max_history_chars)
    if ht:
        l2_prompt = f"【近期对话上下文参考】\n{ht}\n\n" + l2_prompt
    return l2_prompt


def _clean_review_body(review_body: str) -> str:
    text = str(review_body or "").strip()
    if not text:
        return ""

    m = re.search(r"(?is)(?:修正版答案|最终答案|答案正文|修正后答案)\s*[:：]\s*", text)
    if m:
        rest = text[m.end() :]
        parts = re.split(r"(?is)(?:\n\s*)(?:仍不确定处|不确定点|问题清单)\s*[:：]", rest, maxsplit=1)
        body = parts[0].strip()
        if body:
            return body

    text = re.sub(r"(?is)^\s*初稿问题清单\s*[:：].*?(?=\n\s*(?:修正版答案|最终答案)|\Z)", "", text)
    text = re.sub(r"(?is)\n\s*(?:仍不确定处|不确定点|问题清单)\s*[:：].*$", "", text)

    return text.strip()


def _build_layer3_prompt(prompt: str, instruction: str, review_body: str) -> str:
    review_clean = _clean_review_body(review_body)
    return (
        f"{instruction.strip()}\n\n"
        f"【原始问题】\n{prompt.strip()}\n\n"
        f"【审查层答案】\n{review_clean}\n"
    )


@dataclass
class Step:
    name: str
    status: str  # "ok" | "error" | "skipped"
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_preview: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    def _step_id(self) -> str:
        meta = self.meta or {}
        if meta.get("step_id"):
            return str(meta["step_id"])
        pg = str(meta.get("phase_group") or meta.get("pipeline_phase") or "")
        if self.name == "agent_iteration" and meta.get("i") is not None:
            return f"{self.name}:{meta.get('i')}"
        if self.name == "review_web_search" and meta.get("review_round") is not None:
            return f"{self.name}:{pg}:{meta.get('review_round')}"
        if self.name == "agent_web_search" and meta.get("query"):
            return f"{self.name}:{meta.get('query')}"
        return f"{self.name}:{pg}" if pg else self.name

    def to_dict(self) -> Dict[str, Any]:
        meta = dict(self.meta or {})
        sid = self._step_id()
        meta.setdefault("step_id", sid)
        meta.setdefault("parent_id", meta.get("phase_group") or meta.get("pipeline_phase") or "")
        return {
            "step_id": sid,
            "name": self.name,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_preview": self.input_preview,
            "output": self.output,
            "error": self.error,
            "meta": meta,
        }


class ModelRegistry:
    def __init__(self, models_cfg: Dict[str, Any]):
        self.models_cfg = models_cfg or {}
        self._adapters = {}

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
        self.search = SearchService(cfg)
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

    def _track_search_overrides(self, track: str) -> Dict[str, Any]:
        search_cfg = (self.cfg.get("harness") or {}).get("search") or {}
        by_track = search_cfg.get("by_track") or {}
        base = by_track.get(track) or {}
        return {
            "override_max_results": base.get("max_results"),
            "override_search_depth": base.get("search_depth"),
        }

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

    def _attach_documents_to_prompt(self, prompt: str, options: Optional[Dict[str, Any]]) -> str:
        block = str((options or {}).get("_documents_context_block") or "").strip()
        if not block:
            return prompt
        return (
            f"{block}\n\n"
            "请优先基于上述文档回答；涉及文档信息时，尽量标注来自哪份文档或哪段内容。\n\n"
            f"{prompt}"
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
            self._attach_documents_to_prompt(question, options),
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
    ) -> str:
        return _build_layer3_prompt(
            self._attach_documents_to_prompt(question, options),
            instruction,
            _clean_review_body(review_body),
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
        return out or [routing_default or "gpt-5.5"]

    def _fast_cache_scope(self, options: Dict[str, Any]) -> str:
        return json.dumps(
            {
                "track": str(options.get("_runtime_track") or "fast"),
                "search_mode": str(options.get("search_mode") or options.get("search") or "auto").lower(),
                "documents": str(options.get("_documents_signature") or self._documents_signature(options.get("documents"))),
                "history": str(options.get("_history_signature") or ""),
                "upgrade_track": bool(options.get("upgrade_track")),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

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

    def _select_agent_history_messages(self, messages: Optional[List[Dict[str, Any]]], max_chars: int) -> List[Dict[str, Any]]:
        if not messages:
            return []
        picked: List[Dict[str, Any]] = []
        used = 0
        budget = max(1200, max_chars)
        for msg in reversed(messages):
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _msg_content_to_text(msg.get("content"))
            estimate = max(1, len(text))
            if picked and used + estimate > budget:
                break
            picked.append({"role": role, "content": text})
            used += estimate
        return list(reversed(picked))

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
        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine", "agent"):
            mode = "auto"

        cx_cfg = hcfg.get("complexity") or {}
        analyzer_deadline = float(cx_cfg.get("analyzer_total_timeout_s", 35))
        sig_base = str(options.get("search_prompt_base") or prompt or "").strip()
        search_markers = ("联网", "搜索", "查证", "最新", "今天", "实时", "weather", "news", "stock")
        speculative_search_task = None
        speculative_guess_key = ""
        if any(mark in sig_base.lower() for mark in [m.lower() for m in search_markers]):
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
                sub_opts = {**options, **{k: v for k, v in self._track_search_overrides("fast").items() if v is not None}}
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

        intended_track = self._resolve_track(mode, analysis, options)
        chosen_track = self.apply_confidence_track_guard(mode, analysis, sig_base, intended_track)
        if options.get("upgrade_track"):
            bump = {"fast": "refine", "refine": "agent"}
            nt = bump.get(chosen_track)
            if nt:
                ag_en = bool((hcfg.get("agent") or {}).get("enabled", True))
                if nt == "agent" and not ag_en:
                    nt = "refine"
                    analysis["fallback_reason"] = analysis.get("fallback_reason") or "upgrade_target_agent_disabled"
                analysis["client_track_upgrade"] = f"{chosen_track}->{nt}"
                chosen_track = nt

        should_search, search_reason = self.should_search(prompt, analysis, options)
        search_mandatory = self._search_mandatory(analysis, options)
        if self._should_force_relevance_filter_sync(analysis, options):
            options["relevance_filter_sync"] = True
        if speculative_search_task:
            if should_search:
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
            "chosen_track": chosen_track,
            "should_search": should_search,
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

    async def analyze_complexity(self, prompt: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        hcfg = (self.cfg.get("harness") or {}).get("complexity") or {}
        manual = hcfg.get("manual_triggers") or []
        opts = dict(options or {})
        norm_prompt = self._norm_cache_prompt(prompt)

        manual_hits = self._keyword_hit(prompt, manual)
        if manual_hits:
            return {
                "decision": "refine",
                "reasons": ["manual:" + ",".join(manual_hits[:3])],
                "manual_hits": manual_hits,
                "complexity": "high",
                "type": "general",
                "task_type": "generation",
                "confidence": 1.0,
                "suggested_track": "refine",
            }

        use_llm = bool(hcfg.get("use_llm_analyzer", False))
        if not use_llm:
            # fallback rule if llm disabled
            length = len(prompt or "")
            return {
                "decision": "refine" if length > 200 else "fast",
                "reasons": [f"length={length}"],
                "complexity": "high" if length > 200 else "low",
                "type": "general",
                "task_type": "generation" if length > 200 else "conversation",
                "confidence": 0.55,
                "suggested_track": "refine" if length > 200 else "fast",
            }

        analyzer_model = hcfg.get("analyzer_model", "gpt-5.5")
        base_prompt = hcfg.get("analyzer_prompt", "")
        full_prompt = f"{base_prompt}\n\n{prompt}"

        cache_ttl = int(hcfg.get("analysis_cache_ttl_s", 300))
        cache_key = self._analysis_cache_prefix + hashlib.sha256(norm_prompt.encode("utf-8")).hexdigest()
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
            if res.success:
                # Try to parse JSON from the response
                content = res.content.strip()
                # Often models wrap json in ```json ... ```
                json_match = re.search(r"```(?:json)?(.*?)```", content, re.DOTALL)
                if json_match:
                    content = json_match.group(1).strip()
                
                try:
                    data = json.loads(content)
                    complexity = str(data.get("complexity", "low") or "low").lower()
                    if complexity not in ("low", "medium", "high"):
                        complexity = "low"
                    selected_model = data.get("selected_model", "")
                    fallback_models = data.get("fallback_models", [])
                    refine_models = data.get("refine_models", {})
                    reason = data.get("reason", "")
                    decision = str(data.get("decision", "") or "").strip().lower()
                    if decision not in ("fast", "refine"):
                        decision = "refine" if complexity in ("high", "medium") else "fast"
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
                    suggested_track = str(data.get("suggested_track") or "").strip().lower()
                    if suggested_track not in ("fast", "refine", "agent", ""):
                        suggested_track = ""
                    raw_search_queries = data.get("search_queries")
                    search_queries = []
                    if isinstance(raw_search_queries, list):
                        search_queries = [str(item or "").strip() for item in raw_search_queries if str(item or "").strip()]
                    elif str(data.get("search_query") or "").strip():
                        search_queries = [str(data.get("search_query") or "").strip()]

                    result = {
                        "decision": decision,
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
                        "raw_llm_response": res.content,
                        "confidence": confidence,
                        "suggested_track": suggested_track or None,
                        "search_queries": search_queries,
                    }
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
                except json.JSONDecodeError:
                    return {
                        "decision": "fast",
                        "reasons": ["json_parse_error"],
                        "complexity": "low",
                        "type": "general",
                        "task_type": "conversation",
                        "raw_llm_response": res.content,
                        "confidence": 0.35,
                        "suggested_track": "fast",
                    }
        except Exception as e:
            return {
                "decision": "fast",
                "reasons": [f"llm_analyzer_error: {str(e)}"],
                "complexity": "low",
                "type": "general",
                "task_type": "conversation",
                "confidence": 0.35,
                "suggested_track": "fast",
            }
            
        return {
            "decision": "fast",
            "reasons": ["analyzer_failed"],
            "complexity": "low",
            "type": "general",
            "task_type": "conversation",
            "confidence": 0.3,
            "suggested_track": "fast",
        }

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
            "decision": "refine" if choose_refine else "fast",
            "reasons": ["analyzer_total_timeout"],
            "complexity": "high" if choose_refine else "low",
            "type": "code" if has_code else "general",
            "task_type": task_type,
            "reason": "复杂度预判超时，已按多特征启发规则降级并继续处理",
            "analyzer_timed_out": True,
            "confidence": 0.55 if choose_refine else 0.45,
            "suggested_track": "agent" if has_code else ("refine" if choose_refine else "fast"),
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

        if not unique_candidates:
            unique_candidates = default_models

        return {
            "rule": "llm_autonomous_choice",
            "hits": [f"reason:{analysis.get('reason', 'none')}"],
            "candidates": unique_candidates,
            "selected": unique_candidates[0],
        }

    def apply_confidence_track_guard(self, mode: str, analysis: Dict[str, Any], prompt: str, chosen: str) -> str:
        if (mode or "auto").lower() != "auto":
            return chosen
        if str(analysis.get("output_intent") or "").lower() == "fast":
            return chosen
        try:
            cf = float(analysis.get("confidence", 1.0) or 1.0)
        except (TypeError, ValueError):
            cf = 1.0
        if cf < 0.6 and len((prompt or "").strip()) > 200 and chosen == "fast":
            analysis["track_upgrade_reason"] = "low_analyzer_confidence"
            return "refine"
        return chosen

    def _merge_task_model_templates(self, analysis: Dict[str, Any]) -> None:
        hcfg = self.cfg.get("harness") or {}
        tpl_root = hcfg.get("task_model_templates") or {}
        tt = str(analysis.get("task_type") or "conversation").lower()
        tpl = tpl_root.get(tt) or tpl_root.get("conversation") or {}
        if not tpl:
            return
        if not str(analysis.get("selected_model") or "").strip() and tpl.get("selected_model"):
            analysis["selected_model"] = tpl["selected_model"]
        if not analysis.get("fallback_models") and tpl.get("fallback_models"):
            analysis["fallback_models"] = list(tpl["fallback_models"])
        rm = analysis.get("refine_models")
        if not isinstance(rm, dict):
            rm = {}
        rtpl = tpl.get("refine_models") or {}
        for k in ("draft", "review", "polish"):
            if not rm.get(k) and rtpl.get(k):
                rm[k] = list(rtpl[k])
        if rm:
            analysis["refine_models"] = rm

    def _postprocess_analysis(self, prompt: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        out = {**analysis}
        self._merge_task_model_templates(out)
        hit, reason = reasoning_keyword_boost(prompt)
        if hit and str(out.get("complexity") or "low").lower() == "low":
            out["complexity"] = "high"
            out["task_type"] = "reasoning"
            out["decision"] = "refine"
            out["reasoning_rule_boost"] = reason
        return out

    def _norm_cache_prompt(self, p: str) -> str:
        return re.sub(r"\s+", " ", (p or "").strip().lower())

    def _fast_cache_key_source(self, options: Dict[str, Any], augmented_prompt: str) -> str:
        """缓存键分为正文相似度键 + 作用域键，作用域需隔离文档/历史/搜索模式/轨道。"""
        opts = options or {}
        return str(opts.get("_fast_cache_identity") or opts.get("search_prompt_base") or augmented_prompt or "").strip()

    async def _try_fast_cache_hit(self, options: Dict[str, Any], augmented_prompt: str) -> Optional[str]:
        fcfg = (self.cfg.get("harness") or {}).get("fast_answer_cache") or {}
        if not fcfg.get("enabled") or not self._redis:
            return None
        key_src = self._fast_cache_key_source(options, augmented_prompt)
        norm = self._norm_cache_prompt(key_src)
        scope = self._fast_cache_scope(options)
        pref = str(fcfg.get("key_prefix") or "harness:fast:v1:")
        thresh = float(fcfg.get("similarity_threshold", 0.92))
        max_scan = int(fcfg.get("max_scan_keys", 400))
        key = pref + hashlib.sha256(f"{scope}\n{norm}".encode("utf-8")).hexdigest()
        zkey = pref + "zset"

        def _sync() -> Optional[str]:
            v = self._redis.get(key)
            if v:
                return str(v)
            raw = self._redis.zrevrange(zkey, 0, max(0, max_scan - 1))
            best_a: Optional[str] = None
            best_r = 0.0
            for row in raw or []:
                try:
                    o = json.loads(row)
                    n2 = str(o.get("n") or "")
                    s2 = str(o.get("s") or "")
                    if s2 != scope:
                        continue
                    r = semantic_similarity(norm, n2)
                    if r > best_r:
                        best_r = r
                        best_a = str(o.get("a") or "")
                except Exception:
                    continue
            if best_r >= thresh and best_a:
                return best_a
            return None

        try:
            return await asyncio.to_thread(_sync)
        except Exception:
            return None

    async def _store_fast_cache_answer(self, options: Dict[str, Any], augmented_prompt: str, answer: str) -> None:
        fcfg = (self.cfg.get("harness") or {}).get("fast_answer_cache") or {}
        if not fcfg.get("enabled") or not self._redis or not (answer or "").strip():
            return
        key_src = self._fast_cache_key_source(options, augmented_prompt)
        norm = self._norm_cache_prompt(key_src)
        scope = self._fast_cache_scope(options)
        pref = str(fcfg.get("key_prefix") or "harness:fast:v1:")
        ttl = int(fcfg.get("ttl_sec", 86400))
        key = pref + hashlib.sha256(f"{scope}\n{norm}".encode("utf-8")).hexdigest()
        row = json.dumps({"s": scope, "n": norm, "a": answer}, ensure_ascii=False)
        zkey = pref + "zset"

        def _sync() -> None:
            self._redis.set(key, answer, ex=ttl)
            self._redis.zadd(zkey, {row: time.time()})
            size = self._redis.zcard(zkey)
            if size and int(size) > 200:
                self._redis.zremrangebyrank(zkey, 0, int(size) - 201)

        try:
            await asyncio.to_thread(_sync)
        except Exception:
            pass

    def _layer_opts(self, hcfg: Dict[str, Any], layer_key: str, base: Dict[str, Any]) -> Dict[str, Any]:
        chain = hcfg.get("refine_chain") or {}
        lay = chain.get(layer_key) or {}
        t = float(lay.get("temperature", 0.2))
        return {**base, "temperature": t}

    def should_search(self, prompt: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        opts = options or {}
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

        if re.search(r"\b(vue|react|python|node|typescript|java|spring|fastapi|django)\b", cl) and re.search(r"\b\d", cl):
            add_token(str(datetime.now().year) if english else f"{datetime.now().year}年")

        person_suffixes = ("先生", "女士", "老师", "教授", "博士")
        for suffix in person_suffixes:
            if base.endswith(suffix) and len(base) > len(suffix) + 1:
                base = base[: -len(suffix)].strip()
                break

        if extras:
            base = f"{base} " + " ".join(extras)
        base = base.strip()
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
        return (
            f"{search_context}\n"
            "【约束】优先采用与用户提问中实体（地点、时间范围、主题等）一致的检索片段；"
            "若摘要明显指向其他实体或不匹配用户所指范围，勿当作对用户问题的直接证据，应在答复中说明不确定性，并可建议用户收窄表述或改用更权威的本地官方发布渠道核实。\n\n"
            f"{anchor_block}"
            "请在答复中对采用的检索内容标注来源序号（如 [1]），并在文末列出引用链接。\n\n"
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
        self, candidates: List[str], prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        last_error = ""
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
                }
                started_at = time.perf_counter()
                emitted_chars = 0

                async for chunk in adapter.stream(prompt, options, messages=messages):
                    content = chunk.get("content") or ""
                    reasoning = chunk.get("reasoning_content") or ""
                    if content:
                        emitted_chars += len(content)
                        step = max(1, int(options.get("stream_slice_chars") or 96))
                        for idx in range(0, len(content), step):
                            yield {"event": "chunk", "data": {"content": content[idx : idx + step]}}
                            await asyncio.sleep(0)
                    elif reasoning:
                        # 计入非空流，避免仅 reasoning 的模型被误判 EMPTY_STREAM；正文仍只推送 content
                        emitted_chars += len(reasoning)

                if emitted_chars <= 0:
                    last_error = f"{mk} stream ended without content"
                    yield {
                        "event": "model_error",
                        "model": mk,
                        "error": last_error,
                        "error_code": "EMPTY_STREAM",
                    }
                    continue

                yield {
                    "event": "model_end",
                    "model": mk,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "chars": emitted_chars,
                }
                return
            except Exception as e:
                last_error = str(e)
                yield {"event": "model_error", "model": mk, "error": last_error}
                continue

        yield {
            "event": "error",
            "error": f"All fallback models failed in stream. Last error: {last_error or 'unknown'}",
            "error_code": "STREAM_FALLBACK_EXHAUSTED",
        }

    def _session_search_redis_ttl(self, options: Dict[str, Any]) -> int:
        h = self.cfg.get("harness") or {}
        scfg = h.get("search") or {}
        base = int(scfg.get("session_cache_ttl_s", 1800))
        fresh_ttl = int(scfg.get("session_cache_ttl_freshness_s", 600))
        si = str(options.get("_effective_search_intent") or options.get("search_intent") or "none").lower()
        if si == "freshness_required":
            return max(60, fresh_ttl)
        return max(60, base)

    def _search_mandatory(self, analysis: Dict[str, Any], options: Dict[str, Any]) -> bool:
        si = str(analysis.get("search_intent") or "none").lower()
        if si in ("required", "freshness_required"):
            return True
        if bool(analysis.get("search_required")):
            return True
        sm = str(options.get("search_mode") or "").lower()
        if sm in ("on", "true", "1", "force", "always"):
            return True
        return False

    async def perform_web_search(self, query: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        options = options or {}
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
        if cache_key in cache:
            return dict(cache[cache_key])

        session_id = str(options.get("session_id") or "").strip()
        if self._redis and session_id:
            try:
                cached = await asyncio.to_thread(self._redis.get, self._session_search_cache_key(session_id, vq))
                if cached:
                    data = json.loads(cached)
                    data["cached"] = True
                    cache[cache_key] = data
                    return dict(data)
            except Exception:
                pass

        sr = await self.search.search(
            vq,
            override_max_results=options.get("override_max_results"),
            override_search_depth=options.get("override_search_depth"),
        )
        if sr.get("error") or not (sr.get("sources") or []):
            cache[cache_key] = dict(sr)
            return sr
        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        if not rcfg.get("enabled", False):
            cache[cache_key] = dict(sr)
            return sr
        if not bool(options.get("relevance_filter_sync", False)):
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
            return sr
        try:
            from search_relevance import filter_sources_by_relevance, rebuild_context_from_sources

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
        """共享：审查层内 <<ACTION: web_search>> 多轮核查的状态机（供 run / run_stream / refine_pipeline 复用）。"""
        extra_ctx = ""
        search_loops = 0
        rb = (review_body or "").strip()
        overrides = {k: v for k, v in self._track_search_overrides(track).items() if v is not None}
        for _ in range(3):
            wm = RE_AGENT_WS.search(rb)
            if not wm:
                break
            search_loops += 1
            q = wm.group(1).strip()
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

    async def _fast_entry_search_step(
        self,
        prompt: str,
        analysis: Dict[str, Any],
        options: Dict[str, Any],
        search_reason: str,
        *,
        search_mandatory: bool,
    ) -> Tuple[str, Step, Optional[str]]:
        """
        Fast 轨入口联网：校验 query、区分强制/可选失败策略。
        返回 (更新后的 prompt, step, 若需中止整条流则非空 error 文案)。
        """
        queries = self._build_search_queries(prompt, analysis, options)
        raw_q = queries[0] if queries else self.build_search_query(prompt, analysis, options)
        vq, fc, vreason = validate_search_query(raw_q)
        if fc:
            meta = _pg(
                {
                    "query": raw_q,
                    "query_effective": None,
                    "reason": search_reason,
                    "failure_code": fc,
                    "skipped": True,
                    "validate_only": True,
                },
                "search",
                f"检索词未通过校验（{vreason or fc}），已按策略处理。",
            )
            if search_mandatory:
                err = vreason or fc
                return (
                    prompt,
                    Step(
                        name="web_search",
                        status="error",
                        meta=meta,
                        error=err,
                    ),
                    err,
                )
            note = soft_degrade_note(fc, vreason)
            return (
                prompt + "\n\n" + note,
                Step(
                    name="web_search",
                    status="ok",
                    meta=_pg(
                        {**meta, "degraded": True, "note": "校验未通过，已跳过检索调用"},
                        "search",
                        "快轨入口：检索词未过审，已跳过联网并继续生成。",
                    ),
                ),
                None,
            )

        merged_sources: List[Dict[str, Any]] = []
        merged_contexts: List[str] = []
        merged_attempts: List[Dict[str, Any]] = []
        hard_err = None
        fallback_from = None
        total_latency_ms = 0
        overrides = self._track_search_overrides("fast")
        sub_opts = {**options, **{k: v for k, v in overrides.items() if v is not None}}
        results = await asyncio.gather(*[self.perform_web_search(q, sub_opts) for q in queries[:4]])
        seen_urls = set()
        for sr in results:
            total_latency_ms += int(sr.get("latency_ms") or 0)
            merged_attempts.extend(list(sr.get("attempts") or []))
            fallback_from = fallback_from or sr.get("fallback_from")
            if sr.get("error") and not hard_err:
                hard_err = sr.get("error")
            for src in sr.get("sources") or []:
                url = str(src.get("url") or "")
                key = url or json.dumps(src, ensure_ascii=False, sort_keys=True)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                merged_sources.append(src)
            ctx = str(sr.get("context") or "").strip()
            if ctx:
                merged_contexts.append(ctx)
        search_context = "\n\n".join(merged_contexts)
        sources = merged_sources
        sr = {
            "context": search_context,
            "sources": sources,
            "error": hard_err,
            "failure_code": results[0].get("failure_code") if results else None,
            "degraded": any(bool(item.get("degraded")) for item in results),
            "provider_used": next((item.get("provider_used") for item in results if item.get("provider_used")), "none"),
            "latency_ms": total_latency_ms,
            "attempts": merged_attempts,
            "fallback_from": fallback_from,
        }
        nsrc = len(sources)
        meta = {
            "query": raw_q,
            "query_effective": vq,
            "queries": queries,
            "reason": search_reason,
            "sources": sources,
            "result_count": nsrc,
            "failure_code": sr.get("failure_code"),
            "degraded": bool(sr.get("degraded")),
            "attempts": sr.get("attempts") or [],
            "fallback_from": sr.get("fallback_from"),
            "results_preview": search_context[:500] + ("..." if len(search_context) > 500 else ""),
        }
        ok_summary = f"快轨入口：已检索「{(vq or raw_q)[:72]}{'…' if len(str(vq or raw_q)) > 72 else ''}」，摘要已并入上下文（约 {nsrc} 条来源）。"
        if search_mandatory and not hard_err and not sources:
            fc_empty = str(sr.get("failure_code") or "SEARCH_EMPTY_SOURCES")
            user_err = f"强制联网未获得有效来源（{fc_empty}）。"
            return (
                prompt,
                Step(
                    name="web_search",
                    status="error",
                    provider=sr.get("provider_used"),
                    latency_ms=sr.get("latency_ms"),
                    meta=_pg(
                        {**meta, "failure_code": fc_empty},
                        "search",
                        user_err,
                    ),
                    error=user_err,
                ),
                user_err,
            )
        if hard_err and search_mandatory:
            return (
                prompt,
                Step(
                    name="web_search",
                    status="error",
                    provider=sr.get("provider_used"),
                    latency_ms=sr.get("latency_ms"),
                    meta=_pg(meta, "search", f"联网失败（强制检索）：{hard_err}"),
                    error=hard_err,
                ),
                f"当前无法完成实时联网检索：{hard_err}",
            )
        if hard_err:
            note = soft_degrade_note(sr.get("failure_code"), hard_err)
            return (
                prompt + "\n\n" + note,
                Step(
                    name="web_search",
                    status="ok",
                    provider=sr.get("provider_used"),
                    latency_ms=sr.get("latency_ms"),
                    meta=_pg(
                        {**meta, "degraded": True},
                        "search",
                        f"联网未完全成功，已降级继续：{hard_err}",
                    ),
                ),
                None,
            )
        st = Step(
            name="web_search",
            status="ok",
            provider=sr.get("provider_used"),
            latency_ms=sr.get("latency_ms"),
            meta=_pg(meta, "search", ok_summary),
        )
        if search_context:
            prompt = self._merge_search_into_prompt(prompt, search_context, options)
        return prompt, st, None

    async def _refine_entry_light_search(
        self,
        prompt: str,
        analysis: Dict[str, Any],
        options: Dict[str, Any],
    ) -> str:
        """Refine 轨：显式联网意图下 Layer1 前轻量检索，失败仅注入说明、不中止。"""
        queries = self._build_search_queries(prompt, analysis, options)
        raw_q = queries[0] if queries else self.build_search_query(prompt, analysis, options)
        vq, fc, vreason = validate_search_query(raw_q)
        if fc:
            return f"\n【入口联网】检索词未通过校验（{vreason or fc}），请依赖常识与后续审查层按需检索。\n"
        overrides = self._track_search_overrides("refine")
        sub_opts = {**options, **{k: v for k, v in overrides.items() if v is not None}}
        results = await asyncio.gather(*[self.perform_web_search(q, sub_opts) for q in queries[:4]])
        err = next((item.get("error") for item in results if item.get("error")), None)
        if err and not any(item.get("sources") for item in results):
            return f"\n【入口联网】检索未成功：{err}。后续审查层仍可输出 <<ACTION: web_search(\"...\")>> 复核。\n"
        ctx = "\n\n".join(str(item.get("context") or "").strip() for item in results if str(item.get("context") or "").strip())
        if not ctx:
            return "\n【入口联网】未获得有效摘要，请后续审查层按需检索。\n"
        return f"\n【入口联网摘要（供初稿参考）】\n{ctx[:6000]}\n"

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

    def _resolve_track(self, mode: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> str:
        """auto：三轨分流；手动 mode 优先；Agent 关闭时显式降级并写入 fallback_reason。"""
        _opts = options or {}
        mode = (mode or "auto").lower()
        hcfg = self.cfg.get("harness") or {}
        ag_enabled = bool((hcfg.get("agent") or {}).get("enabled", True))
        if mode == "fast":
            return "fast"
        if mode == "refine":
            return "refine"
        if mode == "agent":
            if ag_enabled:
                return "agent"
            analysis["agent_disabled_fallback"] = True
            analysis["fallback_reason"] = "agent_disabled_by_config"
            return "refine"

        if mode == "auto" and analysis.get("high_risk_domain"):
            analysis["search_required"] = True
            si_h = str(analysis.get("search_intent") or "none").lower()
            if si_h in ("none", "optional"):
                analysis["search_intent"] = "required"
            if ag_enabled:
                return "agent"
            analysis["fallback_reason"] = analysis.get("fallback_reason") or "high_risk_agent_disabled"
            return "refine"

        tt = str(analysis.get("task_type") or "").strip().lower()
        if not tt:
            tt = self._infer_task_type_from_json(analysis)
        analysis["task_type"] = tt
        cx = str(analysis.get("complexity") or "low").lower()
        oi = str(analysis.get("output_intent") or "neutral").lower()
        si = str(analysis.get("search_intent") or "none").lower()

        if oi == "fast":
            return "fast"
        if oi == "deep":
            if ag_enabled and tt in ("reasoning", "code"):
                return "agent"
            return "refine"

        if si in ("required", "freshness_required"):
            if ag_enabled and tt in ("reasoning", "code"):
                return "agent"
            if tt == "generation" or cx in ("high", "medium"):
                return "refine"
            return "refine"

        if si == "explicit" and tt == "conversation" and cx == "low":
            dec = str(analysis.get("decision") or "fast").lower()
            if dec == "fast":
                return "refine"

        if ag_enabled and tt in ("reasoning", "code"):
            return "agent"
        if tt in ("reasoning", "code") and not ag_enabled:
            analysis["fallback_reason"] = "agent_disabled_by_config"
            analysis["agent_disabled_fallback"] = True
            return "refine"

        if cx == "low" and tt == "conversation":
            return "fast"
        if tt == "generation":
            return "refine"
        try:
            cf = float(analysis.get("confidence") or 1.0)
        except (TypeError, ValueError):
            cf = 1.0
        if mode == "auto" and cf < 0.55:
            st = norm_suggested_track(str(analysis.get("suggested_track") or ""))
            if st == "agent" and ag_enabled:
                analysis["track_select_low_confidence"] = True
                return "agent"
            if st == "refine":
                analysis["track_select_low_confidence"] = True
                return "refine"
            if st == "fast":
                analysis["track_select_low_confidence"] = True
                return "fast"
        dec = str(analysis.get("decision") or "fast").lower()
        return "refine" if dec == "refine" else "fast"

    def _should_skip_refine_draft(self, prompt: str, analysis: Dict[str, Any], options: Dict[str, Any]) -> bool:
        if bool(options.get("skip_draft")):
            return True
        if str(analysis.get("search_intent") or "none").lower() in ("required", "freshness_required"):
            return False
        text = str(options.get("search_prompt_base") or prompt or "").strip()
        markers = ("帮我改进", "润色这段", "优化这段", "修改这段文字", "改写下面", "improve this", "polish this", "rewrite this")
        return any(marker.lower() in text.lower() for marker in markers)

    async def _emit_text_chunks(self, text: str, options: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        step = max(1, int(options.get("stream_slice_chars") or 96))
        for i in range(0, len(text), step):
            yield {"event": "chunk", "data": {"content": text[i : i + step]}}
            await asyncio.sleep(0)

    def _agent_plain_text_should_refine(self, analysis: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Agent 轨：本轮无 ACTION 的纯文本是否必须改为草稿走 Review→Polish。
        条件：高复杂度 / 分析器倾向精化 / 强时效或高风险信号。
        """
        cx = str(analysis.get("complexity") or "low").lower()
        dec = str(analysis.get("decision") or "fast").lower()
        si = str(analysis.get("search_intent") or "none").lower()
        if cx == "high":
            return True, "complexity_high"
        if dec == "refine":
            return True, "decision_refine"
        if bool(analysis.get("search_required")):
            return True, "search_required"
        if si in ("required", "freshness_required"):
            return True, f"search_intent_{si}"
        if analysis.get("manual_hits"):
            return True, "manual_keyword_trigger"
        raw_type = str(analysis.get("type") or "").lower()
        if raw_type == "web_search":
            return True, "type_web_search"
        return False, ""

    def _pick_agent_self_check_model(self, hcfg: Dict[str, Any], analysis: Dict[str, Any], agent_model: str) -> str:
        """非空 agent_self_check_model 为强制覆盖；否则按推理向 selected → reasoning.review[0] → 主模型 → 路由默认。"""
        scfg = hcfg.get("complexity") or {}
        override = str(scfg.get("agent_self_check_model") or "").strip()
        if override:
            return override
        raw_rk = scfg.get("self_check_reasoning_models")
        if isinstance(raw_rk, list) and raw_rk:
            rk = {str(x).strip() for x in raw_rk if str(x).strip()}
        else:
            rk = {
                "claude-sonnet-4-6-thinking",
                "grok-4-20-reasoning",
                "deepseek-v4-pro",
            }
        sm = str(analysis.get("selected_model") or "").strip()
        if sm and sm in rk:
            return sm
        tpl = hcfg.get("task_model_templates") or {}
        rev = (tpl.get("reasoning") or {}).get("review") or []
        if isinstance(rev, list) and rev:
            first = str(rev[0]).strip()
            if first:
                return first
        am = str(agent_model or "").strip()
        if am:
            return am
        routing = self.cfg.get("routing") or {}
        default_model = str(routing.get("default_model") or "gpt-5.5").strip()
        dm = routing.get("default_models") or [default_model]
        if isinstance(dm, list) and dm:
            z = str(dm[0]).strip()
            if z:
                return z
        return default_model or "gpt-5.5"

    async def _agent_self_check_block(
        self,
        orig_q: str,
        draft: str,
        hcfg: Dict[str, Any],
        analysis: Dict[str, Any],
        options: Dict[str, Any],
        agent_model: str,
    ) -> str:
        if str(analysis.get("complexity") or "low").lower() != "high":
            return ""
        model = self._pick_agent_self_check_model(hcfg, analysis, agent_model)
        check_prompt = (
            "你是审稿助手。请用条目列出相对用户问题的关键缺口、不确定处与建议查证点（不超过 900 字），"
            "不要重复草稿全文。\n【用户问题】\n"
            f"{(orig_q or '')[:4000]}\n【草稿】\n{(draft or '')[:8000]}"
        )
        opts = {**options, "temperature": 0.15, "max_retries": 0}
        res, _ = await self._ask_with_fallback([model], check_prompt, opts, messages=None)
        return (res.content or "").strip() if res and res.success else ""

    async def _refine_from_draft_stream(
        self,
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
        """Agent 的 refine_answer：委托工具层 stream_refine_from_draft。"""
        async for ev in stream_refine_from_draft(
            self,
            question,
            draft_text,
            options,
            messages,
            trace_id,
            hcfg,
            analysis,
            meta_extra=meta_extra,
            extra_review_context=extra_review_context,
        ):
            yield ev

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
        acfg = hcfg.get("agent") or {}
        routing = hcfg.get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")
        agent_candidates = self._agent_model_candidates(hcfg, analysis, default_model)
        agent_model = agent_candidates[0]
        max_map = acfg.get("max_iterations_by_complexity") or {}
        cx = str(analysis.get("complexity") or "low").lower()
        max_iter = int(max_map.get(cx) or acfg.get("max_iterations", 5))
        max_iter = max(3, min(8, max_iter))
        agent_intro = (acfg.get("system_prompt") or "").strip()
        base_rules = (
            "你是具备工具调用能力的智能体，按「思考 → 行动 → 观察」循环推理。\n"
            "优先使用严格 JSON 工具动作，例如：\n"
            "{\"action\":\"web_search\",\"query\":\"查询词\"}\n"
            "{\"action\":\"refine_answer\",\"question\":\"用户原问题\",\"draft\":\"你的结论草稿\"}\n"
            "JSON 动作必须单独输出，且不要包裹额外正文。\n"
            "需要实时信息时输出单行（仅此一行即可）：<<ACTION: web_search(\"查询词\")>>\n"
            "推理已完成且需要长文润色/结构化输出时输出：\n"
            "<<ACTION: refine_answer(\"用户原问题\", \"你的结论草稿\")>>\n"
            "其中两段字符串用英文双引号包裹；草稿内请勿出现未转义的双引号。\n"
            "若能直接给出简短最终答案，则直接输出正文，不要虚构 ACTION。\n"
            "若任务为高复杂度、分析器倾向精化或涉及时效/高风险主题，服务器可能将你本轮无 ACTION 的正文视为草稿并强制进入审查与润色流程。\n"
        )
        sys_content = (agent_intro + "\n\n" + base_rules).strip()
        thread_msgs: List[Dict[str, Any]] = []
        if messages:
            history_budget = _int_budget(options, "agent_history_context_chars", 12000, minimum=2000, maximum=40000)
            for m in self._select_agent_history_messages(messages, history_budget):
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                thread_msgs.append({"role": role, "content": _msg_content_to_text(m.get("content"))})
        agent_prompt = self._attach_documents_to_prompt(prompt, options)
        conv: List[Dict[str, Any]] = [{"role": "system", "content": sys_content}] + thread_msgs + [{"role": "user", "content": agent_prompt}]
        options.setdefault("_agent_loop_ctx", {"last_query": "", "last_ok": False, "all_queries": []})

        yield {
            "event": "step",
            "step": {
                "name": "agent_start",
                "status": "ok",
                "meta": _pg(
                    {
                        "model": agent_model,
                        "model_candidates": agent_candidates,
                        "max_iterations": max_iter,
                        "thread_turns": len(thread_msgs),
                        "phase": "初始化推理线程（system + 近期对话 + 当前问题）",
                    },
                    "reasoning",
                    f"Agent 已启动：主模型「{agent_model}」，最多 {max_iter} 轮「思考→行动→观察」。",
                ),
            },
        }
        yield {"event": "status", "phase": "agent", "message": "正在分析问题并规划工具调用…"}

        for it in range(max_iter):
            yield {
                "event": "step",
                "step": {
                    "name": "agent_iteration",
                    "status": "running",
                    "meta": _pg(
                        {
                            "i": it + 1,
                            "max": max_iter,
                            "phase": "调用主模型生成本轮策略与正文",
                            "model": agent_model,
                        },
                        "reasoning",
                        f"第 {it + 1}/{max_iter} 轮：正在调用主模型推理…",
                    ),
                },
            }
            res, _att = await self._ask_with_fallback(agent_candidates, "", options, messages=conv)
            if not res.success:
                yield {"event": "error", "error": res.error or "Agent 调用失败"}
                return
            text = (res.content or "").strip()
            conv.append({"role": "assistant", "content": text})

            wm0 = RE_AGENT_WS.search(text)
            rm0 = RE_AGENT_REFINE.search(text)
            parse_text = text
            if wm0 and rm0:
                if wm0.start() < rm0.start():
                    parse_text = text[: rm0.start()].rstrip()
                    conv[-1]["content"] = parse_text
                else:
                    parse_text = text[: wm0.start()].rstrip()
                    conv[-1]["content"] = parse_text

            action = parse_agent_action(parse_text)
            action_name = action.get("action") or ""
            if action_name == "refine_answer":
                next_move = "refine_answer"
            elif action_name == "web_search":
                next_move = "web_search"
            else:
                next_move = "direct_reply"
            preview = text[:400] + ("…" if len(text) > 400 else "")
            branch_cn = {
                "refine_answer": "进入审查与润色全链",
                "web_search": "触发联网检索后继续推理",
                "direct_reply": "本轮将直接流式输出答复",
            }.get(next_move, next_move)
            iter_summary = (
                f"第 {it + 1}/{max_iter} 轮思考完成：下一步 — {branch_cn}。"
                f"（模型输出约 {len(text)} 字）"
            )
            yield {
                "event": "step",
                "step": {
                    "name": "agent_iteration",
                    "status": "ok",
                    "provider": res.provider,
                    "model": res.model,
                    "latency_ms": res.latency_ms,
                    "meta": _pg(
                        {
                            "i": it + 1,
                            "max": max_iter,
                            "next_move": next_move,
                            "branch_next": {
                                "refine_answer": "进入 Refine 全链（审查+按需检索+润色）",
                                "web_search": "触发联网检索后继续推理",
                                "direct_reply": "本轮输出短文答复（流式）",
                            }.get(next_move, next_move),
                            "reply_preview": preview,
                            "reply_chars": len(text),
                            "phase": "本轮模型调用已完成，下一步按分支继续",
                        },
                        "reasoning",
                        iter_summary,
                    ),
                },
            }

            if action_name == "refine_answer":
                orig_q = (action.get("question") or "").strip() or prompt
                draft = (action.get("draft") or "").strip()
                if len(draft) < 16:
                    conv.append(
                        {
                            "role": "user",
                            "content": "【系统】refine_answer 草稿过短或为空；请先 web_search 补充事实或输出完整草稿后再调用 refine_answer。",
                        }
                    )
                    yield {
                        "event": "step",
                        "step": {
                            "name": "agent_refine_answer",
                            "status": "skipped",
                            "meta": _pg(
                                {"reason": "empty_or_tiny_draft", "draft_len": len(draft)},
                                "polishing",
                                "refine_answer 草稿过短，已跳过并回到推理循环。",
                            ),
                        },
                    }
                    continue
                extra_sc = await self._agent_self_check_block(orig_q, draft, hcfg, analysis, options, agent_model)
                if extra_sc:
                    yield {
                        "event": "step",
                        "step": {
                            "name": "agent_self_check",
                            "status": "ok",
                            "meta": _pg(
                                {**_tag("review"), "chars": len(extra_sc)},
                                "polishing",
                                f"高复杂度自检完成（约 {len(extra_sc)} 字），供审查层参考。",
                            ),
                        },
                    }
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_refine_answer",
                        "status": "running",
                        "meta": _pg({}, "polishing", "按 Agent 工具调用：进入审查 → 按需联网 → 润色…"),
                    },
                }
                yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
                refine_ok = True
                async for ev in self._refine_from_draft_stream(
                    orig_q,
                    draft,
                    options,
                    messages,
                    trace_id,
                    hcfg,
                    analysis,
                    meta_extra={"agent_tool": "refine_answer"},
                    extra_review_context=extra_sc,
                ):
                    yield ev
                    if ev.get("event") == "error":
                        refine_ok = False
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_refine_answer",
                        "status": "ok" if refine_ok else "error",
                        "meta": _pg(
                            {},
                            "polishing",
                            "Agent 触发的审查与润色流水线已完成。"
                            if refine_ok
                            else "Agent 触发的审查与润色流水线失败。",
                        ),
                        "error": None if refine_ok else "Agent refine_answer failed",
                    },
                }
                return

            if action_name == "web_search":
                query = (action.get("query") or "").strip()
                lo = options.get("_agent_loop_ctx") or {}
                history_queries = list(lo.get("all_queries") or [])
                repeated = False
                for old_query in history_queries:
                    if ngram_overlap_ratio(old_query, query) >= 0.7:
                        repeated = True
                        break
                if repeated:
                    conv.append(
                        {
                            "role": "user",
                            "content": "【系统】该检索词与之前轮次高度重复，请换角度、泛化关键词或拆分子问题后再搜索。",
                        }
                    )
                    yield {
                        "event": "step",
                        "step": {
                            "name": "agent_web_search",
                            "status": "skipped",
                            "meta": _pg({"query": query, "reason": "repeat_query_detected"}, "reasoning", "检测到重复搜索，已要求 Agent 改写查询词。"),
                        },
                    }
                    continue
                vq, vfc, vreason = validate_search_query(query)
                if vfc:
                    conv.append(
                        {
                            "role": "user",
                            "content": f"【系统】检索词未通过校验：{vreason or vfc}。请改写查询词后仅输出一行 <<ACTION: web_search(\"...\")>>。",
                        }
                    )
                    yield {
                        "event": "step",
                        "step": {
                            "name": "agent_web_search",
                            "status": "skipped",
                            "meta": _pg(
                                {"query": query, "failure_code": vfc},
                                "reasoning",
                                f"联网动作已跳过：检索词未过审（{vreason or vfc}）。",
                            ),
                        },
                    }
                    continue
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_web_search",
                        "status": "running",
                        "meta": _pg(
                            {"query": vq},
                            "reasoning",
                            f"行动：联网搜索「{str(vq)[:72]}{'…' if len(str(vq)) > 72 else ''}」…",
                        ),
                    },
                }
                track_overrides = self._track_search_overrides("agent")
                sr = await self.tools.web_search(vq, {**options, **{k: v for k, v in track_overrides.items() if v is not None}})
                ctx = (sr.get("context") or "")[:12000]
                n_ok = len(sr.get("sources") or [])
                st = Step(
                    name="agent_web_search",
                    status="error" if sr.get("error") else "ok",
                    meta=_pg(
                        {"query": vq, "query_raw": query, "sources": sr.get("sources") or [], "from": "agent"},
                        "reasoning",
                        (
                            f"观察：检索返回约 {n_ok} 条来源，摘要已注入对话。"
                            if not sr.get("error")
                            else f"观察：检索失败 — {sr.get('error') or 'error'}"
                        ),
                    ),
                    error=sr.get("error"),
                )
                yield {"event": "step", "step": st.to_dict()}
                if sr.get("error") or not (sr.get("sources") or []):
                    hint = ""
                    if lo.get("last_query"):
                        hint = (
                            f"【系统】上一轮检索词为「{lo.get('last_query')}」。"
                            "请换用不同角度、更泛化或拆分子问题的检索词，避免重复同一查询。"
                        )
                    conv.append(
                        {
                            "role": "user",
                            "content": f"【观察】搜索失败或无有效结果：{sr.get('error') or 'empty'}。{hint}请换查询词或继续推理。",
                        }
                    )
                else:
                    lo["last_query"] = vq
                    lo["last_ok"] = True
                    lo.setdefault("all_queries", []).append(vq)
                    conv.append({"role": "user", "content": f"【观察】联网摘要：\n{ctx}\n请继续推理或给出最终答案。"})
                continue

            force_refine, coerce_reason = self._agent_plain_text_should_refine(analysis)
            draft_plain = parse_text.strip()
            if force_refine and draft_plain:
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_plain_coerce_refine",
                        "status": "ok",
                        "meta": _pg(
                            {
                                "reason": coerce_reason,
                                "draft_chars": len(draft_plain),
                                "phase": "无 ACTION 纯文本 → 强制 Review / Polish",
                            },
                            "polishing",
                            f"策略：本轮无工具调用，因「{coerce_reason}」将正文视为草稿，强制进入审查与润色。",
                        ),
                    },
                }
                yield {
                    "event": "status",
                    "phase": "agent",
                    "message": "高复杂度或精化/强时效倾向：将本轮正文作为草稿进入审查与润色…",
                }
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_refine_answer",
                        "status": "running",
                        "meta": _pg(
                            {"coerced_from_plain_text": True, "coerce_reason": coerce_reason},
                            "polishing",
                            "后处理：对纯文本草稿执行审查与润色流水线…",
                        ),
                    },
                }
                extra_sc2 = await self._agent_self_check_block(prompt, draft_plain, hcfg, analysis, options, agent_model)
                if extra_sc2:
                    yield {
                        "event": "step",
                        "step": {
                            "name": "agent_self_check",
                            "status": "ok",
                            "meta": _pg(
                                {**_tag("review"), "chars": len(extra_sc2), "coerced_plain_text": True},
                                "polishing",
                                f"强制精化前自检完成（约 {len(extra_sc2)} 字），供审查层参考。",
                            ),
                        },
                    }
                yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
                refine_ok = True
                async for ev in self._refine_from_draft_stream(
                    prompt,
                    draft_plain,
                    options,
                    messages,
                    trace_id,
                    hcfg,
                    analysis,
                    meta_extra={
                        "agent_tool": "refine_answer",
                        "coerced_plain_text": True,
                        "coerce_reason": coerce_reason,
                    },
                    extra_review_context=extra_sc2,
                ):
                    yield ev
                    if ev.get("event") == "error":
                        refine_ok = False
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_refine_answer",
                        "status": "ok" if refine_ok else "error",
                        "meta": _pg(
                            {"coerced_plain_text": True},
                            "polishing",
                            "强制精化流水线已完成。" if refine_ok else "强制精化流水线失败。",
                        ),
                        "error": None if refine_ok else "Agent coerced refine failed",
                    },
                }
                return

            yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
            async for ev in self._emit_text_chunks(text, options):
                yield ev
            return

        # 迭代用尽：与 refine_answer 相同的全链路 Review + Polish（非 Fast 摘要）
        draft_fb = compile_agent_fallback_draft(conv, prompt)
        yield {
            "event": "step",
            "step": {
                "name": "agent_refine_fallback",
                "status": "running",
                "meta": _pg(
                    {"reason": "max_iterations_exhausted", "same_pipeline_as": "refine_answer"},
                    "polishing",
                    "迭代次数用尽：正将多轮对话摘录拼成草稿，走审查与润色兜底。",
                ),
            },
        }
        yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
        fb_ok = True
        async for ev in self._refine_from_draft_stream(
            prompt,
            draft_fb,
            options,
            messages,
            trace_id,
            hcfg,
            analysis,
            meta_extra={"agent_fallback": True, "reason": "max_iterations_exhausted"},
        ):
            yield ev
            if ev.get("event") == "error":
                fb_ok = False
        yield {
            "event": "step",
            "step": {
                "name": "agent_refine_fallback",
                "status": "ok" if fb_ok else "error",
                "meta": _pg(
                    {},
                    "polishing",
                    "Agent 兜底 Refine 已完成。" if fb_ok else "Agent 兜底 Refine 失败。",
                ),
            },
        }

    async def run_stream(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        options = options or {}
        options.setdefault("_history_signature", self._messages_signature(messages))
        options.setdefault("_documents_signature", self._documents_signature(options.get("documents")))
        if "_fast_cache_identity" not in options:
            options["_fast_cache_identity"] = str(options.get("search_prompt_base") or prompt or "").strip()
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []
        _tag = self._make_tagger()

        yield {"event": "trace", "trace_id": trace_id}
        yield {"event": "status", "phase": "analyze", "message": "正在分析问题…"}

        yield {
            "event": "step",
            "step": {
                "name": "complexity_analyze",
                "status": "running",
                "meta": _pg({}, "intake", "正在调用预判模型分析意图与复杂度…"),
            },
        }
        runtime = await self._resolve_runtime_context(prompt, mode, options)
        hcfg = runtime["hcfg"]
        mode = runtime["mode"]
        analysis = runtime["analysis"]
        intended_track = runtime["intended_track"]
        chosen_track = runtime["chosen_track"]
        should_search = runtime["should_search"]
        search_reason = runtime["search_reason"]
        search_mandatory = runtime["search_mandatory"]
        options["_runtime_track"] = chosen_track

        step_analyze = Step(
            name="complexity_analyze",
            status="ok",
            meta=_pg({**analysis, **_tag("intake")}, "intake", _analyze_step_summary(analysis)),
            input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
        )
        steps.append(step_analyze)
        yield {"event": "step", "step": step_analyze.to_dict()}

        yield {"event": "status", "phase": "route", "message": "正在选择处理轨道…"}
        step_track = Step(
            name="track_select",
            status="ok",
            meta=_pg(
                {
                    **_tag("routing"),
                    "mode": mode,
                    "track": chosen_track,
                    "task_type": analysis.get("task_type"),
                    "complexity": analysis.get("complexity"),
                    "decision": analysis.get("decision"),
                    "confidence": analysis.get("confidence"),
                    "search_required": analysis.get("search_required"),
                    "intended_track": intended_track,
                    "search_intent": analysis.get("search_intent"),
                    "output_intent": analysis.get("output_intent"),
                    "fallback_reason": analysis.get("fallback_reason"),
                    "high_risk_domain": analysis.get("high_risk_domain"),
                    "agent_disabled_fallback": bool(analysis.get("agent_disabled_fallback")),
                },
                "intake",
                _track_select_summary(chosen_track, intended_track, analysis),
            ),
        )
        steps.append(step_track)
        yield {"event": "step", "step": step_track.to_dict()}
        yield {"event": "trace", "trace_id": trace_id, "track": chosen_track}
        if chosen_track == "fast" and should_search:
            yield {"event": "status", "phase": "search", "message": "正在联网检索（快轨）…"}
            yield {
                "event": "step",
                "step": {
                    "name": "web_search",
                    "status": "running",
                    "meta": _pg({}, "search", "快轨入口：正在联网补充实时信息…"),
                },
            }
            prompt, step_ws, abort_err = await self._fast_entry_search_step(
                prompt, analysis, options, search_reason, search_mandatory=search_mandatory
            )
            steps.append(step_ws)
            yield {"event": "step", "step": step_ws.to_dict()}
            if abort_err:
                yield {"event": "error", "error": abort_err}
                return

        if chosen_track == "agent":
            yield {"event": "status", "phase": "agent", "message": "正在 Agent 推理与按需工具…"}
            async for ev in self._run_agent_stream(prompt, analysis, options, messages, trace_id, hcfg, _tag):
                yield ev
            return

        if chosen_track == "fast":
            prompt_for_answer = self._attach_documents_to_prompt(prompt, options)
            cached = await self._try_fast_cache_hit(options, prompt_for_answer)
            if cached:
                yield {
                    "event": "step",
                    "step": {
                        "name": "fast_answer_cache",
                        "status": "ok",
                        "meta": _pg(
                            {**_tag("cache"), "chars": len(cached)},
                            "fast",
                            "命中本地/Redis 答案缓存，跳过模型调用。",
                        ),
                    },
                }
                yield {
                    "event": "stream_start",
                    "track": "fast",
                    "trace_id": trace_id,
                    "meta": {"stream_phase": "answer"},
                }
                async for s_event in self._emit_text_chunks(cached, options):
                    yield s_event
                return
            yield {"event": "status", "phase": "draft", "message": "正在生成回答…"}
            route = self.route_fast_model(prompt, analysis)
            step_route = Step(
                name="fast_route",
                status="ok",
                meta=_pg(
                    {**route, **_tag("draft")},
                    "fast",
                    f"快轨已选定首选模型「{route.get('selected') or '—'}」，将单段流式生成。",
                ),
            )
            steps.append(step_route)
            yield {"event": "step", "step": step_route.to_dict()}

            candidates = route.get("candidates") or [route.get("selected")]
            yield {
                "event": "stream_start",
                "track": "fast",
                "trace_id": trace_id,
                "meta": {"stream_phase": "answer"},
            }
            buf: List[str] = []
            async for s_event in self._stream_with_fallback(candidates, prompt_for_answer, options, messages=messages):
                yield s_event
                if s_event.get("event") == "chunk":
                    buf.append(str((s_event.get("data") or {}).get("content") or ""))
            await self._store_fast_cache_answer(options, prompt_for_answer, "".join(buf))
            return

        # refine track
        refine_ctx = self._resolve_refine_context(analysis, hcfg)
        chain = refine_ctx["chain"]
        if not chain.get("enabled", True):
            route = self.route_fast_model(prompt, analysis)
            steps.append(
                Step(
                    name="refine_disabled_fallback_fast",
                    status="ok",
                    meta=_pg(
                        dict(route) if isinstance(route, dict) else {"route": route},
                        "fast",
                        "精化链已关闭，已改为快轨单段生成。",
                    ),
                )
            )
            candidates = route.get("candidates") or [route.get("selected")]

            yield {
                "event": "stream_start",
                "track": "fast",
                "trace_id": trace_id,
                "meta": {"stream_phase": "answer"},
            }
            async for s_event in self._stream_with_fallback(candidates, prompt, options, messages=messages):
                yield s_event
            return

        l1 = refine_ctx["l1"]
        l2 = refine_ctx["l2"]
        l3 = refine_ctx["l3"]
        default_model = refine_ctx["default_model"]
        refine_models = refine_ctx["refine_models"]
        history_chars = _int_budget(options, "history_context_chars", 4000, minimum=800, maximum=12000)
        review_search_chars = _int_budget(options, "review_search_context_chars", 6000, minimum=1500, maximum=12000)

        entry_block = ""
        si0 = str(analysis.get("search_intent") or "none").lower()
        if si0 in ("explicit", "required", "freshness_required"):
            yield {"event": "status", "phase": "search", "message": "正在为精化流程做入口轻量检索…"}
            step_re0 = Step(
                name="refine_entry_web_search",
                status="running",
                meta=_pg(
                    {"phase": "Refine 入口 · 轻量联网（Layer1 前）"},
                    "search",
                    "精化轨：入口轻量联网，为初稿补充实时摘要…",
                ),
            )
            yield {"event": "step", "step": step_re0.to_dict()}
            entry_block = await self._refine_entry_light_search(prompt, analysis, options)
            step_re1 = Step(
                name="refine_entry_web_search",
                status="ok",
                meta=_pg(
                    {
                        "phase": "Refine 入口 · 轻量联网（Layer1 前）",
                        "injected_chars": len(entry_block),
                    },
                    "search",
                    f"入口联网完成，已向初稿上下文注入约 {len(entry_block)} 字摘要。",
                ),
            )
            steps.append(step_re1)
            yield {"event": "step", "step": step_re1.to_dict()}

        skip_draft = self._should_skip_refine_draft(prompt, analysis, options)
        # Layer 1 — 流式输出草稿，让用户实时看到初稿内容
        yield {
            "event": "stream_start",
            "track": "refine",
            "trace_id": trace_id,
            "meta": {"stream_phase": "draft"},
        }
        yield {"event": "status", "phase": "draft", "message": "正在生成初稿…"}
        yield {
            "event": "step",
            "step": {
                "name": "refine_layer1_draft",
                "status": "running",
                "meta": _pg({"phase": "初稿层 · 生成草稿"}, "refine", "精化轨：正在生成初稿…"),
            },
        }
        l1_prompt = ""
        l1_candidates = refine_models.get("draft") or [default_model]
        l1_stream_meta: Dict[str, Any] = {"model": None, "provider": None, "latency_ms": 0}
        l1_stream_failed = False
        if skip_draft:
            r1_content = str(options.get("search_prompt_base") or prompt or "").strip()
            l1_stream_meta["model"] = "user_draft"
            l1_stream_meta["provider"] = "local"
        else:
            l1_prompt = self._build_refine_layer1_prompt(
                prompt,
                l1.get("instruction", ""),
                entry_block,
                messages,
                max_history_chars=history_chars,
                options=options,
            )
            l1_buf: List[str] = []
            async for s_event in self._stream_with_fallback(l1_candidates, l1_prompt, self._layer_opts(hcfg, "layer1", options), messages=None):
                if s_event.get("event") == "chunk":
                    chunk_content = str((s_event.get("data") or {}).get("content") or "")
                    if chunk_content:
                        l1_buf.append(chunk_content)
                        yield s_event
                elif s_event.get("event") == "model_start":
                    l1_stream_meta["model"] = s_event.get("model")
                    l1_stream_meta["provider"] = s_event.get("provider")
                    yield s_event
                elif s_event.get("event") == "model_end":
                    l1_stream_meta["latency_ms"] = s_event.get("latency_ms", 0)
                elif s_event.get("event") == "model_error":
                    yield s_event
                elif s_event.get("event") == "error":
                    l1_stream_failed = True
                    yield s_event
            r1_content = "".join(l1_buf).strip()
        step_l1 = Step(
            name="refine_layer1_draft",
            status="ok" if (not l1_stream_failed and r1_content) else "error",
            provider=l1_stream_meta["provider"],
            model=l1_stream_meta["model"],
            latency_ms=int(l1_stream_meta.get("latency_ms") or 0),
            input_preview=l1_prompt[:240] + ("…" if len(l1_prompt) > 240 else ""),
            output=r1_content if not l1_stream_failed else None,
            error=None if (not l1_stream_failed and r1_content) else "Layer 1 failed",
            meta=_pg(
                {**_tag("draft"), "candidates": l1_candidates, "skip_draft": skip_draft},
                "refine",
                "已直接将用户输入作为初稿，跳过 Layer1。"
                if skip_draft
                else f"初稿层完成（模型 {l1_stream_meta.get('model') or '—'}）。",
            ),
        )
        steps.append(step_l1)
        yield {"event": "step", "step": step_l1.to_dict()}

        if l1_stream_failed or not r1_content:
            if not l1_stream_failed:
                yield {"event": "error", "error": "Layer 1 failed."}
            return

        # 审查前重置前端内容，L3 润色后输出最终版本
        yield {"event": "content_reset"}

        # Layer 2（审查；若输出 <<ACTION: web_search("...")>> 则联网核查后重审，最多 3 轮）
        yield {"event": "status", "phase": "review", "message": "正在审查答案与必要时联网复核…"}
        yield {
            "event": "step",
            "step": {
                "name": "refine_layer2_review",
                "status": "running",
                "meta": _pg({"phase": "审查层 · 核对与必要时动作"}, "refine", "审查层：核对初稿，必要时触发联网核查…"),
            },
        }
        l2_prompt = self._build_refine_layer2_prompt(
            prompt,
            l2.get("instruction", ""),
            r1_content,
            messages,
            max_history_chars=history_chars,
            options=options,
        )

        l2_candidates = refine_models.get("review") or [default_model]
        polish_pool = refine_models.get("polish") or [default_model]
        r2, a2, l2_polish_recovered, l2_polish_tried = await self._refine_layer2_ask_with_polish_rescue(
            l2_candidates,
            l2_prompt,
            self._layer_opts(hcfg, "layer2", options),
            polish_pool,
            default_model,
        )
        if not r2.success:
            fallback_text = self._build_refine_layer2_fallback_text(r1_content, entry_block)
            step_l2 = Step(
                name="refine_layer2_review",
                status="error",
                provider=r2.provider,
                model=r2.model,
                latency_ms=r2.latency_ms,
                input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                error=r2.error,
                meta=_pg(
                    {
                        "attempts": a2,
                        "candidates": l2_candidates,
                        "polish_rescue_attempted": l2_polish_tried,
                        "polish_rescue_recovered": l2_polish_recovered,
                    },
                    "refine",
                    "审查层调用失败（含润色池补救未成功）。" if l2_polish_tried else "审查层调用失败。",
                ),
            )
            steps.append(step_l2)
            yield {"event": "step", "step": step_l2.to_dict()}
            yield {"event": "status", "phase": "fallback", "message": "审查层失败，已回退到初稿结果…"}
            yield {
                "event": "step",
                "step": Step(
                    name="refine_degrade_to_layer1",
                    status="ok",
                    output=fallback_text,
                    meta=_pg(
                        {
                            **_tag("review"),
                            "reason": "layer2_failed",
                            "has_entry_search_summary": bool(str(entry_block or "").strip()),
                        },
                        "refine",
                        "审查层失败，已回退到 Layer1 草稿；若已有入口联网摘要则一并保留。",
                    ),
                ).to_dict(),
            }
            async for s_event in self._emit_text_chunks(fallback_text, options):
                yield s_event
            return

        review_body = (r2.content or "").strip()
        if l2_polish_recovered:
            yield {
                "event": "step",
                "step": {
                    "name": "refine_layer2_polish_rescue",
                    "status": "ok",
                    "meta": _pg(
                        {"phase": "审查层 · 润色池补救", "model": r2.model},
                        "refine",
                        "审查模型池失败，已由润色模型池完成同任务补救。",
                    ),
                },
            }
        l2_layer_opts = self._layer_opts(hcfg, "layer2", options)
        search_loops = 0
        async for ev in self._iter_refine_review_web_rounds(
            review_body,
            l2_prompt,
            l2_candidates,
            l2_layer_opts,
            options,
            review_search_chars,
            "refine",
        ):
            if ev["kind"] == "round_start":
                search_loops = ev["loop"]
                q = ev["query"]
                yield {
                    "event": "status",
                    "phase": "search",
                    "message": f"正在联网检索（审查第 {search_loops} 轮）…",
                }
                yield {
                    "event": "step",
                    "step": {
                        "name": "review_web_search",
                        "status": "running",
                        "meta": _pg(
                            {"query": q, "review_round": search_loops, "phase": "审查内按需检索"},
                            "refine",
                            f"审查中联网：第 {search_loops} 轮，检索「{q[:60]}{'…' if len(q) > 60 else ''}」…",
                        ),
                    },
                }
            elif ev["kind"] == "after_search":
                search_loops = ev["loop"]
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
                                "query": q,
                                "review_round": search_loops,
                                "phase": "审查内按需检索",
                                "result_count": rc,
                                "sources": sr.get("sources") or [],
                            },
                            "refine",
                            (
                                f"审查检索完成：约 {rc} 条来源。"
                                if not sr.get("error")
                                else f"审查检索失败：{sr.get('error') or 'error'}"
                            ),
                        ),
                        "error": sr.get("error"),
                    },
                }
            elif ev["kind"] == "complete":
                review_body = ev["review_body"]
                search_loops = int(ev["search_loops"] or 0)

        step_l2 = Step(
            name="refine_layer2_review",
            status="ok",
            provider=r2.provider,
            model=r2.model,
            latency_ms=r2.latency_ms,
            input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
            output=_clean_review_body(review_body),
            meta=_pg(
                {
                    **_tag("review"),
                    "attempts": a2,
                    "candidates": l2_candidates,
                    "review_search_loops": search_loops,
                },
                "refine",
                f"审查层完成；其间联网核查 {search_loops} 轮。",
            ),
        )
        steps.append(step_l2)
        yield {"event": "step", "step": step_l2.to_dict()}

        # Layer 3 (Streaming the final output)
        yield {"event": "status", "phase": "polish", "message": "正在生成最终回复…"}
        yield {
            "event": "step",
            "step": {
                "name": "refine_layer3_polish",
                "status": "running",
                "meta": _pg({"phase": "润色层 · 流式成文"}, "refine", "润色层：按审查结论流式生成最终答复…"),
            },
        }
        l3_prompt = self._build_refine_layer3_prompt(
            prompt,
            l3.get("instruction", ""),
            review_body,
            options=options,
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        
        yield {
            "event": "stream_start",
            "track": "refine",
            "trace_id": trace_id,
            "meta": {"stream_phase": "polish"},
        }
        l3_ok = True
        async for s_event in self._stream_with_fallback(
            l3_candidates, l3_prompt, self._layer_opts(hcfg, "layer3", options), messages=None
        ):
            yield s_event
            if s_event.get("event") == "error":
                l3_ok = False

        step_l3 = Step(
            name="refine_layer3_polish",
            status="ok" if l3_ok else "error",
            meta=_pg(
                {**_tag("polish"), "candidates": l3_candidates},
                "refine",
                "润色层流式输出已完成。" if l3_ok else "润色层流式输出失败，请查看错误事件。",
            ),
            error=None if l3_ok else "Layer 3 stream failed",
        )
        yield {"event": "step", "step": step_l3.to_dict()}

    async def run(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        options = options or {}
        options.setdefault("_history_signature", self._messages_signature(messages))
        options.setdefault("_documents_signature", self._documents_signature(options.get("documents")))
        if "_fast_cache_identity" not in options:
            options["_fast_cache_identity"] = str(options.get("search_prompt_base") or prompt or "").strip()
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []
        _tag = self._make_tagger()
        runtime = await self._resolve_runtime_context(prompt, mode, options)
        hcfg = runtime["hcfg"]
        mode = runtime["mode"]
        analysis = runtime["analysis"]
        intended_track = runtime["intended_track"]
        chosen_track = runtime["chosen_track"]
        should_search = runtime["should_search"]
        search_reason = runtime["search_reason"]
        search_mandatory = runtime["search_mandatory"]

        steps.append(
            Step(
                name="complexity_analyze",
                status="ok",
                meta=_pg({**analysis, **_tag("intake")}, "intake", _analyze_step_summary(analysis)),
                input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
            )
        )

        if chosen_track == "agent":
            # 非流式接口暂不跑 Agent 循环，降级为 Refine 三阶段
            chosen_track = "refine"
            analysis = {
                **analysis,
                "sync_downgraded_from": "agent",
                "intended_track": "agent",
                "fallback_reason": analysis.get("fallback_reason") or "sync_api_no_agent_loop",
            }
        options["_runtime_track"] = chosen_track

        steps.append(
            Step(
                name="track_select",
                status="ok",
                meta=_pg(
                    {
                        **_tag("routing"),
                        "mode": mode,
                        "track": chosen_track,
                        "task_type": analysis.get("task_type"),
                        "confidence": analysis.get("confidence"),
                        "intended_track": intended_track,
                        "search_intent": analysis.get("search_intent"),
                        "output_intent": analysis.get("output_intent"),
                        "fallback_reason": analysis.get("fallback_reason"),
                        "high_risk_domain": analysis.get("high_risk_domain"),
                        "agent_disabled_fallback": bool(analysis.get("agent_disabled_fallback")),
                    },
                    "intake",
                    _track_select_summary(chosen_track, intended_track, analysis),
                ),
            )
        )

        if chosen_track == "fast" and should_search:
            prompt, ws_step, abort_err = await self._fast_entry_search_step(
                prompt, analysis, options, search_reason, search_mandatory=search_mandatory
            )
            steps.append(ws_step)
            if abort_err:
                fail = AskResult(
                    success=False,
                    content="",
                    provider=ws_step.provider or "web_search",
                    model="search",
                    latency_ms=int(ws_step.latency_ms or 0),
                    error=abort_err,
                )
                return {
                    "trace_id": trace_id,
                    "track": chosen_track,
                    "final": fail.to_dict(),
                    "steps": [s.to_dict() for s in steps],
                }

        if chosen_track == "fast":
            prompt_for_answer = self._attach_documents_to_prompt(prompt, options)
            cached = await self._try_fast_cache_hit(options, prompt_for_answer)
            if cached:
                steps.append(Step(name="fast_answer_cache", status="ok", meta={"chars": len(cached)}))
                return {
                    "trace_id": trace_id,
                    "track": "fast",
                    "final": AskResult(True, cached, "cache", "redis", 0).to_dict(),
                    "steps": [s.to_dict() for s in steps],
                }
            route = self.route_fast_model(prompt, analysis)
            steps.append(Step(name="fast_route", status="ok", meta=route))

            candidates = route.get("candidates") or [route.get("selected")]
            res, attempts = await self._ask_with_fallback(candidates, prompt_for_answer, options, messages=messages)
            steps.append(
                Step(
                    name="fast_ask",
                    status="ok" if res.success else "error",
                    provider=res.provider,
                    model=res.model,
                    latency_ms=res.latency_ms,
                    output=res.content if res.success else None,
                    error=res.error if not res.success else None,
                    meta={"attempts": attempts},
                )
            )
            if res.success:
                await self._store_fast_cache_answer(options, prompt_for_answer, res.content or "")
            return {
                "trace_id": trace_id,
                "track": "fast",
                "final": res.to_dict(),
                "steps": [s.to_dict() for s in steps],
            }

        # refine track
        refine_ctx = self._resolve_refine_context(analysis, hcfg)
        chain = refine_ctx["chain"]
        if not chain.get("enabled", True):
            # fallback to fast if disabled
            route = self.route_fast_model(prompt, analysis)
            steps.append(Step(name="refine_disabled_fallback_fast", status="ok", meta=route))
            candidates = route.get("candidates") or [route.get("selected")]
            res, attempts = await self._ask_with_fallback(candidates, prompt, options, messages=messages)
            steps.append(
                Step(
                    name="fast_ask",
                    status="ok" if res.success else "error",
                    provider=res.provider,
                    model=res.model,
                    latency_ms=res.latency_ms,
                    output=res.content if res.success else None,
                    error=res.error if not res.success else None,
                    meta={"attempts": attempts},
                )
            )
            return {
                "trace_id": trace_id,
                "track": "fast",
                "final": res.to_dict(),
                "steps": [s.to_dict() for s in steps],
            }

        l1 = refine_ctx["l1"]
        l2 = refine_ctx["l2"]
        l3 = refine_ctx["l3"]
        default_model = refine_ctx["default_model"]
        refine_models = refine_ctx["refine_models"]
        history_chars = _int_budget(options, "history_context_chars", 4000, minimum=800, maximum=12000)
        review_search_chars = _int_budget(options, "review_search_context_chars", 6000, minimum=1500, maximum=12000)

        entry_block = ""
        si_sync = str(analysis.get("search_intent") or "none").lower()
        if si_sync in ("explicit", "required", "freshness_required"):
            steps.append(
                Step(
                    name="refine_entry_web_search",
                    status="running",
                    meta={"phase": "Refine 入口 · 轻量联网（Layer1 前）"},
                )
            )
            entry_block = await self._refine_entry_light_search(prompt, analysis, options)
            steps.append(
                Step(
                    name="refine_entry_web_search",
                    status="ok",
                    meta={"phase": "Refine 入口 · 轻量联网（Layer1 前）", "injected_chars": len(entry_block)},
                )
            )

        skip_draft = self._should_skip_refine_draft(prompt, analysis, options)
        # Layer 1
        # 对于 refine 链的第一层，我们将历史消息注入，但要把 prompt 包装为 l1_prompt
        l1_candidates = refine_models.get("draft") or [default_model]
        if skip_draft:
            draft_text = str(options.get("search_prompt_base") or prompt or "").strip()
            r1 = AskResult(True, draft_text, "local", "user_draft", 0)
            a1: List[Dict[str, Any]] = []
            l1_prompt = draft_text
        else:
            l1_prompt = self._build_refine_layer1_prompt(
                prompt,
                l1.get("instruction", ""),
                entry_block,
                messages,
                max_history_chars=history_chars,
                options=options,
            )
            r1, a1 = await self._ask_with_fallback(
                l1_candidates, l1_prompt, self._layer_opts(hcfg, "layer1", options), messages=None
            )
        steps.append(
            Step(
                name="refine_layer1_draft",
                status="ok" if r1.success else "error",
                provider=r1.provider,
                model=r1.model,
                latency_ms=r1.latency_ms,
                input_preview=l1_prompt[:240] + ("…" if len(l1_prompt) > 240 else ""),
                output=r1.content if r1.success else None,
                error=r1.error if not r1.success else None,
                meta={**_tag("draft"), "attempts": a1, "candidates": l1_candidates, "skip_draft": skip_draft},
            )
        )
        if not r1.success:
            # degrade: return failure but show steps
            return {
                "trace_id": trace_id,
                "track": "refine",
                "final": r1.to_dict(),
                "steps": [s.to_dict() for s in steps],
            }

        # Layer 2（含可选联网核查，与流式接口逻辑对齐）
        l2_prompt = self._build_refine_layer2_prompt(
            prompt,
            l2.get("instruction", ""),
            r1.content or "",
            messages,
            max_history_chars=history_chars,
            options=options,
        )
        l2_candidates = refine_models.get("review") or [default_model]
        polish_pool = refine_models.get("polish") or [default_model]
        r2, a2, l2_polish_recovered, l2_polish_tried = await self._refine_layer2_ask_with_polish_rescue(
            l2_candidates,
            l2_prompt,
            self._layer_opts(hcfg, "layer2", options),
            polish_pool,
            default_model,
        )
        if not r2.success:
            fallback_text = self._build_refine_layer2_fallback_text(r1.content or "", entry_block)
            steps.append(
                Step(
                    name="refine_layer2_review",
                    status="error",
                    provider=r2.provider,
                    model=r2.model,
                    latency_ms=r2.latency_ms,
                    input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                    error=r2.error,
                    meta={
                        "attempts": a2,
                        "candidates": l2_candidates,
                        "polish_rescue_attempted": l2_polish_tried,
                        "polish_rescue_recovered": l2_polish_recovered,
                    },
                )
            )
            steps.append(
                Step(
                    name="refine_degrade_to_layer1",
                    status="ok",
                    meta={
                        **_tag("review"),
                        "reason": "layer2_failed",
                        "has_entry_search_summary": bool(str(entry_block or "").strip()),
                    },
                    output=fallback_text,
                )
            )
            return {
                "trace_id": trace_id,
                "track": "refine",
                "final": {
                    **r1.to_dict(),
                    "content": fallback_text,
                },
                "steps": [s.to_dict() for s in steps],
            }

        if l2_polish_recovered:
            steps.append(
                Step(
                    name="refine_layer2_polish_rescue",
                    status="ok",
                    meta=_pg(
                        {"phase": "审查层 · 润色池补救", "model": r2.model},
                        "refine",
                        "审查模型池失败，已由润色模型池完成同任务补救。",
                    ),
                )
            )

        review_body = (r2.content or "").strip()
        search_loops = 0
        l2_layer_opts = self._layer_opts(hcfg, "layer2", options)
        async for ev in self._iter_refine_review_web_rounds(
            review_body,
            l2_prompt,
            l2_candidates,
            l2_layer_opts,
            options,
            review_search_chars,
            "refine",
        ):
            if ev["kind"] == "after_search":
                q = ev["query"]
                sr = ev["sr"]
                steps.append(
                    Step(
                        name="review_web_search",
                        status="error" if sr.get("error") else "ok",
                        meta={"query": q, "sources": sr.get("sources") or []},
                        error=sr.get("error"),
                    )
                )
            elif ev["kind"] == "complete":
                review_body = ev["review_body"]
                search_loops = int(ev["search_loops"] or 0)

        steps.append(
            Step(
                name="refine_layer2_review",
                status="ok",
                provider=r2.provider,
                model=r2.model,
                latency_ms=r2.latency_ms,
                input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                output=_clean_review_body(review_body),
                meta={**_tag("review"), "attempts": a2, "candidates": l2_candidates, "review_search_loops": search_loops},
            )
        )

        # Layer 3
        l3_prompt = self._build_refine_layer3_prompt(
            prompt,
            l3.get("instruction", ""),
            review_body,
            options=options,
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        r3, a3 = await self._ask_with_fallback(
            l3_candidates, l3_prompt, self._layer_opts(hcfg, "layer3", options), messages=None
        )
        steps.append(
            Step(
                name="refine_layer3_polish",
                status="ok" if r3.success else "error",
                provider=r3.provider,
                model=r3.model,
                latency_ms=r3.latency_ms,
                input_preview=l3_prompt[:240] + ("…" if len(l3_prompt) > 240 else ""),
                output=r3.content if r3.success else None,
                error=r3.error if not r3.success else None,
                meta={**_tag("polish"), "attempts": a3, "candidates": l3_candidates},
            )
        )

        if not r3.success:
            # degrade: return layer2 as final
            steps.append(
                Step(
                    name="refine_degrade_to_layer2",
                    status="ok",
                    meta={"reason": "layer3_failed"},
                    output=r2.content,
                )
            )
            return {
                "trace_id": trace_id,
                "track": "refine",
                "final": r2.to_dict(),
                "steps": [s.to_dict() for s in steps],
            }

        return {
            "trace_id": trace_id,
            "track": "refine",
            "final": r3.to_dict(),
            "steps": [s.to_dict() for s in steps],
        }

