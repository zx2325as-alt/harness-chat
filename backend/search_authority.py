"""检索结果权威域加权排序：提升政府、教育、国际组织等官方来源在上下文中的优先级。"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlparse

# 主机后缀权威基线 0~1（越高越倾向「官方 / 可核验」）
_SUFFIX_SCORE: tuple[tuple[str, float], ...] = (
    (".gov.cn", 1.0),
    (".gov.uk", 0.98),
    (".gov", 0.96),
    (".edu.cn", 0.92),
    (".edu", 0.90),
    (".ac.uk", 0.90),
    (".ac.cn", 0.88),
    (".org.cn", 0.72),
    (".org", 0.65),
    (".int", 0.85),
    (".mil", 0.82),
)

# 标题或域名中含此类关键词时略微加权（避免标题党抢排位）
_AUTHORITY_HINTS: tuple[tuple[str, float], ...] = (
    ("official", 0.06),
    ("government", 0.06),
    ("ministry", 0.08),
    ("国务院", 0.10),
    ("工信部", 0.08),
    ("国家统计局", 0.10),
    ("人大常委会", 0.08),
    ("who.int", 0.09),
    ("un.org", 0.08),
    ("ieee.org", 0.05),
    ("iso.org", 0.06),
    ("nist.gov", 0.07),
)


def _hostname(url: str) -> str:
    try:
        u = urlparse(url if "://" in url else "https://" + url)
        return (u.netloc or "").lower().split("@")[-1].split(":")[0]
    except Exception:
        return ""


def authority_score_for_source(src: Dict[str, Any], extra_hints: List[str]) -> float:
    """单条来源 0~1 权威分。"""
    url = str(src.get("url") or "")
    host = _hostname(url)
    title = str(src.get("title") or "")
    score = 0.35  # 默认：普通站点
    low = host
    for suf, sc in _SUFFIX_SCORE:
        if low.endswith(suf):
            score = max(score, sc)
            break
    blob = (host + " " + title).lower()
    for kw, bump in _AUTHORITY_HINTS:
        if kw.lower() in blob:
            score = min(1.0, score + bump)
    for pat in extra_hints:
        p = (pat or "").strip().lower()
        if p and p in blob:
            score = min(1.0, score + 0.04)
    return score


def _rebuild_context(sources: List[Dict[str, Any]]) -> str:
    ctx = "【联网搜索结果】\n"
    for i, r in enumerate(sources):
        title = r.get("title") or "未命名来源"
        body = r.get("snippet") or ""
        href = r.get("url") or ""
        ctx += f"{i+1}. {title}\n{body}\n链接: {href}\n\n"
    return ctx


def apply_authority_ranking(sr: Dict[str, Any], harness_search_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    按权威加权重新排序 sources，并重写 context 与 index。
    harness_search_cfg = cfg['harness']['search'] 片段。
    """
    acfg = (harness_search_cfg or {}).get("authority") or {}
    if not bool(acfg.get("enabled", True)):
        return sr
    try:
        blend = float(acfg.get("rank_blend", 0.42))
    except (TypeError, ValueError):
        blend = 0.42
    blend = max(0.0, min(1.0, blend))
    extra = list(acfg.get("extra_domain_hints") or [])
    if isinstance(extra, str):
        extra = [extra]

    sources: List[Dict[str, Any]] = [dict(x) for x in (sr.get("sources") or []) if isinstance(x, dict)]
    if len(sources) <= 1:
        return sr

    n = len(sources)
    ranked: List[tuple[float, int, Dict[str, Any]]] = []
    for i, s in enumerate(sources):
        auth = authority_score_for_source(s, extra)
        pos = 1.0 - (i / max(n - 1, 1)) if n > 1 else 1.0
        combined = (1.0 - blend) * pos + blend * auth
        s["authority_score"] = round(auth, 4)
        s["authority_rank_blend"] = round(combined, 4)
        ranked.append((combined, i, s))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    new_sources: List[Dict[str, Any]] = []
    for j, (_, _old_i, s) in enumerate(ranked):
        s["index"] = j + 1
        new_sources.append(s)

    out = dict(sr)
    out["sources"] = new_sources
    out["context"] = _rebuild_context(new_sources)
    meta = dict(out.get("authority_ranking_meta") or {})
    meta.update({"blend": blend, "reordered": True, "count": len(new_sources)})
    out["authority_ranking_meta"] = meta
    return out
