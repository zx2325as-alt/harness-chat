from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, AsyncGenerator

import httpx
import asyncio

from utils import Timer, env_get


import json

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


class OpenAICompatAdapter(BaseAdapter):
    """统一：OpenAI Chat Completions 协议（Bearer + messages + model）。"""

    provider = "openai_compat"

    async def ask(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AskResult:
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
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    req_messages = [{"role": "system", "content": "请始终使用中文进行回答。"}]
                    
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
                    r = await client.post(url, headers=headers, json=body)
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
                    return AskResult(
                        success=True,
                        content=content,
                        provider="openai_compat",
                        model=str(model),
                        latency_ms=t.elapsed_ms(),
                        tokens_in=int(usage.get("prompt_tokens") or 0),
                        tokens_out=int(usage.get("completion_tokens") or 0),
                        raw={"id": data.get("id")},
                    )
            except Exception as e:
                last_error = str(e)
                # Retry on connection errors or server errors, but not on auth errors (except occasionally some proxies flake with 401)
                # We will log the error but still try to fail fast on 401 unless it's a known flaky API. 
                # To be safe, we will just break on 401/403/404 as before, but ensure the error message is clear.
                if "401" in last_error or "403" in last_error or "404" in last_error:
                    break
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                continue

        return AskResult(
            success=False,
            content="",
            provider="openai_compat",
            model=str(model),
            latency_ms=t.elapsed_ms(),
            error=last_error or "Unknown error",
        )

    async def stream(self, prompt: str, options: Dict[str, Any], messages: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[Dict[str, Any], None]:
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

        req_messages = [{"role": "system", "content": "请始终使用中文进行回答。"}]
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
            
        timeout_s = float(self.cfg.get("timeout_s", 60))
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                # We don't retry extensively on stream, just let it fail if it fails immediately
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    break
                                try:
                                    data = json.loads(data_str)
                                    delta = data.get("choices", [{}])[0].get("delta", {})
                                    
                                    # Standard text delta
                                    content = delta.get("content", "")
                                    
                                    # Handle Reasoning/Think models (like DeepSeek) that might return reasoning_content
                                    reasoning_content = delta.get("reasoning_content", "")
                                    
                                    if content or reasoning_content:
                                        yield {
                                            "content": content,
                                            "reasoning_content": reasoning_content
                                        }
                                except json.JSONDecodeError:
                                    pass
                # 如果成功执行完流，跳出重试循环
                break
            except Exception as e:
                last_error = str(e)
                if "401" in last_error or "403" in last_error or "404" in last_error:
                    # 对于明确的鉴权或路径错误，不重试，直接抛出
                    raise Exception(f"请求失败: {last_error}")
                
                # 针对 502/503/504 以及网络断开等临时错误，进行重试退避
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
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
