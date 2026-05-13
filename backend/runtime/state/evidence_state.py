"""Evidence-Aware：结构化证据节点与轻量图（支持矛盾/新鲜度/源权重摘要）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from search_evidence import SearchEvidence


@dataclass
class EvidenceNode:
    source: str
    claim: str
    content_excerpt: str
    timestamp_iso: str
    trust_score: float
    freshness_score: float
    relevance_score: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceGraph:
    nodes: List[EvidenceNode] = field(default_factory=list)

    @classmethod
    def from_search_evidence(cls, items: List[SearchEvidence]) -> "EvidenceGraph":
        out: List[EvidenceNode] = []
        now = datetime.now(timezone.utc).isoformat()
        for e in items:
            body = (e.content or "").strip()
            claim = body[:240].split("\n", 1)[0].strip() if body else ""
            out.append(
                EvidenceNode(
                    source=str(e.source or "")[:500],
                    claim=claim or body[:120],
                    content_excerpt=body[:800],
                    timestamp_iso=now,
                    trust_score=float(e.trust_score),
                    freshness_score=float(e.freshness_score),
                    relevance_score=float(e.relevance_score),
                )
            )
        return cls(nodes=out)

    def to_dict(self) -> Dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self.nodes[:128]], "count": len(self.nodes)}

    def contradiction_hints(self) -> List[str]:
        """低信任 vs 高信任源并存时的极简矛盾信号。"""
        if len(self.nodes) < 2:
            return []
        lows = [n for n in self.nodes if n.trust_score < 0.35]
        highs = [n for n in self.nodes if n.trust_score > 0.75]
        if lows and highs:
            return [f"trust_gap:{lows[0].source}->{highs[0].source}"]
        return []

    def freshness_analysis(self) -> Dict[str, Any]:
        """新鲜度分布：均值 / 低新鲜度占比。"""
        if not self.nodes:
            return {"mean": 0.5, "low_share": 0.0, "n": 0}
        fs = [n.freshness_score for n in self.nodes]
        mean = sum(fs) / len(fs)
        low_share = sum(1 for x in fs if x < 0.35) / len(fs)
        return {"mean": round(mean, 4), "low_share": round(low_share, 4), "n": len(fs)}

    def source_weighting_summary(self) -> Dict[str, Any]:
        """源权重：按 trust * relevance 聚合 Top sources。"""
        scored: List[tuple[float, str]] = []
        for n in self.nodes:
            w = float(n.trust_score) * float(n.relevance_score)
            scored.append((w, n.source[:200]))
        scored.sort(key=lambda x: -x[0])
        top = [{"source": s, "weight": round(w, 4)} for w, s in scored[:8]]
        return {"top_sources": top}

    def apply_freshness_heuristic_from_year_tokens(self) -> None:
        """若摘录中含当年份标记则略抬高 freshness（启发式）。"""
        y = str(datetime.now(timezone.utc).year)
        for n in self.nodes:
            if y in n.content_excerpt or "最新" in n.content_excerpt:
                n.freshness_score = min(1.0, float(n.freshness_score) + 0.12)
