from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, AsyncGenerator, List

import httpx
import asyncio
import os
import uuid
import time

from semantic_utils import is_probably_english
from utils import Timer, env_get


import json

_SHARED_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0)
_shared_client: Optional[httpx.AsyncClient] = None


def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(limits=_SHARED_LIMITS)
    return _shared_client


def _trace_enabled() -> bool:
    v = str(os.getenv("MODEL_TRACE") or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


async def _trace_write(event: Dict[str, Any]) -> None:
    """
    最小回放日志：JSONL 追加写入，不记录 prompt/messages 正文，避免泄漏与体积爆炸。
    通过环境变量开启：
      MODEL_TRACE=1
      MODEL_TRACE_PATH=...（可选，默认 backend/model_trace.jsonl）
    """
    if not _trace_enabled():
        return
    try:
        path = str(os.getenv("MODEL_TRACE_PATH") or "model_trace.jsonl").strip() or "model_trace.jsonl"
        line = json.dumps({**(event or {}), "ts": time.time()}, ensure_ascii=False)
    except Exception:
        return

    def _append() -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            return

    try:
        await asyncio.to_thread(_append)
    except Exception:
        return

def _failure_kind_from_error(exc: BaseException, message: str) -> Optional[str]:
    """供上层回退链决策：限流不重试同一 key，尽快换模型。"""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        c = int(exc.response.status_code)
        if c == 429:
            return "rate_limit"
        if c in (401, 403):
            return "auth"
        if c == 404:
            return "bad_request"
        if c >= 500:
            return "server"
        return "http_error"
    low = (message or "").lower()
    if "429" in message or "too many requests" in low:
        return "rate_limit"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "401" in message or "403" in message:
        return "auth"
    if "404" in message:
        return "bad_request"
    return None


@dataclass
class AskResult:
    success: bool
    content: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None
    failure_kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "error": self.error,
            "raw": self.raw,
            "failure_kind": self.failure_kind,
        }


class BaseAdapter:
    provider: str

    def __init__(self, model_name: str, cfg: Dict[str, Any]):
        self.model_name = model_name
        self.cfg = cfg

    async def ask(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AskResult:
        raise NotImplementedError

    async def stream(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """流式输出接口。"""
        raise NotImplementedError


def _openai_chat_completions_url(base_url: str) -> str:
    """保证最终请求路径为 .../v1/chat/completions（OpenAI 兼容）。"""
    u = (base_url or "").strip().rstrip("/")
    if not u:
        u = "https://api.openai.com/v1"
    if not u.endswith("/v1"):
        u = u + "/v1"
    return u + "/chat/completions"


def _system_language_message(prompt: str, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, str]:
    probe = str(prompt or "").strip()
    if not probe and messages:
        for msg in reversed(messages):
            probe = str(msg.get("content") or "").strip()
            if probe:
                break
    if is_probably_english(probe):
        return {"role": "system", "content": "Please answer in the same language as the user, defaulting to English."}
    return {"role": "system", "content": "请使用与用户相同的语言回答；若用户主要使用中文，则默认用中文回答。"}


class OpenAICompatAdapter(BaseAdapter):
    """统一：OpenAI Chat Completions 协议（Bearer + messages + model）。"""

    provider = "openai_compat"

    async def ask(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AskResult:
        trace_id = uuid.uuid4().hex[:10]
        base_url = self.cfg.get("base_url", "https://api.openai.com/v1")
        url = _openai_chat_completions_url(str(base_url))
        model = self.cfg.get("model", self.model_name)
        # 先看是否显式配置了 api_key（推荐方式）
        api_key = self.cfg.get("api_key")
        
        if not api_key:
            api_key_env = self.cfg.get("api_key_env", "OPENAI_API_KEY")
            
            # If the user put the actual api key in api_key_env, just use it directly.
            # Note: the key could also start with 'sk-' or just be a direct token string.
            # But we don't want to accidentally expose system env vars. 
            # For our specific proxy `https://api.n1n.ai/v1`, keys might not start with `sk-` strictly.
            if api_key_env and (api_key_env.startswith("sk-") or len(api_key_env) > 30):
                api_key = api_key_env
            else:
                api_key = env_get(api_key_env)

        api_key_optional = bool(self.cfg.get("api_key_optional", False))
        
        if not api_key and not api_key_optional:
            return AskResult(
                success=False,
                content="",
                provider="openai_compat",
                model=str(model),
                latency_ms=0,
                error=f"Missing API key env: {api_key_env}",
                failure_kind="config",
            )

        timeout_s = float(options.get("request_timeout_s", self.cfg.get("timeout_s", 60)))
        t = Timer.start()
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "HarnessChat/1.0"
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        extra = self.cfg.get("extra_headers") or {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k and v is not None:
                    headers[str(k)] = str(v)

        max_retries = max(1, int(options.get("max_retries", self.cfg.get("max_retries", 3))))
        last_error = None
        last_failure_kind: Optional[str] = None
        await _trace_write(
            {
                "kind": "ask_request",
                "trace_id": trace_id,
                "model_key": self.model_name,
                "model": str(model),
                "base_url": str(base_url),
                "prompt_len": len(prompt or ""),
                "messages_len": len(messages or []),
                "temperature": options.get("temperature"),
                "request_timeout_s": timeout_s,
                "max_retries": max_retries,
            }
        )
        for attempt in range(max_retries):
            try:
                client = _get_shared_client()
                req_messages = [_system_language_message(prompt, messages)]
                
                if messages:
                    # Append the historical messages directly
                    req_messages.extend(messages)
                    
                # Always append the current prompt as the latest user message
                if prompt:
                    req_messages.append({"role": "user", "content": prompt})
                    
                body: Dict[str, Any] = {
                    "model": model,
                    "messages": req_messages,
                }
                mlow = str(model).lower()
                if not (mlow.startswith("o1") or mlow.startswith("o3")):
                    body["temperature"] = float(options.get("temperature", 0.2))
                r = await client.post(url, headers=headers, json=body, timeout=timeout_s)
                r.raise_for_status()
                data = r.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
                if isinstance(content, list):
                    # 少数兼容实现返回多段 content
                    parts: list[str] = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(str(block.get("text", "")))
                    content = "\n".join(parts)
                content = str(content).strip()
                usage = data.get("usage") or {}
                out = AskResult(
                    success=True,
                    content=content,
                    provider="openai_compat",
                    model=str(model),
                    latency_ms=t.elapsed_ms(),
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    raw={"id": data.get("id")},
                )
                await _trace_write(
                    {
                        "kind": "ask_response",
                        "trace_id": trace_id,
                        "success": True,
                        "latency_ms": out.latency_ms,
                        "tokens_in": out.tokens_in,
                        "tokens_out": out.tokens_out,
                        "content_len": len(out.content or ""),
                    }
                )
                return out
            except Exception as e:
                last_error = str(e)
                last_failure_kind = _failure_kind_from_error(e, last_error)
                if "401" in last_error or "403" in last_error or "404" in last_error:
                    break
                # 限流时不浪费内层重试次数，交给 harness 换候选模型
                if last_failure_kind == "rate_limit":
                    break
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                continue

        out_fail = AskResult(
            success=False,
            content="",
            provider="openai_compat",
            model=str(model),
            latency_ms=t.elapsed_ms(),
            error=last_error or "Unknown error",
            failure_kind=last_failure_kind,
        )
        await _trace_write(
            {
                "kind": "ask_response",
                "trace_id": trace_id,
                "success": False,
                "latency_ms": out_fail.latency_ms,
                "failure_kind": out_fail.failure_kind,
                "error": (out_fail.error or "")[:300],
            }
        )
        return out_fail

    async def stream(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        trace_id = uuid.uuid4().hex[:10]
        base_url = self.cfg.get("base_url", "https://api.openai.com/v1")
        url = _openai_chat_completions_url(str(base_url))
        model = self.cfg.get("model", self.model_name)
        api_key = self.cfg.get("api_key")
        
        if not api_key:
            api_key_env = self.cfg.get("api_key_env", "OPENAI_API_KEY")
            if api_key_env and (api_key_env.startswith("sk-") or len(api_key_env) > 30):
                api_key = api_key_env
            else:
                api_key = env_get(api_key_env)

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "HarnessChat/1.0"
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        extra = self.cfg.get("extra_headers") or {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k and v is not None:
                    headers[str(k)] = str(v)

        req_messages = [_system_language_message(prompt, messages)]
        if messages:
            req_messages.extend(messages)
            
        if prompt:
            req_messages.append({"role": "user", "content": prompt})
            
        body: Dict[str, Any] = {
            "model": model,
            "messages": req_messages,
            "stream": True
        }
        mlow = str(model).lower()
        if not (mlow.startswith("o1") or mlow.startswith("o3")):
            body["temperature"] = float(options.get("temperature", 0.2))
            
        timeout_s = float(options.get("request_timeout_s", self.cfg.get("timeout_s", 60)))
        max_retries = max(1, int(options.get("max_retries", self.cfg.get("max_retries", 3))))
        last_error = None
        await _trace_write(
            {
                "kind": "stream_request",
                "trace_id": trace_id,
                "model_key": self.model_name,
                "model": str(model),
                "base_url": str(base_url),
                "prompt_len": len(prompt or ""),
                "messages_len": len(messages or []),
                "temperature": options.get("temperature"),
                "request_timeout_s": timeout_s,
                "max_retries": max_retries,
            }
        )
        for attempt in range(max_retries):
            try:
                # We don't retry extensively on stream, just let it fail if it fails immediately
                client = _get_shared_client()
                async with client.stream("POST", url, headers=headers, json=body, timeout=timeout_s) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            await _trace_write(
                                {
                                    "kind": "stream_anomaly",
                                    "trace_id": trace_id,
                                    "type": "non_data_line",
                                    "sample": line[:120],
                                }
                            )
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})

                            # Standard text delta
                            content = delta.get("content", "")

                            # Handle Reasoning/Think models (like DeepSeek) that might return reasoning_content
                            reasoning_content = delta.get("reasoning_content", "")

                            if reasoning_content and not content:
                                await _trace_write(
                                    {
                                        "kind": "stream_anomaly",
                                        "trace_id": trace_id,
                                        "type": "reasoning_only_delta",
                                        "model": str(model),
                                    }
                                )

                            if content or reasoning_content:
                                yield {
                                    "content": content or "",
                                    "reasoning_content": reasoning_content or "",
                                }
                        except json.JSONDecodeError:
                            await _trace_write(
                                {
                                    "kind": "stream_anomaly",
                                    "trace_id": trace_id,
                                    "type": "json_decode_error",
                                    "sample": data_str[:120],
                                }
                            )
                # 如果成功执行完流，跳出重试循环
                break
            except Exception as e:
                last_error = str(e)
                fk = _failure_kind_from_error(e, last_error)
                if "401" in last_error or "403" in last_error or "404" in last_error:
                    await _trace_write(
                        {
                            "kind": "stream_response",
                            "trace_id": trace_id,
                            "success": False,
                            "failure_kind": fk,
                            "error": last_error[:300],
                        }
                    )
                    raise Exception(f"请求失败: {last_error}")
                if fk == "rate_limit":
                    await _trace_write(
                        {
                            "kind": "stream_response",
                            "trace_id": trace_id,
                            "success": False,
                            "failure_kind": fk,
                            "error": last_error[:300],
                        }
                    )
                    raise Exception(f"请求失败: {last_error}")

                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    await _trace_write(
                        {
                            "kind": "stream_response",
                            "trace_id": trace_id,
                            "success": False,
                            "failure_kind": fk,
                            "error": last_error[:300],
                        }
                    )
                    raise Exception(f"服务暂时不可用或网络异常，已重试 {max_retries} 次: {last_error}")


def build_adapter(model_key: str, model_cfg: Dict[str, Any]) -> BaseAdapter:
    provider = (model_cfg.get("provider") or "openai_compat").strip().lower()
    # 历史别名：一律按 OpenAI 兼容调用（需在 config 中配置正确 base_url）
    if provider in ("openai_compat", "openai", "ollama", "anthropic", "gemini"):
        return OpenAICompatAdapter(model_key, model_cfg)
    raise ValueError(
        f"Unknown provider {provider!r} for model {model_key}. "
        "请使用 openai_compat（或 openai），并通过 base_url 指向 OpenAI 兼容网关。"
    )
