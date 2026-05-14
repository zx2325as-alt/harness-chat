"""ExecutionState 真相源仍在 ``runtime_state``（避免全仓库迁移）；此处提供规格路径导入。"""
from __future__ import annotations

from runtime_state import ExecutionState, GoalExecutionState

__all__ = ["ExecutionState", "GoalExecutionState"]
