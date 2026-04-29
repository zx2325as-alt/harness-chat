from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, AsyncGenerator

from model_adapters import AskResult, build_adapter
from utils import Timer, new_trace_id


import json
import re
import asyncio
from duckduckgo_search import DDGS

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
                    
                    return {
                        "decision": "refine" if complexity == "high" else "fast",
                        "reasons": [f"llm_reason: {reason}"],
                        "complexity": complexity,
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
                # Yield a meta event so frontend knows which model is streaming
                yield {"event": "model_start", "model": mk, "provider": adapter.provider}
                
                async for chunk in adapter.stream(prompt, options, messages=messages):
                    yield {"event": "chunk", "data": chunk}
                    
                # If we finish successfully without exception, we break (don't fallback)
                yield {"event": "model_end", "model": mk}
                return
            except Exception as e:
                # On error, we yield an error event and try the next candidate
                yield {"event": "model_error", "model": mk, "error": str(e)}
                continue
                
        yield {"event": "error", "error": "All fallback models failed in stream."}

    async def perform_web_search(self, query: str) -> str:
        try:
            def search_sync():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=3))
            
            results = await asyncio.to_thread(search_sync)
            if not results:
                return "无搜索结果。"
            context = "【联网搜索结果】\n"
            for i, r in enumerate(results):
                context += f"{i+1}. {r.get('title')}\n{r.get('body')}\n链接: {r.get('href')}\n\n"
            return context
        except Exception as e:
            return f"联网搜索失败: {e}"

    async def run_stream(self, prompt: str, mode: str = "auto", options: Optional[Dict[str, Any]] = None, messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        options = options or {}
        trace_id = options.get("trace_id") or new_trace_id()
        steps: List[Step] = []

        hcfg = self.cfg.get("harness") or {}
        default_mode = hcfg.get("default_mode", "auto")
        mode = (mode or default_mode).lower()
        if mode not in ("auto", "fast", "refine"):
            mode = "auto"

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

        if analysis.get("type") == "web_search":
            yield {"event": "step", "step": {"name": "web_search", "status": "running"}}
            search_context = await self.perform_web_search(prompt)
            step_ws = Step(name="web_search", status="ok", meta={"query": prompt, "results": search_context[:200] + "..."})
            steps.append(step_ws)
            yield {"event": "step", "step": step_ws.to_dict()}
            
            prompt = f"{search_context}\n【用户问题】\n{prompt}"

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

