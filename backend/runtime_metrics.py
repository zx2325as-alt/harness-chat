"""轻量运行时指标（文档第十六章）：默认 SQLite 持久化；可选 JSONL 副本。

常用 metric（emit_product_metric）：fast_escalation_rate、critic_reject_rate、agent_stuck_rate、
search_sufficiency_fail、search_contradiction_rate、refine_success_rate、refine_quality_delta、
refine_improved_score、refine_regression_rate。
用户侧 thumb_down_rate / retry_rate / followup_rate / conversation_abandon_rate 由 POST /api/feedback 映射写入。
补充：fast_search_more（快轨 critic 要求补充检索）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from runtime_metrics_sqlite import append_runtime_row, resolve_metrics_sqlite_path


def _path(hcfg: Dict[str, Any]) -> Optional[str]:
    orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
    obs = orch.get("observability") if isinstance(orch.get("observability"), dict) else {}
    p = str(obs.get("metrics_jsonl_path") or "").strip()
    return p or None


def emit_product_metric(hcfg: Dict[str, Any], metric: str, **fields: Any) -> None:
    """文档第十六章：产品/路由质量指标（SQLite 必写；JSONL 可选）。"""
    log_runtime_event(hcfg, {"metric": metric, **fields})


def log_runtime_event(hcfg: Dict[str, Any], row: Dict[str, Any]) -> None:
    base = {"ts_ms": int(time.time() * 1000), **row}
    try:
        append_runtime_row(resolve_metrics_sqlite_path(hcfg), base)
    except Exception:
        # sqlite3.* / 权限 / 磁盘满等均不应阻断主请求，也不应阻止下方 JSONL
        pass
    path = _path(hcfg)
    if not path:
        return
    line = json.dumps(base, ensure_ascii=False)
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
