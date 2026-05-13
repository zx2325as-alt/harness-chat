"""Adaptive Self-Correcting Async DAG Runtime：编排入口 ``runtime.dag_stream.run_dag_runtime_stream``。"""
from __future__ import annotations

from runtime.capability_runtime import RuntimeHandle, enable, snapshot_capabilities

__all__ = ["__version__", "RuntimeHandle", "enable", "snapshot_capabilities"]

__version__ = "0.2.0"
