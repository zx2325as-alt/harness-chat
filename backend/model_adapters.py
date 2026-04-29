from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from utils import Timer, env_get


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

    async def ask(self, prompt: str, options: Dict[str, Any]) -> AskResult:
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

    async def ask(self, prompt: str, options: Dict[str, Any]) -> AskResult:
        base_url = self.cfg.get("base_url", "https://api.openai.com/v1")
        url = _openai_chat_completions_url(str(base_url))
        model = self.cfg.get("model", self.model_name)
        api_key_env = self.cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key_optional = bool(self.cfg.get("api_key_optional", False))
        api_key = env_get(api_key_env)
        if not api_key and not api_key_optional:
            return AskResult(
                success=False,
                content="",
                provider="openai_compat",
                model=str(model),
                latency_ms=0,
                error=f"Missing API key env: {api_key_env}",
            )

        timeout_s = float(self.cfg.get("timeout_s", 60))
        t = Timer.start()
        headers: Dict[str, str] = {"content-type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        extra = self.cfg.get("extra_headers") or {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k and v is not None:
                    headers[str(k)] = str(v)

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                body: Dict[str, Any] = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
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
            return AskResult(
                success=False,
                content="",
                provider="openai_compat",
                model=str(model),
                latency_ms=t.elapsed_ms(),
                error=str(e),
            )


def build_adapter(model_key: str, model_cfg: Dict[str, Any]) -> BaseAdapter:
    provider = (model_cfg.get("provider") or "openai_compat").strip().lower()
    # 历史别名：一律按 OpenAI 兼容调用（需在 config 中配置正确 base_url）
    if provider in ("openai_compat", "openai", "ollama", "anthropic", "gemini"):
        return OpenAICompatAdapter(model_key, model_cfg)
    raise ValueError(
        f"Unknown provider {provider!r} for model {model_key}. "
        "请使用 openai_compat（或 openai），并通过 base_url 指向 OpenAI 兼容网关。"
    )
