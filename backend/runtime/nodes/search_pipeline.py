"""Multi-stage Retrieval Runtime：rewrite → expansion → dedup → rerank → 并行查询前整形。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from search_evidence import SearchEvidence


def rewrite_query(q: str) -> str:
    q = str(q or "").strip()
    return q[:500]


def expand_queries(prompt: str, seeds: List[str], target_n: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for s in seeds:
        r = rewrite_query(s)
        k = r.lower()
        if r and k not in seen:
            seen.add(k)
            out.append(r)
    base = str(prompt or "").strip().replace("\n", " ")[:180]
    candidates = []
    if base:
        candidates.append(f"{base} 要点")
        candidates.append(f"{base} 最新")
        candidates.append(f"{base} 官方")
    for c in candidates:
        if len(out) >= target_n:
            break
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c[:400])
    return out[:target_n]


def dedup_queries(queries: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for q in queries:
        s = str(q or "").strip()
        k = s.lower()[:240]
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def rerank_search_evidence(items: List[SearchEvidence], *, top_k: int = 48) -> List[SearchEvidence]:
    """轻量 rerank：trust * relevance * (0.5+freshness)，无需外部 reranker API。"""
    if not items:
        return []

    def score(e: SearchEvidence) -> float:
        return float(e.trust_score) * float(e.relevance_score) * (0.5 + float(e.freshness_score))

    ranked = sorted(items, key=score, reverse=True)
    return ranked[:top_k]


def prepare_parallel_queries(
    prompt: str,
    analysis: Dict[str, Any],
    *,
    n: int,
    entry_search_required: bool,
    search_reason: str,
    seed_builder: Callable[..., List[str]],
) -> List[str]:
    """seed_builder：与 ``dag_common.build_search_queries`` 签名兼容的可调用对象。"""
    raw = seed_builder(
        prompt,
        analysis,
        n=max(n * 2, 4),
        entry_search_required=entry_search_required,
        search_reason=search_reason or "",
    )
    expanded = expand_queries(prompt, raw, target_n=max(n * 2, n))
    return dedup_queries(expanded)[: max(1, n)]
