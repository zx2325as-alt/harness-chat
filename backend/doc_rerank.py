from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

import httpx

from utils import env_get


def _safe_snip(text: str, limit: int = 900) -> str:
    t = str(text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:limit]


def _parse_pick(raw: str, n: int) -> List[int]:
    """
    接受两种输出：
    1) JSON: {"pick":[1,3,5]}
    2) 纯文本包含数字：1,3,5
    返回 0-based idx 列表（去重、保序、截断）。
    """
    s = (raw or "").strip()
    if not s:
        return []
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            obj = json.loads(m.group(0))
            pick = obj.get("pick")
            if isinstance(pick, list):
                out: List[int] = []
                seen = set()
                for x in pick:
                    try:
                        k = int(x)
                    except Exception:
                        continue
                    if 1 <= k <= n and k not in seen:
                        seen.add(k)
                        out.append(k - 1)
                return out
        except Exception:
            pass
    nums = []
    for tok in re.findall(r"\b\d+\b", s):
        try:
            nums.append(int(tok))
        except Exception:
            continue
    out2: List[int] = []
    seen2 = set()
    for k in nums:
        if 1 <= k <= n and k not in seen2:
            seen2.add(k)
            out2.append(k - 1)
    return out2


def _get_api_key_from_model_cfg(cfg: Dict[str, Any]) -> str:
    api_key = str(cfg.get("api_key") or "").strip()
    if api_key:
        return api_key
    api_key_env = str(cfg.get("api_key_env") or "").strip()
    if api_key_env and (api_key_env.startswith("sk-") or len(api_key_env) > 30):
        return api_key_env
    if api_key_env:
        return str(env_get(api_key_env) or "").strip()
    return ""


def _parse_n1n_rerank_results(data: Any, n_docs: int) -> List[float]:
    """
    兼容多种返回结构：
    - {"results":[{"index":0,"relevance_score":0.12}, ...]}
    - {"data":[{"index":0,"score":...}, ...]}
    - {"scores":[...]}
    - 或 choices.message.content 里包 JSON
    """
    if isinstance(data, dict):
        if isinstance(data.get("scores"), list):
            out = []
            for x in data["scores"][:n_docs]:
                try:
                    out.append(float(x))
                except Exception:
                    out.append(0.0)
            return out
        for key in ("results", "data"):
            rows = data.get(key)
            if isinstance(rows, list) and rows:
                scores = [0.0] * n_docs
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    idx = r.get("index")
                    sc = r.get("relevance_score", r.get("score", r.get("similarity")))
                    try:
                        ii = int(idx)
                        ff = float(sc)
                    except Exception:
                        continue
                    if 0 <= ii < n_docs:
                        scores[ii] = ff
                return scores
        # OpenAI-like wrapper
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = (msg.get("content") or "").strip()
            m = re.search(r"\{[\s\S]*\}", content)
            if m:
                try:
                    return _parse_n1n_rerank_results(json.loads(m.group(0)), n_docs)
                except Exception:
                    pass
    return []


async def _n1n_rerank_scores(
    model_cfg: Dict[str, Any],
    query: str,
    documents: List[str],
    *,
    timeout_s: float,
) -> List[float]:
    base_url = str(model_cfg.get("base_url") or "https://api.n1n.ai").rstrip("/")
    url = base_url + "/v1/rerank"
    api_key = _get_api_key_from_model_cfg(model_cfg)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": str(model_cfg.get("model") or ""),
        "query": query,
        "documents": documents,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
    return _parse_n1n_rerank_results(data, len(documents))


async def rerank_document_chunks(
    harness: Any,
    query: str,
    rows: List[Dict[str, Any]],
    *,
    model_key: str,
    max_items: int,
    top_k: int,
    options: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    rows: 每个元素至少包含 name/content/chunk/score
    返回 (重排后的 rows（最多 top_k）, meta)
    """
    meta: Dict[str, Any] = {"model": model_key, "max_items": max_items, "top_k": top_k, "picked": [], "raw": ""}
    q = str(query or "").strip()
    if not q or not rows:
        return rows[: max(1, top_k)], meta
    items = rows[: max(1, min(int(max_items or 12), len(rows)))]
    top_k = max(1, min(int(top_k or 8), len(items)))

    cfg = (harness.registry.models_cfg or {}).get(model_key) if getattr(harness, "registry", None) else None
    provider = str((cfg or {}).get("provider") or "openai_compat").strip().lower()
    if provider == "n1n_rerank":
        try:
            docs = [_safe_snip(r.get("content") or "", 1400) for r in items]
            timeout_s = float((cfg or {}).get("timeout_s", 25))
            scores = await _n1n_rerank_scores(cfg or {}, _safe_snip(q, 900), docs, timeout_s=timeout_s)
            if scores and len(scores) == len(docs):
                order = list(range(len(items)))
                order.sort(key=lambda i: float(scores[i] if i < len(scores) else 0.0), reverse=True)
                meta["picked"] = [i + 1 for i in order[:top_k]]
                meta["provider_used"] = "n1n_rerank"
                return [items[i] for i in order[:top_k]], meta
            meta["provider_used"] = "n1n_rerank"
            meta["skipped"] = True
        except Exception as e:
            meta["provider_used"] = "n1n_rerank"
            meta["error"] = str(e)
        # fallthrough to chat rerank

    lines = [
        "你是文档检索 reranker。请根据用户问题，从候选片段中挑选最能支撑回答的片段。",
        "只输出 JSON：{\"pick\":[...]}，pick 为 1-based 序号数组，长度不超过 top_k。",
        f"用户问题：{_safe_snip(q, 700)}",
        f"top_k: {top_k}",
        "",
    ]
    for i, r in enumerate(items, start=1):
        name = str(r.get("name") or "")
        chunk = r.get("chunk")
        label = f"{name}#{chunk}" if chunk is not None else name
        lines.append(f"[{i}] {label}: {_safe_snip(r.get('content') or '', 950)}")
    prompt = "\n".join(lines)

    adapter = harness.registry.get(model_key)
    opts = {**options, "temperature": 0.0, "request_timeout_s": min(25.0, float(options.get("request_timeout_s") or 25))}
    res = await adapter.ask(prompt, opts, messages=None)
    meta["raw"] = (res.content or "")[:1200]
    meta["provider_used"] = "chat_rerank"
    if not res.success:
        meta["error"] = res.error or "rerank_failed"
        return items[:top_k], meta
    picked = _parse_pick(res.content or "", len(items))
    if not picked:
        meta["skipped"] = True
        return items[:top_k], meta
    meta["picked"] = [p + 1 for p in picked[:top_k]]
    out = [items[i] for i in picked[:top_k] if 0 <= i < len(items)]
    if not out:
        return items[:top_k], meta
    return out, meta

