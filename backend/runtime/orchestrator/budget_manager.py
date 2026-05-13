from __future__ import annotations

from typing import Any, Dict, Optional

from runtime_state import need_search_allowed


class BudgetManager:
    """检索预算与 tokens/latency 占位（与 options 内预算字段对齐）。"""

    def __init__(self, options: Dict[str, Any]) -> None:
        self.options = options

    def search_ok(self) -> bool:
        return need_search_allowed(self.options)

    def note_llm_cost(self, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
        ctx = self.options.setdefault("_dag_cost_accum", {"tokens_in": 0, "tokens_out": 0})
        ctx["tokens_in"] = int(ctx.get("tokens_in") or 0) + int(tokens_in)
        ctx["tokens_out"] = int(ctx.get("tokens_out") or 0) + int(tokens_out)

    def latency_exceeded(self, intent_tier: str, elapsed_s: float) -> bool:
        cap = {"low": 12.0, "medium": 45.0, "high": 120.0}.get(intent_tier, 45.0)
        return elapsed_s > cap

    def token_budget_exceeded(self, intent_tier: str) -> bool:
        """Token Budget Runtime：累计 tokens_in/out 超阈则停止加深推理。"""
        caps = {"low": 12000, "medium": 48000, "high": 128000}
        cap = caps.get(intent_tier, 48000)
        ctx = self.options.get("_dag_cost_accum") or {}
        tot = int(ctx.get("tokens_in") or 0) + int(ctx.get("tokens_out") or 0)
        return tot > cap

    def cost_pause_parallel_draft(self, intent_tier: str, quality_tier: str) -> bool:
        """Cost-aware：低延迟且非高质量时不跑双并行稿。"""
        return intent_tier == "low" and quality_tier != "high"

    def prefer_cheaper_models(self, intent_tier: str, quality_tier: str) -> bool:
        """Cost-aware Runtime：与 parallel_draft 暂停策略对齐，起草阶段倾向单候选模型。"""
        return self.cost_pause_parallel_draft(intent_tier, quality_tier)
