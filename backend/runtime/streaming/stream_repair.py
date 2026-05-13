"""Streaming Repair 占位：边生成边修的钩子（供后续接入增量校验）。"""
from __future__ import annotations

from typing import Any, Callable, Dict


def attach_stream_repair_hook(options: Dict[str, Any], hook: Callable[[str], Any]) -> None:
    opts = options.setdefault("_stream_repair_hooks", [])
    opts.append(hook)
