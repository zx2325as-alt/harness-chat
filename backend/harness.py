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
import difflib
from datetime import datetime, timedelta

from search_service import SearchService
from routing_signals import derive_user_signals, merge_signals_into_analysis, reasoning_keyword_boost
from search_query_util import soft_degrade_note, validate_search_query

from tools.layer import HarnessTools
from tools.parsing import RE_AGENT_REFINE, RE_AGENT_WS
from tools.refine_pipeline import compile_agent_fallback_draft, stream_refine_from_draft


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_preview": self.input_preview,
            "output": self.output,
            "error": self.error,
            "meta": self.meta or {},
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
        self._pipeline_seq = 0

    def _tag(self, phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._pipeline_seq += 1
        d: Dict[str, Any] = {"pipeline_phase": phase, "pipeline_sequence": self._pipeline_seq}
        if extra:
            d.update(extra)
        return d

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
        
        # force JSON output where possible (some models support response_format)
        llm_opts = dict(options or {})
        llm_opts["temperature"] = 0.0
        llm_opts["max_retries"] = int(hcfg.get("analyzer_max_retries", 1))
        llm_opts["request_timeout_s"] = float(hcfg.get("analyzer_request_timeout_s", 25))

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
                    complexity = data.get("complexity", "low").lower()
                    selected_model = data.get("selected_model", "")
                    fallback_models = data.get("fallback_models", [])
                    refine_models = data.get("refine_models", {})
                    reason = data.get("reason", "")
                    decision = str(data.get("decision", "") or "").strip().lower()
                    if decision not in ("fast", "refine"):
                        decision = "refine" if complexity == "high" else "fast"
                    task_type = str(data.get("task_type") or "").strip().lower()
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

                    return {
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
                    }
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
        length = len(prompt or "")
        return {
            "decision": "refine" if length > 200 else "fast",
            "reasons": ["analyzer_total_timeout"],
            "complexity": "high" if length > 200 else "low",
            "type": "general",
            "task_type": "generation" if length > 200 else "conversation",
            "reason": "复杂度预判超时，已按长度规则降级并继续处理",
            "analyzer_timed_out": True,
            "confidence": 0.45,
            "suggested_track": "refine" if length > 200 else "fast",
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
        """与联网摘要无关的稳定键：优先原始用户面（search_prompt_base），否则入口钉死的身份串。"""
        opts = options or {}
        return str(opts.get("_fast_cache_identity") or opts.get("search_prompt_base") or augmented_prompt or "").strip()

    async def _try_fast_cache_hit(self, options: Dict[str, Any], augmented_prompt: str) -> Optional[str]:
        fcfg = (self.cfg.get("harness") or {}).get("fast_answer_cache") or {}
        if not fcfg.get("enabled") or not self._redis:
            return None
        key_src = self._fast_cache_key_source(options, augmented_prompt)
        norm = self._norm_cache_prompt(key_src)
        pref = str(fcfg.get("key_prefix") or "harness:fast:v1:")
        thresh = float(fcfg.get("similarity_threshold", 0.9))
        max_scan = int(fcfg.get("max_scan_keys", 400))
        key = pref + hashlib.sha256(norm.encode("utf-8")).hexdigest()

        def _sync() -> Optional[str]:
            v = self._redis.get(key)
            if v:
                return str(v)
            raw = self._redis.lrange(pref + "lru", 0, -1)
            best_a: Optional[str] = None
            best_r = 0.0
            for i, row in enumerate(raw or []):
                if i >= max_scan:
                    break
                try:
                    o = json.loads(row)
                    n2 = str(o.get("n") or "")
                    r = difflib.SequenceMatcher(a=norm, b=n2).ratio()
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
        pref = str(fcfg.get("key_prefix") or "harness:fast:v1:")
        ttl = int(fcfg.get("ttl_sec", 864000))
        key = pref + hashlib.sha256(norm.encode("utf-8")).hexdigest()
        row = json.dumps({"n": norm, "a": answer}, ensure_ascii=False)

        def _sync() -> None:
            self._redis.set(key, answer, ex=ttl)
            self._redis.lpush(pref + "lru", row)
            self._redis.ltrim(pref + "lru", 0, 199)

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

        if extras:
            base = f"{base} " + " ".join(extras)
        base = base.strip()
        if str(analysis.get("search_intent") or "").lower() == "freshness_required":
            y = datetime.now().year
            for tok in (f"{y}年", "近期", "最新进展", "latest update"):
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
        for mk in candidates:
            if not mk:
                continue
            try:
                adapter = self.registry.get(mk)
                yield {"event": "model_start", "model": mk, "provider": adapter.provider}
                started_at = time.perf_counter()

                async for chunk in adapter.stream(prompt, options, messages=messages):
                    content = chunk.get("content") or ""
                    if content:
                        step = int(options.get("stream_slice_chars") or 8)
                        for idx in range(0, len(content), step):
                            yield {"event": "chunk", "data": {"content": content[idx : idx + step]}}
                            await asyncio.sleep(0)

                yield {
                    "event": "model_end",
                    "model": mk,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                }
                return
            except Exception as e:
                yield {"event": "model_error", "model": mk, "error": str(e)}
                continue

        yield {"event": "error", "error": "All fallback models failed in stream."}

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
        sr = await self.search.search(vq)
        if sr.get("error") or not (sr.get("sources") or []):
            return sr
        h = self.cfg.get("harness") or {}
        rcfg = (h.get("search") or {}).get("relevance_filter") or {}
        if not rcfg.get("enabled", False):
            return sr
        try:
            from search_relevance import filter_sources_by_relevance, rebuild_context_from_sources

            uq = str(options.get("search_prompt_base") or "")[:6000]
            mk = str(rcfg.get("model") or "gpt-5.5")
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
                    kept, datetime.now().isoformat(timespec="seconds")
                )
            sr["relevance_filter_meta"] = fmeta
        except Exception as e:
            sr["relevance_filter_meta"] = {"error": str(e)}
        return sr

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
        raw_q = self.build_search_query(prompt, analysis, options)
        vq, fc, vreason = validate_search_query(raw_q)
        if fc:
            meta = {
                "query": raw_q,
                "query_effective": None,
                "reason": search_reason,
                "failure_code": fc,
                "skipped": True,
                "validate_only": True,
            }
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
                    meta={**meta, "degraded": True, "note": "校验未通过，已跳过检索调用"},
                ),
                None,
            )

        sr = await self.perform_web_search(vq, options)
        search_context = sr.get("context", "")
        sources = sr.get("sources", [])
        hard_err = sr.get("error")
        meta = {
            "query": raw_q,
            "query_effective": vq,
            "reason": search_reason,
            "sources": sources,
            "result_count": len(sources),
            "failure_code": sr.get("failure_code"),
            "degraded": bool(sr.get("degraded")),
            "attempts": sr.get("attempts") or [],
            "fallback_from": sr.get("fallback_from"),
            "results_preview": search_context[:500] + ("..." if len(search_context) > 500 else ""),
        }
        if hard_err and search_mandatory:
            return (
                prompt,
                Step(
                    name="web_search",
                    status="error",
                    provider=sr.get("provider_used"),
                    latency_ms=sr.get("latency_ms"),
                    meta=meta,
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
                    meta={**meta, "degraded": True},
                ),
                None,
            )
        st = Step(
            name="web_search",
            status="ok",
            provider=sr.get("provider_used"),
            latency_ms=sr.get("latency_ms"),
            meta=meta,
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
        raw_q = self.build_search_query(prompt, analysis, options)
        vq, fc, vreason = validate_search_query(raw_q)
        if fc:
            return f"\n【入口联网】检索词未通过校验（{vreason or fc}），请依赖常识与后续审查层按需检索。\n"
        sr = await self.perform_web_search(vq, options)
        if sr.get("error"):
            return f"\n【入口联网】检索未成功：{sr.get('error')}。后续审查层仍可输出 <<ACTION: web_search(\"...\")>> 复核。\n"
        ctx = (sr.get("context") or "").strip()
        if not ctx:
            return "\n【入口联网】未获得有效摘要，请后续审查层按需检索。\n"
        return f"\n【入口联网摘要（供初稿参考）】\n{ctx[:6000]}\n"

    def _infer_task_type_from_json(self, data: Dict[str, Any]) -> str:
        """当模型未返回 task_type 时，由 complexity/type 推断。"""
        raw_type = str(data.get("type") or "").lower()
        if raw_type in ("math_logic", "code"):
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
            if ag_enabled and tt == "reasoning":
                return "agent"
            return "refine"

        if si in ("required", "freshness_required"):
            if ag_enabled and tt == "reasoning":
                return "agent"
            if tt == "generation" or cx in ("high", "medium"):
                return "refine"
            return "refine"

        if si == "explicit" and tt == "conversation" and cx == "low":
            dec = str(analysis.get("decision") or "fast").lower()
            if dec == "fast":
                return "refine"

        if ag_enabled and tt == "reasoning":
            return "agent"
        if tt == "reasoning" and not ag_enabled:
            analysis["fallback_reason"] = "agent_disabled_by_config"
            analysis["agent_disabled_fallback"] = True
            return "refine"

        if cx == "low" and tt == "conversation":
            return "fast"
        if tt == "generation":
            return "refine"
        dec = str(analysis.get("decision") or "fast").lower()
        return "refine" if dec == "refine" else "fast"

    async def _emit_text_chunks(self, text: str, options: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        step = max(1, int(options.get("stream_slice_chars") or 8))
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
        if str(analysis.get("complexity") or "").lower() != "high":
            return ""
        scfg = hcfg.get("complexity") or {}
        cm = self._pick_agent_self_check_model(hcfg, analysis, agent_model)
        sc_prompt = (
            "请只做自检清单，不要输出对用户最终答案。\n"
            "分两块：\n1) 我已核实或可依赖的事实\n2) 我仍不确定或需要更多证据的点\n\n"
            f"【用户问题】\n{orig_q[:4000]}\n【草稿】\n{draft[:12000]}"
        )
        opts_sc = {**options, "temperature": 0.1}
        rsc, _ = await self._ask_with_fallback([cm], sc_prompt, opts_sc, None)
        return (rsc.content or "").strip() if rsc and rsc.success else ""

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
    ) -> AsyncGenerator[Dict[str, Any], None]:
        acfg = hcfg.get("agent") or {}
        routing = hcfg.get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")
        agent_model = acfg.get("model") or analysis.get("selected_model") or default_model
        max_iter = max(1, int(acfg.get("max_iterations", 5)))
        agent_intro = (acfg.get("system_prompt") or "").strip()
        base_rules = (
            "你是具备工具调用能力的智能体，按「思考 → 行动 → 观察」循环推理。\n"
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
            for m in messages[-16:]:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                thread_msgs.append({"role": role, "content": _msg_content_to_text(m.get("content"))})
        conv: List[Dict[str, Any]] = [{"role": "system", "content": sys_content}] + thread_msgs + [{"role": "user", "content": prompt}]
        options.setdefault("_agent_loop_ctx", {"last_query": "", "last_ok": False})

        yield {
            "event": "step",
            "step": {
                "name": "agent_start",
                "status": "ok",
                "meta": {
                    "model": agent_model,
                    "max_iterations": max_iter,
                    "thread_turns": len(thread_msgs),
                    "phase": "初始化推理线程（system + 近期对话 + 当前问题）",
                },
            },
        }
        yield {"event": "status", "phase": "agent", "message": "正在分析问题并规划工具调用…"}

        for it in range(max_iter):
            yield {
                "event": "step",
                "step": {
                    "name": "agent_iteration",
                    "status": "running",
                    "meta": {
                        "i": it + 1,
                        "max": max_iter,
                        "phase": "调用主模型生成本轮策略与正文",
                        "model": agent_model,
                    },
                },
            }
            res, _att = await self._ask_with_fallback([agent_model], "", options, messages=conv)
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

            if RE_AGENT_REFINE.search(parse_text):
                next_move = "refine_answer"
            elif RE_AGENT_WS.search(parse_text):
                next_move = "web_search"
            else:
                next_move = "direct_reply"
            preview = text[:400] + ("…" if len(text) > 400 else "")
            yield {
                "event": "step",
                "step": {
                    "name": "agent_iteration",
                    "status": "ok",
                    "provider": res.provider,
                    "model": res.model,
                    "latency_ms": res.latency_ms,
                    "meta": {
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
                },
            }

            rm = RE_AGENT_REFINE.search(parse_text)
            if rm:
                orig_q = (rm.group(1) or "").strip() or prompt
                draft = (rm.group(2) or "").strip()
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
                            "meta": {"reason": "empty_or_tiny_draft", "draft_len": len(draft)},
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
                            "meta": {**self._tag("review"), "chars": len(extra_sc)},
                        },
                    }
                yield {"event": "step", "step": {"name": "agent_refine_answer", "status": "running", "meta": {}}}
                yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
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
                yield {"event": "step", "step": {"name": "agent_refine_answer", "status": "ok"}}
                return

            wm = RE_AGENT_WS.search(parse_text)
            if wm:
                query = wm.group(1).strip()
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
                            "meta": {"query": query, "failure_code": vfc},
                        },
                    }
                    continue
                yield {
                    "event": "step",
                    "step": {"name": "agent_web_search", "status": "running", "meta": {"query": vq}},
                }
                sr = await self.tools.web_search(vq, options)
                ctx = (sr.get("context") or "")[:12000]
                lo = options.get("_agent_loop_ctx") or {}
                st = Step(
                    name="agent_web_search",
                    status="error" if sr.get("error") else "ok",
                    meta={"query": query, "sources": sr.get("sources") or [], "from": "agent"},
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
                        "meta": {
                            "reason": coerce_reason,
                            "draft_chars": len(draft_plain),
                            "phase": "无 ACTION 纯文本 → 强制 Review / Polish",
                        },
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
                        "meta": {"coerced_from_plain_text": True, "coerce_reason": coerce_reason},
                    },
                }
                extra_sc2 = await self._agent_self_check_block(prompt, draft_plain, hcfg, analysis, options, agent_model)
                yield {"event": "stream_start", "track": "agent", "trace_id": trace_id}
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
                yield {
                    "event": "step",
                    "step": {
                        "name": "agent_refine_answer",
                        "status": "ok",
                        "meta": {"coerced_plain_text": True},
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
                "meta": {"reason": "max_iterations_exhausted", "same_pipeline_as": "refine_answer"},
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
        yield {"event": "step", "step": {"name": "agent_refine_fallback", "status": "ok" if fb_ok else "error"}}

    async def run_stream(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        options = options or {}
        if "_fast_cache_identity" not in options:
            options["_fast_cache_identity"] = str(options.get("search_prompt_base") or prompt or "").strip()
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []
        self._pipeline_seq = 0

        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine", "agent"):
            mode = "auto"

        yield {"event": "trace", "trace_id": trace_id}
        yield {"event": "status", "phase": "analyze", "message": "正在分析问题…"}

        yield {"event": "step", "step": {"name": "complexity_analyze", "status": "running"}}
        cx_cfg = hcfg.get("complexity") or {}
        analyzer_deadline = float(cx_cfg.get("analyzer_total_timeout_s", 45))
        try:
            analysis = await asyncio.wait_for(
                self.analyze_complexity(prompt, options),
                timeout=analyzer_deadline,
            )
        except asyncio.TimeoutError:
            analysis = self._analyzer_fallback_timeout(prompt)
        sig_base = str(options.get("search_prompt_base") or prompt or "").strip()
        analysis = self._postprocess_analysis(sig_base, analysis)
        signals = derive_user_signals(sig_base, options)
        analysis = merge_signals_into_analysis(analysis, signals, sig_base)

        step_analyze = Step(
            name="complexity_analyze",
            status="ok",
            meta={**analysis, **self._tag("intake")},
            input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
        )
        steps.append(step_analyze)
        yield {"event": "step", "step": step_analyze.to_dict()}

        yield {"event": "status", "phase": "route", "message": "正在选择处理轨道…"}
        intended_track = self._resolve_track(mode, analysis, options)
        chosen_track = self.apply_confidence_track_guard(mode, analysis, sig_base, intended_track)
        if options.get("upgrade_track") and mode == "auto":
            bump = {"fast": "refine", "refine": "agent"}
            nt = bump.get(chosen_track)
            if nt:
                ag_en = bool((hcfg.get("agent") or {}).get("enabled", True))
                if nt == "agent" and not ag_en:
                    nt = "refine"
                    analysis["fallback_reason"] = analysis.get("fallback_reason") or "upgrade_target_agent_disabled"
                analysis["client_track_upgrade"] = f"{chosen_track}->{nt}"
                chosen_track = nt

        step_track = Step(
            name="track_select",
            status="ok",
            meta={
                **self._tag("routing"),
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
            },
        )
        steps.append(step_track)
        yield {"event": "step", "step": step_track.to_dict()}
        yield {"event": "trace", "trace_id": trace_id, "track": chosen_track}

        should_search, search_reason = self.should_search(prompt, analysis, options)
        search_mandatory = self._search_mandatory(analysis, options)
        if chosen_track == "fast" and should_search:
            yield {"event": "status", "phase": "search", "message": "正在联网检索（快轨）…"}
            yield {"event": "step", "step": {"name": "web_search", "status": "running"}}
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
            async for ev in self._run_agent_stream(prompt, analysis, options, messages, trace_id, hcfg):
                yield ev
            return

        if chosen_track == "fast":
            cached = await self._try_fast_cache_hit(options, prompt)
            if cached:
                yield {
                    "event": "step",
                    "step": {
                        "name": "fast_answer_cache",
                        "status": "ok",
                        "meta": {**self._tag("cache"), "chars": len(cached)},
                    },
                }
                yield {"event": "stream_start", "track": "fast", "trace_id": trace_id}
                async for s_event in self._emit_text_chunks(cached, options):
                    yield s_event
                return
            yield {"event": "status", "phase": "draft", "message": "正在生成回答…"}
            route = self.route_fast_model(prompt, analysis)
            step_route = Step(
                name="fast_route",
                status="ok",
                meta={**route, **self._tag("draft")},
            )
            steps.append(step_route)
            yield {"event": "step", "step": step_route.to_dict()}

            candidates = route.get("candidates") or [route.get("selected")]
            yield {"event": "stream_start", "track": "fast", "trace_id": trace_id}
            buf: List[str] = []
            async for s_event in self._stream_with_fallback(candidates, prompt, options, messages=messages):
                yield s_event
                if s_event.get("event") == "chunk":
                    buf.append(str((s_event.get("data") or {}).get("content") or ""))
            await self._store_fast_cache_answer(options, prompt, "".join(buf))
            return

        # refine track
        chain = hcfg.get("refine_chain") or {}
        if not chain.get("enabled", True):
            route = self.route_fast_model(prompt, analysis)
            steps.append(Step(name="refine_disabled_fallback_fast", status="ok", meta=route))
            candidates = route.get("candidates") or [route.get("selected")]
            
            yield {"event": "stream_start", "track": "fast", "trace_id": trace_id}
            async for s_event in self._stream_with_fallback(candidates, prompt, options, messages=messages):
                yield s_event
            return

        l1 = chain.get("layer1") or {}
        l2 = chain.get("layer2") or {}
        l3 = chain.get("layer3") or {}
        
        routing = hcfg.get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")
        refine_models = analysis.get("refine_models") or {}

        entry_block = ""
        si0 = str(analysis.get("search_intent") or "none").lower()
        if si0 in ("explicit", "required", "freshness_required"):
            yield {"event": "status", "phase": "search", "message": "正在为精化流程做入口轻量检索…"}
            step_re0 = Step(
                name="refine_entry_web_search",
                status="running",
                meta={"phase": "Refine 入口 · 轻量联网（Layer1 前）"},
            )
            yield {"event": "step", "step": step_re0.to_dict()}
            entry_block = await self._refine_entry_light_search(prompt, analysis, options)
            step_re1 = Step(
                name="refine_entry_web_search",
                status="ok",
                meta={"phase": "Refine 入口 · 轻量联网（Layer1 前）", "injected_chars": len(entry_block)},
            )
            steps.append(step_re1)
            yield {"event": "step", "step": step_re1.to_dict()}

        # Layer 1 (Non-streaming for intermediate layers to keep things manageable)
        yield {"event": "status", "phase": "draft", "message": "正在生成初稿…"}
        yield {
            "event": "step",
            "step": {"name": "refine_layer1_draft", "status": "running", "meta": {"phase": "初稿层 · 生成草稿"}},
        }
        # For Layer 1 & 2 we do NOT pass the entire chat history. They should only focus on the *current* task context.
        # We append history manually into the prompt if needed, or simply let Layer 3 handle the conversational tone.
        # Here we choose to just pass the prompt directly to keep drafts focused.
        l1_prompt = f"{l1.get('instruction','').strip()}\n{entry_block}\n\n【原始问题】\n{prompt.strip()}\n"
        
        # If we have messages, we should probably prefix the recent history to give context, but NOT as role messages
        # because the layer instructions expect a specific prompt format.
        if messages:
            history_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages[-4:]])
            l1_prompt = f"【近期对话上下文参考】\n{history_text}\n\n" + l1_prompt

        l1_candidates = refine_models.get("draft") or [default_model]
        r1, a1 = await self._ask_with_fallback(
            l1_candidates, l1_prompt, self._layer_opts(hcfg, "layer1", options), messages=None
        )
        step_l1 = Step(
            name="refine_layer1_draft",
            status="ok" if r1.success else "error",
            provider=r1.provider,
            model=r1.model,
            latency_ms=r1.latency_ms,
            input_preview=l1_prompt[:240] + ("…" if len(l1_prompt) > 240 else ""),
            output=r1.content if r1.success else None,
            error=r1.error if not r1.success else None,
            meta={**self._tag("draft"), "attempts": a1, "candidates": l1_candidates},
        )
        steps.append(step_l1)
        yield {"event": "step", "step": step_l1.to_dict()}
        
        if not r1.success:
            yield {"event": "error", "error": "Layer 1 failed."}
            return

        # Layer 2（审查；若输出 <<ACTION: web_search("...")>> 则联网核查后重审，最多 3 轮）
        yield {"event": "status", "phase": "review", "message": "正在审查答案与必要时联网复核…"}
        yield {
            "event": "step",
            "step": {"name": "refine_layer2_review", "status": "running", "meta": {"phase": "审查层 · 核对与必要时动作"}},
        }
        l2_prompt = (
            f"{l2.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【初稿答案】\n{r1.content.strip()}\n"
            "\n如需核实实时数据，可在审查结论中单行输出：<<ACTION: web_search(\"查询词\")>>\n"
        )
        if messages:
            history_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages[-4:]])
            l2_prompt = f"【近期对话上下文参考】\n{history_text}\n\n" + l2_prompt

        l2_candidates = refine_models.get("review") or [default_model]
        r2, a2 = await self._ask_with_fallback(
            l2_candidates, l2_prompt, self._layer_opts(hcfg, "layer2", options), messages=None
        )
        if not r2.success:
            step_l2 = Step(
                name="refine_layer2_review",
                status="error",
                provider=r2.provider,
                model=r2.model,
                latency_ms=r2.latency_ms,
                input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                error=r2.error,
                meta={"attempts": a2, "candidates": l2_candidates},
            )
            steps.append(step_l2)
            yield {"event": "step", "step": step_l2.to_dict()}
            yield {"event": "error", "error": "Layer 2 failed."}
            return

        review_body = (r2.content or "").strip()
        extra_ctx = ""
        search_loops = 0
        for _ in range(3):
            wm = RE_AGENT_WS.search(review_body)
            if not wm:
                break
            search_loops += 1
            q = wm.group(1).strip()
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
                    "meta": {"query": q, "review_round": search_loops, "phase": "审查内按需检索"},
                },
            }
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
                sr = await self.perform_web_search(vq, options)
            snip = (sr.get("context") or "")[:8000]
            yield {
                "event": "step",
                "step": {
                    "name": "review_web_search",
                    "status": "error" if sr.get("error") else "ok",
                    "meta": {
                        "query": q,
                        "review_round": search_loops,
                        "phase": "审查内按需检索",
                        "result_count": len(sr.get("sources") or []),
                        "sources": sr.get("sources") or [],
                    },
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
                + "\n\n请结合上述联网信息更新审查结论；若仍需核实可再次输出 <<ACTION: web_search(\"查询词\")>>。"
            )
            r2b, _ = await self._ask_with_fallback(
                l2_candidates, retry_prompt, self._layer_opts(hcfg, "layer2", options), messages=None
            )
            if r2b.success:
                review_body = (r2b.content or "").strip()
            else:
                break

        step_l2 = Step(
            name="refine_layer2_review",
            status="ok",
            provider=r2.provider,
            model=r2.model,
            latency_ms=r2.latency_ms,
            input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
            output=review_body,
            meta={
                **self._tag("review"),
                "attempts": a2,
                "candidates": l2_candidates,
                "review_search_loops": search_loops,
            },
        )
        steps.append(step_l2)
        yield {"event": "step", "step": step_l2.to_dict()}

        # Layer 3 (Streaming the final output)
        yield {"event": "status", "phase": "polish", "message": "正在生成最终回复…"}
        yield {
            "event": "step",
            "step": {"name": "refine_layer3_polish", "status": "running", "meta": {"phase": "润色层 · 流式成文"}},
        }
        l3_prompt = (
            f"{l3.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【审查层答案】\n{review_body}\n"
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        
        yield {"event": "stream_start", "track": "refine", "trace_id": trace_id}
        async for s_event in self._stream_with_fallback(
            l3_candidates, l3_prompt, self._layer_opts(hcfg, "layer3", options), messages=messages
        ):
            yield s_event

        step_l3 = Step(
            name="refine_layer3_polish",
            status="ok",
            meta={**self._tag("polish"), "candidates": l3_candidates},
        )
        yield {"event": "step", "step": step_l3.to_dict()}

    async def run(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        options = options or {}
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []
        self._pipeline_seq = 0

        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine", "agent"):
            mode = "auto"

        cx_cfg = hcfg.get("complexity") or {}
        analyzer_deadline = float(cx_cfg.get("analyzer_total_timeout_s", 45))
        try:
            analysis = await asyncio.wait_for(
                self.analyze_complexity(prompt, options),
                timeout=analyzer_deadline,
            )
        except asyncio.TimeoutError:
            analysis = self._analyzer_fallback_timeout(prompt)
        sig_base = str(options.get("search_prompt_base") or prompt or "").strip()
        analysis = self._postprocess_analysis(sig_base, analysis)
        signals = derive_user_signals(sig_base, options)
        analysis = merge_signals_into_analysis(analysis, signals, sig_base)

        steps.append(
            Step(
                name="complexity_analyze",
                status="ok",
                meta={**analysis, **self._tag("intake")},
                input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
            )
        )

        intended_track = self._resolve_track(mode, analysis, options)
        chosen_track = self.apply_confidence_track_guard(mode, analysis, sig_base, intended_track)
        if options.get("upgrade_track") and mode == "auto":
            bump = {"fast": "refine", "refine": "agent"}
            nt = bump.get(chosen_track)
            if nt:
                ag_en = bool((hcfg.get("agent") or {}).get("enabled", True))
                if nt == "agent" and not ag_en:
                    nt = "refine"
                    analysis["fallback_reason"] = analysis.get("fallback_reason") or "upgrade_target_agent_disabled"
                analysis["client_track_upgrade"] = f"{chosen_track}->{nt}"
                chosen_track = nt
        if chosen_track == "agent":
            # 非流式接口暂不跑 Agent 循环，降级为 Refine 三阶段
            chosen_track = "refine"
            analysis = {
                **analysis,
                "sync_downgraded_from": "agent",
                "intended_track": "agent",
                "fallback_reason": analysis.get("fallback_reason") or "sync_api_no_agent_loop",
            }

        steps.append(
            Step(
                name="track_select",
                status="ok",
                meta={
                    **self._tag("routing"),
                    "mode": mode,
                    "track": chosen_track,
                    "task_type": analysis.get("task_type"),
                    "confidence": analysis.get("confidence"),
                    "intended_track": intended_track,
                    "search_intent": analysis.get("search_intent"),
                    "output_intent": analysis.get("output_intent"),
                    "fallback_reason": analysis.get("fallback_reason"),
                    "high_risk_domain": analysis.get("high_risk_domain"),
                },
            )
        )

        should_search, search_reason = self.should_search(prompt, analysis, options)
        search_mandatory = self._search_mandatory(analysis, options)
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
            route = self.route_fast_model(prompt, analysis)
            steps.append(Step(name="fast_route", status="ok", meta=route))

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

        # refine track
        chain = hcfg.get("refine_chain") or {}
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

        l1 = chain.get("layer1") or {}
        l2 = chain.get("layer2") or {}
        l3 = chain.get("layer3") or {}
        
        # fallback default
        routing = hcfg.get("routing") or {}
        default_model = routing.get("default_model", "gpt-5.5")

        refine_models = analysis.get("refine_models") or {}

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

        # Layer 1
        # 对于 refine 链的第一层，我们将历史消息注入，但要把 prompt 包装为 l1_prompt
        l1_prompt = f"{l1.get('instruction','').strip()}\n{entry_block}\n\n【原始问题】\n{prompt.strip()}\n"
        l1_candidates = refine_models.get("draft") or [default_model]
        r1, a1 = await self._ask_with_fallback(
            l1_candidates, l1_prompt, self._layer_opts(hcfg, "layer1", options), messages=messages
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
                meta={**self._tag("draft"), "attempts": a1, "candidates": l1_candidates},
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
        l2_prompt = (
            f"{l2.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【初稿答案】\n{r1.content.strip()}\n"
            "\n如需核实实时数据，可在审查结论中单行输出：<<ACTION: web_search(\"查询词\")>>\n"
        )
        l2_candidates = refine_models.get("review") or [default_model]
        r2, a2 = await self._ask_with_fallback(
            l2_candidates, l2_prompt, self._layer_opts(hcfg, "layer2", options), messages=messages
        )
        if not r2.success:
            steps.append(
                Step(
                    name="refine_layer2_review",
                    status="error",
                    provider=r2.provider,
                    model=r2.model,
                    latency_ms=r2.latency_ms,
                    input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                    error=r2.error,
                    meta={"attempts": a2, "candidates": l2_candidates},
                )
            )
            steps.append(
                Step(
                    name="refine_degrade_to_layer1",
                    status="ok",
                    meta={"reason": "layer2_failed"},
                    output=r1.content,
                )
            )
            return {
                "trace_id": trace_id,
                "track": "refine",
                "final": r1.to_dict(),
                "steps": [s.to_dict() for s in steps],
            }

        review_body = (r2.content or "").strip()
        extra_ctx = ""
        search_loops = 0
        for _ in range(3):
            wm = RE_AGENT_WS.search(review_body)
            if not wm:
                break
            search_loops += 1
            q = wm.group(1).strip()
            vq, vfc, vreason = validate_search_query(q)
            if vfc:
                sr = {
                    "context": "",
                    "sources": [],
                    "error": vreason or vfc,
                    "failure_code": vfc,
                }
            else:
                sr = await self.perform_web_search(vq, options)
            snip = (sr.get("context") or "")[:8000]
            steps.append(
                Step(
                    name="review_web_search",
                    status="error" if sr.get("error") else "ok",
                    meta={"query": q, "sources": sr.get("sources") or []},
                    error=sr.get("error"),
                )
            )
            if sr.get("error"):
                extra_ctx += f"\n\n【联网核查失败】{sr.get('error')}"
            else:
                extra_ctx += f"\n\n【联网核查补充】\n{snip}"
            retry_prompt = (
                l2_prompt
                + extra_ctx
                + "\n\n请结合上述联网信息更新审查结论；若仍需核实可再次输出 <<ACTION: web_search(\"查询词\")>>。"
            )
            r2b, _ = await self._ask_with_fallback(
                l2_candidates, retry_prompt, self._layer_opts(hcfg, "layer2", options), messages=messages
            )
            if r2b.success:
                review_body = (r2b.content or "").strip()
            else:
                break

        steps.append(
            Step(
                name="refine_layer2_review",
                status="ok",
                provider=r2.provider,
                model=r2.model,
                latency_ms=r2.latency_ms,
                input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                output=review_body,
                meta={**self._tag("review"), "attempts": a2, "candidates": l2_candidates, "review_search_loops": search_loops},
            )
        )

        # Layer 3
        l3_prompt = (
            f"{l3.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【审查层答案】\n{review_body}\n"
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        r3, a3 = await self._ask_with_fallback(
            l3_candidates, l3_prompt, self._layer_opts(hcfg, "layer3", options), messages=messages
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
                meta={**self._tag("polish"), "attempts": a3, "candidates": l3_candidates},
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

