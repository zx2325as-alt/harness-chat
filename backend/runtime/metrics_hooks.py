from __future__ import annotations

from typing import Any, Dict


def record_parallelism(options: Dict[str, Any], width: int) -> None:
    ctx = options.setdefault("_dag_parallelism", {"max_width": 1, "waves": 0})
    ctx["max_width"] = max(int(ctx.get("max_width") or 1), int(width))
    ctx["waves"] = int(ctx.get("waves") or 0) + 1
