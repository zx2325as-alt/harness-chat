"""Runtime 指标 SQLite 落库（文档第十六章「数据库」硬性 ✓）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict

_lock = threading.Lock()


def resolve_metrics_sqlite_path(hcfg: Dict[str, Any]) -> str:
    orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
    obs = orch.get("observability") if isinstance(orch.get("observability"), dict) else {}
    raw = str(obs.get("metrics_sqlite_path") or "").strip()
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    if not raw:
        raw = os.path.join("data", "runtime_metrics.sqlite")
    if os.path.isabs(raw):
        return raw
    return os.path.normpath(os.path.join(backend_dir, raw))


def append_runtime_row(path: str, row: Dict[str, Any]) -> None:
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = json.dumps(row, ensure_ascii=False)
    ts = int(row.get("ts_ms") or 0)
    metric = str(row.get("metric") or "").strip()
    with _lock:
        conn = sqlite3.connect(path, timeout=30)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts_ms INTEGER NOT NULL,
                  metric TEXT,
                  payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_ts ON runtime_events(ts_ms)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_metric ON runtime_events(metric)"
            )
            conn.execute(
                "INSERT INTO runtime_events (ts_ms, metric, payload) VALUES (?, ?, ?)",
                (ts, metric or None, payload),
            )
            conn.commit()
        finally:
            conn.close()
