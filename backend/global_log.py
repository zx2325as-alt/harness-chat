from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional


def _log_path() -> str:
    # 默认写入 backend/runtime.log.jsonl；可通过环境变量覆盖
    return str(os.getenv("HARNESS_LOG_PATH") or "runtime.log.jsonl").strip() or "runtime.log.jsonl"


def _enabled() -> bool:
    v = str(os.getenv("HARNESS_LOG") or "1").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def _safe(obj: Any, *, limit: int = 6000) -> Any:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + "…"
    try:
        return json.loads(s)
    except Exception:
        return s


async def log_event(kind: str, data: Optional[Dict[str, Any]] = None) -> None:
    """
    全局运行时日志（JSONL）：用于“发生错误完全找不到原因”时定位。
    默认开启（HARNESS_LOG=1），写入 runtime.log.jsonl（HARNESS_LOG_PATH 可改）。
    """
    if not _enabled():
        return
    row: Dict[str, Any] = {"ts": time.time(), "kind": str(kind or "event")}
    if data:
        row.update(data)
    line = ""
    try:
        line = json.dumps(row, ensure_ascii=False, default=str)
    except Exception:
        line = json.dumps({"ts": time.time(), "kind": "log_json_encode_error", "raw": str(row)[:4000]}, ensure_ascii=False)

    path = _log_path()

    def _append() -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            return

    try:
        await asyncio.to_thread(_append)
    except Exception:
        return

