"""联网检索：优先 Tavily，可选 DuckDuckGo 兜底。"""
from __future__ import annotations

import asyncio
import time
import hashlib
import json
from typing import Any, Dict, List, Optional

import httpx

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class SearchService:
    def __init__(self, cfg: Dict[str, Any], redis_client: Any = None):
        self.cfg = cfg
        h = (cfg.get("harness") or {}).get("search") or {}
        self._search_cfg = h
        self._redis = redis_client
        self.provider = str(h.get("provider", "tavily")).strip().lower()
        self.fallback = str(h.get("fallback", "duckduckgo")).strip().lower()
        if self.fallback in ("", "false", "no", "off"):
            self.fallback = "none"
        self.search_depth = str(h.get("search_depth", "basic")).strip().lower()
        self.max_results = int(h.get("max_results", 8))
        self.topic = str(h.get("topic", "general")).strip().lower()
        self.timeout_s = float(h.get("timeout_s", 15))
        self.timeout_s_max = float(h.get("timeout_s_max", self.timeout_s))
        self.include_answer = bool(h.get("include_answer", False))

    def _api_key(self) -> Optional[str]:
        key = str(self._search_cfg.get("tavily_api_key") or "").strip()
        return key or None

    def _results_to_context_and_sources(
        self, results: List[Dict[str, Any]], provider: str
    ) -> tuple[str, List[Dict[str, Any]]]:
        context = "【联网搜索结果】\n"
        sources: List[Dict[str, Any]] = []
        for i, r in enumerate(results):
            title = r.get("title") or "未命名来源"
            body = r.get("snippet") or r.get("body") or r.get("content") or ""
            href = r.get("url") or r.get("href") or ""
            context += f"{i+1}. {title}\n{body}\n链接: {href}\n\n"
            sources.append(
                {
                    "index": i + 1,
                    "title": title,
                    "snippet": body[:2000] if isinstance(body, str) else str(body),
                    "url": href,
                    "provider": provider,
                }
            )
        return context, sources

    def _resolve_search_depth(self, override: Optional[str] = None) -> str:
        depth = str(override or self.search_depth or "basic").strip().lower()
        return depth if depth in ("basic", "advanced", "fast", "ultra-fast") else "basic"

    def _resolve_max_results(self, override: Optional[int] = None) -> int:
        try:
            value = int(override if override is not None else self.max_results)
        except (TypeError, ValueError):
            value = self.max_results
        return max(1, min(20, value))

    def _session_cache_key(self, session_id: str, query: str) -> str:
        norm = (query or "").strip().lower()
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        return f"harness:searchkb:{session_id}:{digest}"

    async def _tavily_search(
        self,
        query: str,
        *,
        override_search_depth: Optional[str] = None,
        override_max_results: Optional[int] = None,
    ) -> Dict[str, Any]:
        api_key = self._api_key()
        if not api_key:
            return {
                "ok": False,
                "failure_code": "TAVILY_AUTH",
                "error": "未配置 Tavily API Key：请在 config.yaml 的 harness.search.tavily_api_key 中填写",
                "sources": [],
                "context": "",
                "latency_ms": 0,
                "raw_status": None,
            }

        q = (query or "").strip()
        if len(q) > 400:
            q = q[:400]

        body: Dict[str, Any] = {
            "api_key": api_key,
            "query": q,
            "search_depth": self._resolve_search_depth(override_search_depth),
            "max_results": self._resolve_max_results(override_max_results),
            "topic": self.topic if self.topic in ("general", "news", "finance") else "general",
            "include_answer": self.include_answer,
        }

        t0 = time.perf_counter()
        # 单次 HTTP 等待上限：夹在 timeout_s 与 timeout_s_max 之间（timeout_s_max 用作硬上限）
        base_t = max(3.0, float(self.timeout_s))
        hard_t = max(3.0, float(self.timeout_s_max))
        http_timeout = min(hard_t, base_t)
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                r = await client.post(TAVILY_SEARCH_URL, json=body)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                if r.status_code in (401, 403):
                    return {
                        "ok": False,
                        "failure_code": "TAVILY_AUTH",
                        "error": f"Tavily 鉴权失败 HTTP {r.status_code}",
                        "sources": [],
                        "context": "",
                        "latency_ms": latency_ms,
                        "raw_status": r.status_code,
                    }
                if r.status_code == 429:
                    return {
                        "ok": False,
                        "failure_code": "TAVILY_RATE_LIMIT",
                        "error": "Tavily 请求过于频繁 (429)",
                        "sources": [],
                        "context": "",
                        "latency_ms": latency_ms,
                        "raw_status": r.status_code,
                    }
                if r.status_code >= 400:
                    return {
                        "ok": False,
                        "failure_code": "TAVILY_HTTP",
                        "error": f"Tavily HTTP {r.status_code}: {r.text[:300]}",
                        "sources": [],
                        "context": "",
                        "latency_ms": latency_ms,
                        "raw_status": r.status_code,
                    }
                data = r.json()
                raw_results = data.get("results") or []
                norm: List[Dict[str, Any]] = []
                for item in raw_results:
                    if not isinstance(item, dict):
                        continue
                    norm.append(
                        {
                            "title": item.get("title"),
                            "url": item.get("url"),
                            "snippet": item.get("content") or item.get("snippet") or "",
                        }
                    )
                if not norm:
                    return {
                        "ok": True,
                        "failure_code": "TAVILY_EMPTY",
                        "error": None,
                        "sources": [],
                        "context": "【联网搜索结果】\n未检索到条目（Tavily 返回空列表）。\n",
                        "latency_ms": latency_ms,
                        "degraded": True,
                        "provider": "tavily",
                        "raw_status": r.status_code,
                    }
                ctx, sources = self._results_to_context_and_sources(norm, "tavily")
                return {
                    "ok": True,
                    "failure_code": None,
                    "error": None,
                    "sources": sources,
                    "context": ctx,
                    "latency_ms": latency_ms,
                    "degraded": False,
                    "provider": "tavily",
                    "raw_status": r.status_code,
                }
        except httpx.TimeoutException:
            return {
                "ok": False,
                "failure_code": "TAVILY_TIMEOUT",
                "error": "Tavily 请求超时",
                "sources": [],
                "context": "",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "raw_status": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "failure_code": "TAVILY_ERROR",
                "error": str(e),
                "sources": [],
                "context": "",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "raw_status": None,
            }

    async def _duckduckgo_search(self, query: str, *, override_max_results: Optional[int] = None) -> Dict[str, Any]:
        from duckduckgo_search import DDGS

        t0 = time.perf_counter()

        def sync_search():
            with DDGS() as ddgs:
                return list(
                    ddgs.text(
                        query,
                        region="wt-wt",
                        safesearch="moderate",
                        timelimit=None,
                        max_results=max(1, min(15, self._resolve_max_results(override_max_results))),
                    )
                )

        try:
            raw = await asyncio.to_thread(sync_search)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            if not raw:
                return {
                    "ok": True,
                    "failure_code": "DDG_EMPTY",
                    "error": None,
                    "sources": [],
                    "context": "【联网搜索结果】\nDuckDuckGo 未返回条目。\n",
                    "latency_ms": latency_ms,
                    "degraded": True,
                    "provider": "duckduckgo",
                }
            norm = [
                {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body") or ""}
                for r in raw
                if isinstance(r, dict)
            ]
            ctx, sources = self._results_to_context_and_sources(norm, "duckduckgo")
            return {
                "ok": True,
                "failure_code": None,
                "error": None,
                "sources": sources,
                "context": ctx,
                "latency_ms": latency_ms,
                "degraded": True,
                "provider": "duckduckgo",
            }
        except Exception as e:
            return {
                "ok": False,
                "failure_code": "DDG_ERROR",
                "error": str(e),
                "sources": [],
                "context": "",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "provider": "duckduckgo",
            }

    async def search(
        self,
        query: str,
        *,
        override_max_results: Optional[int] = None,
        override_search_depth: Optional[str] = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        query = (query or "").strip()
        if not query:
            out0 = {
                "context": "",
                "sources": [],
                "error": "搜索关键词为空",
                "failure_code": "EMPTY_QUERY",
                "degraded": False,
                "provider_used": "none",
                "latency_ms": 0,
                "attempts": [],
            }
            if self._redis:
                asyncio.create_task(
                    self._log_search_stat(
                        {
                            "provider_used": "none",
                            "success": False,
                            "failure_code": "EMPTY_QUERY",
                            "latency_ms": 0,
                            "result_count": 0,
                            "query_len": 0,
                            "ts": time.time(),
                        }
                    )
                )
            return out0

        attempts: List[Dict[str, Any]] = []

        if self.provider == "duckduckgo":
            r = await self._duckduckgo_search(query, override_max_results=override_max_results)
            attempts.append(
                {
                    "provider": "duckduckgo",
                    "ok": r.get("ok"),
                    "failure_code": r.get("failure_code"),
                    "error": r.get("error"),
                    "latency_ms": r.get("latency_ms"),
                }
            )
            if not r.get("ok"):
                out1 = {
                    "context": r.get("context") or "",
                    "sources": [],
                    "error": r.get("error") or "DuckDuckGo 搜索失败",
                    "failure_code": r.get("failure_code") or "DDG_ERROR",
                    "degraded": False,
                    "provider_used": "duckduckgo",
                    "latency_ms": int(r.get("latency_ms") or 0),
                    "attempts": attempts,
                }
                if self._redis:
                    asyncio.create_task(
                        self._log_search_stat(
                            {
                                "provider_used": "duckduckgo",
                                "success": False,
                                "failure_code": out1.get("failure_code") or "",
                                "latency_ms": int((time.perf_counter() - t0) * 1000),
                                "result_count": 0,
                                "query_len": len(query),
                                "ts": time.time(),
                            }
                        )
                    )
                return out1
            out_ok = self._finalize_result(r, attempts)
            if self._redis:
                asyncio.create_task(
                    self._log_search_stat(
                        {
                            "provider_used": out_ok.get("provider_used") or "duckduckgo",
                            "success": True,
                            "failure_code": out_ok.get("failure_code") or "",
                            "latency_ms": int((time.perf_counter() - t0) * 1000),
                            "result_count": len(out_ok.get("sources") or []),
                            "query_len": len(query),
                            "degraded": bool(out_ok.get("degraded")),
                            "fallback_from": out_ok.get("fallback_from") or "",
                            "ts": time.time(),
                        }
                    )
                )
            return out_ok

        r = await self._tavily_search(
            query,
            override_search_depth=override_search_depth,
            override_max_results=override_max_results,
        )
        attempts.append(
            {
                "provider": "tavily",
                "ok": r.get("ok"),
                "failure_code": r.get("failure_code"),
                "error": r.get("error"),
                "latency_ms": r.get("latency_ms"),
            }
        )

        if r.get("ok"):
            out_ok2 = self._finalize_result(r, attempts)
            if self._redis:
                asyncio.create_task(
                    self._log_search_stat(
                        {
                            "provider_used": out_ok2.get("provider_used") or "tavily",
                            "success": True,
                            "failure_code": out_ok2.get("failure_code") or "",
                            "latency_ms": int((time.perf_counter() - t0) * 1000),
                            "result_count": len(out_ok2.get("sources") or []),
                            "query_len": len(query),
                            "degraded": bool(out_ok2.get("degraded")),
                            "fallback_from": out_ok2.get("fallback_from") or "",
                            "ts": time.time(),
                        }
                    )
                )
            return out_ok2

        if self.fallback == "duckduckgo":
            r2 = await self._duckduckgo_search(query, override_max_results=override_max_results)
            attempts.append(
                {
                    "provider": "duckduckgo",
                    "ok": r2.get("ok"),
                    "failure_code": r2.get("failure_code"),
                    "error": r2.get("error"),
                    "latency_ms": r2.get("latency_ms"),
                }
            )
            if r2.get("ok"):
                meta = dict(r2)
                meta["fallback_from"] = r.get("failure_code") or "tavily"
                out_ok3 = self._finalize_result(meta, attempts)
                if self._redis:
                    asyncio.create_task(
                        self._log_search_stat(
                            {
                                "provider_used": out_ok3.get("provider_used") or "duckduckgo",
                                "success": True,
                                "failure_code": out_ok3.get("failure_code") or "",
                                "latency_ms": int((time.perf_counter() - t0) * 1000),
                                "result_count": len(out_ok3.get("sources") or []),
                                "query_len": len(query),
                                "degraded": bool(out_ok3.get("degraded")),
                                "fallback_from": out_ok3.get("fallback_from") or "",
                                "ts": time.time(),
                            }
                        )
                    )
                return out_ok3

        out_fail = {
            "context": r.get("context") or "",
            "sources": r.get("sources") or [],
            "error": r.get("error") or "搜索失败",
            "failure_code": r.get("failure_code") or "SEARCH_FAILED",
            "degraded": False,
            "provider_used": "tavily",
            "latency_ms": r.get("latency_ms") or 0,
            "attempts": attempts,
        }
        if self._redis:
            asyncio.create_task(
                self._log_search_stat(
                    {
                        "provider_used": out_fail.get("provider_used") or "tavily",
                        "success": False,
                        "failure_code": out_fail.get("failure_code") or "",
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                        "result_count": len(out_fail.get("sources") or []),
                        "query_len": len(query),
                        "degraded": bool(out_fail.get("degraded")),
                        "fallback_from": out_fail.get("fallback_from") or "",
                        "ts": time.time(),
                    }
                )
            )
        return out_fail

    def _finalize_result(self, r: Dict[str, Any], attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
        provider = r.get("provider") or "unknown"
        return {
            "context": r.get("context") or "",
            "sources": r.get("sources") or [],
            "error": None,
            "failure_code": r.get("failure_code"),
            "degraded": bool(r.get("degraded")),
            "provider_used": provider,
            "latency_ms": int(r.get("latency_ms") or 0),
            "attempts": attempts,
            "fallback_from": r.get("fallback_from"),
        }

    async def _log_search_stat(self, stat: Dict[str, Any]) -> None:
        """
        写入 Redis Stream（不影响主路径）。
        仅记录统计字段，不记录 query/内容，避免泄漏与体积膨胀。
        """
        if not self._redis:
            return
        try:
            payload = {k: ("" if v is None else str(v)) for k, v in (stat or {}).items()}
        except Exception:
            return
        try:
            await asyncio.to_thread(
                self._redis.xadd,
                "harness:search:stats",
                payload,
                maxlen=5000,
                approximate=True,
            )
        except Exception:
            return
