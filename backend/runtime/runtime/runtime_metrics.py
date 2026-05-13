"""产品指标写入（后端根模块 runtime_metrics，此处为规格路径门面）。"""
from __future__ import annotations

from runtime_metrics import emit_product_metric, log_runtime_event

__all__ = ["emit_product_metric", "log_runtime_event"]
