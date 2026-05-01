from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, AsyncGenerator

from model_adapters import AskResult, build_adapter
from utils import Timer, new_trace_id


import json
import re
import asyncio
import time
from datetime import datetime, timedelta

from search_service import SearchService

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
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.registry = ModelRegistry(cfg.get("models", {}))
        self.search = SearchService(cfg)

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
            }

        analyzer_model = hcfg.get("analyzer_model", "gpt-5.5")
        base_prompt = hcfg.get("analyzer_prompt", "")
        
        full_prompt = f"{base_prompt}\n\n{prompt}"
        
        # force JSON output where possible (some models support response_format)
        llm_opts = dict(options or {})
        llm_opts["temperature"] = 0.0

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

                    return {
                        "decision": decision,
                        "reasons": [f"llm_reason: {reason}"],
                        "complexity": complexity,
                        "type": data.get("type", "general"),
                        "search_required": bool(data.get("search_required", False)),
                        "search_query": str(data.get("search_query") or ""),
                        "selected_model": selected_model,
                        "fallback_models": fallback_models,
                        "refine_models": refine_models,
                        "reason": reason,
                        "raw_llm_response": res.content,
                    }
                except json.JSONDecodeError:
                    return {
                        "decision": "fast",
                        "reasons": ["json_parse_error"],
                        "complexity": "low",
                        "type": "general",
                        "raw_llm_response": res.content,
                    }
        except Exception as e:
            return {
                "decision": "fast",
                "reasons": [f"llm_analyzer_error: {str(e)}"],
                "complexity": "low",
                "type": "general",
            }
            
        return {
            "decision": "fast",
            "reasons": ["analyzer_failed"],
            "complexity": "low",
            "type": "general",
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

    def should_search(self, prompt: str, analysis: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        opts = options or {}
        mode = str(opts.get("search_mode") or opts.get("search") or "auto").lower()
        if mode in ("off", "false", "0", "disabled"):
            return False, "手动关闭联网搜索"
        if mode in ("on", "true", "1", "force"):
            return True, "用户手动开启联网搜索"
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
        return base.strip()

    def _merge_search_into_prompt(self, prompt: str, search_context: str, options: Optional[Dict[str, Any]]) -> str:
        """合并联网摘要时强约束地点/主题与用户原话一致，减少『张冠李戴』式回答。"""
        opts = options or {}
        user_anchor = str(opts.get("search_prompt_base") or "").strip()
        anchor_block = (
            f"【用户原话】（回答中的城市/区县、日期、『明天』等时间含义必须与此一致，禁止擅自替换为其他城市）\n{user_anchor}\n\n"
            if user_anchor
            else ""
        )
        return (
            f"{search_context}\n"
            "【约束】只采纳与「用户原话」中地点、时间范围直接相关的检索内容；"
            "若摘要中的城市/站点与用户原话不符，不得将其当作用户所问地点的事实；"
            "此时应明确说明当前结果无法支持该地逐日预报，并建议用户核对检索词或使用当地气象台/中国天气网等权威来源。\n\n"
            f"{anchor_block}"
            "请在回答中用 [来源序号] 标注使用到的联网信息，并在末尾列出引用链接。\n\n"
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

    async def perform_web_search(self, query: str) -> Dict[str, Any]:
        return await self.search.search(query)

    async def run_stream(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        options = options or {}
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []

        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine"):
            mode = "auto"

        yield {"event": "trace", "trace_id": trace_id}

        yield {"event": "step", "step": {"name": "complexity_analyze", "status": "running"}}
        analysis = await self.analyze_complexity(prompt, options)
        step_analyze = Step(
            name="complexity_analyze",
            status="ok",
            meta=analysis,
            input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
        )
        steps.append(step_analyze)
        yield {"event": "step", "step": step_analyze.to_dict()}

        chosen_track = "fast"
        if mode == "fast":
            chosen_track = "fast"
        elif mode == "refine":
            chosen_track = "refine"
        else:
            chosen_track = analysis.get("decision", "fast")

        step_track = Step(name="track_select", status="ok", meta={"mode": mode, "track": chosen_track})
        steps.append(step_track)
        yield {"event": "step", "step": step_track.to_dict()}
        yield {"event": "trace", "trace_id": trace_id, "track": chosen_track}

        should_search, search_reason = self.should_search(prompt, analysis, options)
        if should_search:
            yield {"event": "step", "step": {"name": "web_search", "status": "running"}}
            search_query = self.build_search_query(prompt, analysis, options)
            search_result = await self.perform_web_search(search_query)
            search_context = search_result.get("context", "")
            sources = search_result.get("sources", [])
            hard_err = search_result.get("error")
            step_ws = Step(
                name="web_search",
                status="error" if hard_err else "ok",
                provider=search_result.get("provider_used"),
                latency_ms=search_result.get("latency_ms"),
                meta={
                    "query": search_query,
                    "query_effective": search_query,
                    "reason": search_reason,
                    "sources": sources,
                    "result_count": len(sources),
                    "failure_code": search_result.get("failure_code"),
                    "degraded": bool(search_result.get("degraded")),
                    "attempts": search_result.get("attempts") or [],
                    "fallback_from": search_result.get("fallback_from"),
                    "results_preview": search_context[:500] + ("..." if len(search_context) > 500 else ""),
                },
                error=hard_err,
            )
            steps.append(step_ws)
            yield {"event": "step", "step": step_ws.to_dict()}

            if step_ws.status == "error" or hard_err:
                yield {"event": "error", "error": step_ws.error or hard_err or "联网搜索失败"}
                return

            if search_context:
                prompt = self._merge_search_into_prompt(prompt, search_context, options)

        if chosen_track == "fast":
            route = self.route_fast_model(prompt, analysis)
            step_route = Step(name="fast_route", status="ok", meta=route)
            steps.append(step_route)
            yield {"event": "step", "step": step_route.to_dict()}

            candidates = route.get("candidates") or [route.get("selected")]
            
            # Start streaming the final answer
            yield {"event": "stream_start", "track": "fast", "trace_id": trace_id}
            
            async for s_event in self._stream_with_fallback(candidates, prompt, options, messages=messages):
                yield s_event
                
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

        # Layer 1 (Non-streaming for intermediate layers to keep things manageable)
        yield {"event": "step", "step": {"name": "refine_layer1_draft", "status": "running"}}
        # For Layer 1 & 2 we do NOT pass the entire chat history. They should only focus on the *current* task context.
        # We append history manually into the prompt if needed, or simply let Layer 3 handle the conversational tone.
        # Here we choose to just pass the prompt directly to keep drafts focused.
        l1_prompt = f"{l1.get('instruction','').strip()}\n\n【原始问题】\n{prompt.strip()}\n"
        
        # If we have messages, we should probably prefix the recent history to give context, but NOT as role messages
        # because the layer instructions expect a specific prompt format.
        if messages:
            history_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages[-4:]])
            l1_prompt = f"【近期对话上下文参考】\n{history_text}\n\n" + l1_prompt

        l1_candidates = refine_models.get("draft") or [default_model]
        r1, a1 = await self._ask_with_fallback(l1_candidates, l1_prompt, options, messages=None)
        step_l1 = Step(
            name="refine_layer1_draft",
            status="ok" if r1.success else "error",
            provider=r1.provider,
            model=r1.model,
            latency_ms=r1.latency_ms,
            input_preview=l1_prompt[:240] + ("…" if len(l1_prompt) > 240 else ""),
            output=r1.content if r1.success else None,
            error=r1.error if not r1.success else None,
            meta={"attempts": a1, "candidates": l1_candidates},
        )
        steps.append(step_l1)
        yield {"event": "step", "step": step_l1.to_dict()}
        
        if not r1.success:
            yield {"event": "error", "error": "Layer 1 failed."}
            return

        # Layer 2
        yield {"event": "step", "step": {"name": "refine_layer2_review", "status": "running"}}
        l2_prompt = (
            f"{l2.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【初稿答案】\n{r1.content.strip()}\n"
        )
        if messages:
            history_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages[-4:]])
            l2_prompt = f"【近期对话上下文参考】\n{history_text}\n\n" + l2_prompt

        l2_candidates = refine_models.get("review") or [default_model]
        r2, a2 = await self._ask_with_fallback(l2_candidates, l2_prompt, options, messages=None)
        step_l2 = Step(
            name="refine_layer2_review",
            status="ok" if r2.success else "error",
            provider=r2.provider,
            model=r2.model,
            latency_ms=r2.latency_ms,
            input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
            output=r2.content if r2.success else None,
            error=r2.error if not r2.success else None,
            meta={"attempts": a2, "candidates": l2_candidates},
        )
        steps.append(step_l2)
        yield {"event": "step", "step": step_l2.to_dict()}

        if not r2.success:
            yield {"event": "error", "error": "Layer 2 failed."}
            return

        # Layer 3 (Streaming the final output)
        yield {"event": "step", "step": {"name": "refine_layer3_polish", "status": "running"}}
        l3_prompt = (
            f"{l3.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【审查层答案】\n{r2.content.strip()}\n"
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        
        yield {"event": "stream_start", "track": "refine", "trace_id": trace_id}
        async for s_event in self._stream_with_fallback(l3_candidates, l3_prompt, options, messages=messages):
            yield s_event
            
        step_l3 = Step(
            name="refine_layer3_polish",
            status="ok",
            meta={"candidates": l3_candidates},
        )
        yield {"event": "step", "step": step_l3.to_dict()}

    async def run(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        options = options or {}
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []

        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine"):
            mode = "auto"

        analysis = await self.analyze_complexity(prompt, options)
        steps.append(
            Step(
                name="complexity_analyze",
                status="ok",
                meta=analysis,
                input_preview=(prompt[:240] + ("…" if len(prompt) > 240 else "")),
            )
        )

        chosen_track = "fast"
        if mode == "fast":
            chosen_track = "fast"
        elif mode == "refine":
            chosen_track = "refine"
        else:
            # auto
            chosen_track = analysis.get("decision", "fast")

        steps.append(Step(name="track_select", status="ok", meta={"mode": mode, "track": chosen_track}))

        should_search, search_reason = self.should_search(prompt, analysis, options)
        if should_search:
            search_query = self.build_search_query(prompt, analysis, options)
            search_result = await self.perform_web_search(search_query)
            search_context = search_result.get("context", "")
            hard_err = search_result.get("error")
            ws_step = Step(
                name="web_search",
                status="error" if hard_err else "ok",
                provider=search_result.get("provider_used"),
                latency_ms=search_result.get("latency_ms"),
                meta={
                    "query": search_query,
                    "query_effective": search_query,
                    "reason": search_reason,
                    "sources": search_result.get("sources", []),
                    "result_count": len(search_result.get("sources") or []),
                    "failure_code": search_result.get("failure_code"),
                    "degraded": bool(search_result.get("degraded")),
                    "attempts": search_result.get("attempts") or [],
                    "fallback_from": search_result.get("fallback_from"),
                    "results_preview": search_context[:500] + ("..." if len(search_context) > 500 else ""),
                },
                error=hard_err,
            )
            steps.append(ws_step)
            if ws_step.status == "error" or hard_err:
                fail = AskResult(
                    success=False,
                    content="",
                    provider=search_result.get("provider_used") or "web_search",
                    model="search",
                    latency_ms=int(search_result.get("latency_ms") or 0),
                    error=ws_step.error or str(hard_err or "联网搜索失败"),
                )
                return {
                    "trace_id": trace_id,
                    "track": chosen_track,
                    "final": fail.to_dict(),
                    "steps": [s.to_dict() for s in steps],
                }
            if search_context:
                prompt = self._merge_search_into_prompt(prompt, search_context, options)

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

        # Layer 1
        # 对于 refine 链的第一层，我们将历史消息注入，但要把 prompt 包装为 l1_prompt
        l1_prompt = f"{l1.get('instruction','').strip()}\n\n【原始问题】\n{prompt.strip()}\n"
        l1_candidates = refine_models.get("draft") or [default_model]
        r1, a1 = await self._ask_with_fallback(l1_candidates, l1_prompt, options, messages=messages)
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
                meta={"attempts": a1, "candidates": l1_candidates},
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

        # Layer 2
        l2_prompt = (
            f"{l2.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【初稿答案】\n{r1.content.strip()}\n"
        )
        l2_candidates = refine_models.get("review") or [default_model]
        r2, a2 = await self._ask_with_fallback(l2_candidates, l2_prompt, options, messages=messages)
        steps.append(
            Step(
                name="refine_layer2_review",
                status="ok" if r2.success else "error",
                provider=r2.provider,
                model=r2.model,
                latency_ms=r2.latency_ms,
                input_preview=l2_prompt[:240] + ("…" if len(l2_prompt) > 240 else ""),
                output=r2.content if r2.success else None,
                error=r2.error if not r2.success else None,
                meta={"attempts": a2, "candidates": l2_candidates},
            )
        )
        if not r2.success:
            # degrade: return layer1 as final
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

        # Layer 3
        l3_prompt = (
            f"{l3.get('instruction','').strip()}\n\n"
            f"【原始问题】\n{prompt.strip()}\n\n"
            f"【审查层答案】\n{r2.content.strip()}\n"
        )
        l3_candidates = refine_models.get("polish") or [default_model]
        r3, a3 = await self._ask_with_fallback(l3_candidates, l3_prompt, options, messages=messages)
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
                meta={"attempts": a3, "candidates": l3_candidates},
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

