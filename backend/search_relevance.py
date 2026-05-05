"""检索结果与用户问题的轻量相关性过滤（极速模型）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def _parse_keep_flag(raw: str) -> bool:
    t = (raw or "").strip()
    m = re.search(r'"keep"\s*:\s*(true|false)', t, re.I)
    if m:
        return m.group(1).lower() == "true"
    if re.search(r"keep\s*[:=]\s*false", t, re.I):
        return False
    return True


async def filter_sources_by_relevance(
    harness: Any,
    user_question: str,
    sources: List[Dict[str, Any]],
    *,
    model_key: str,
    options: Dict[str, Any],
    max_items: int = 8,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    分批调用小模型判定 keep；失败则原样返回。
    返回 (过滤后 sources, meta)。
    """
    meta: Dict[str, Any] = {"model": model_key, "checked": 0, "dropped": 0, "skipped": False, "batches": 0}
    if not sources or not (user_question or "").strip():
        return sources, meta
    q = (user_question or "").strip()[:2000]
    opts = {**options, "temperature": 0.0, "request_timeout_s": min(20.0, float(options.get("request_timeout_s") or 20))}
    kept: List[Dict[str, Any]] = []
    adapter = harness.registry.get(model_key)
    batch_size = max(1, min(4, int(options.get("relevance_filter_batch_size") or 4)))
    pending: List[Tuple[Dict[str, Any], str]] = []

    async def _flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        prompt_lines = [
            "你是检索质检员。请根据用户问题，判断每条搜索摘要是否相关。",
            '仅输出 JSON 数组，例如：[{"idx":1,"keep":true,"why":"不超过20字"}]',
            f"用户问题：{q[:800]}",
            "",
        ]
        for idx, (_, snip) in enumerate(pending, start=1):
            prompt_lines.append(f"[{idx}] {snip}")
        prompt = "\n".join(prompt_lines)
        try:
            res = await adapter.ask(prompt, opts, messages=None)
            meta["batches"] += 1
            meta["checked"] += len(pending)
            flags = _parse_batch_keep_flags(res.content or "") if res.success else {}
            for idx, (src, _) in enumerate(pending, start=1):
                keep = flags.get(idx)
                if keep is False:
                    meta["dropped"] += 1
                else:
                    kept.append(src)
        except Exception:
            kept.extend(src for src, _ in pending)
        pending = []

    for src in sources[:max_items]:
        snip = str(src.get("snippet") or src.get("title") or "")[:1200]
        if not snip.strip():
            kept.append(src)
            continue
        pending.append((src, snip))
        if len(pending) >= batch_size:
            await _flush_pending()

    await _flush_pending()
    if not kept and sources:
        meta["skipped"] = True
        return sources[:3], meta
    return kept, meta


def _parse_batch_keep_flags(raw: str) -> Dict[int, bool]:
    text = (raw or "").strip()
    flags: Dict[int, bool] = {}
    for idx, keep in re.findall(r'"idx"\s*:\s*(\d+)[\s\S]*?"keep"\s*:\s*(true|false)', text, re.I):
        try:
            flags[int(idx)] = keep.lower() == "true"
        except (TypeError, ValueError):
            continue
    return flags


def rebuild_context_from_sources(sources: List[Dict[str, Any]], retrieved_at: str, max_total_chars: int = 6000) -> str:
    lines = [f"【联网搜索结果】（检索时间：{retrieved_at}）\n"]
    used = len(lines[0])
    for i, r in enumerate(sources):
        title = r.get("title") or "未命名"
        body = str(r.get("snippet") or "")
        href = r.get("url") or ""
        block = f"{i+1}. {title}\n{body}\n链接: {href}\n\n"
        remain = max_total_chars - used
        if remain <= 0:
            break
        clipped = block[:remain]
        lines.append(clipped)
        used += len(clipped)
        if len(clipped) < len(block):
            break
    return "".join(lines)
