from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from runtime.orchestrator.dag import DAG

logger = logging.getLogger(__name__)


EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]


class DAGScheduler:
    """并行执行所有就绪节点；支持 gather、节点级超时、失败重试与部分失败标记。"""

    def __init__(self, dag: DAG, *, node_timeout_s: float = 120.0, max_retries: int = 1) -> None:
        self.dag = dag
        self.node_timeout_s = node_timeout_s
        self.max_retries = max(0, int(max_retries))

    async def run(
        self,
        ctx: Any,
        *,
        emit: Optional[EmitFn] = None,
        parallelism_metric: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.dag.reset_runtime()
        while True:
            pending = self.dag.incomplete_ids()
            if not pending:
                break
            ready = self.dag.ready_nodes()
            if not ready:
                if self.dag.failed:
                    logger.warning("DAG stalled with failures: %s", self.dag.failed)
                break

            ce = getattr(ctx, "cancel_event", None)
            if ce is not None and ce.is_set():
                logger.info("DAGScheduler: cancel_event set, stopping before wave")
                break

            if parallelism_metric:
                parallelism_metric(len(ready))

            async def _one(spec):
                if not spec.run:
                    self.dag.mark_done(spec.id)
                    return
                attempts = self.max_retries + 1
                for attempt in range(attempts):
                    try:
                        await asyncio.wait_for(spec.run(ctx), timeout=self.node_timeout_s)
                        self.dag.mark_done(spec.id)
                        return
                    except Exception as e:
                        if attempt + 1 < attempts:
                            logger.warning(
                                "DAG node %s retry %s/%s: %s",
                                spec.id,
                                attempt + 1,
                                attempts,
                                e,
                            )
                            continue
                        self.dag.mark_failed(spec.id, str(e))
                        if emit:
                            await emit(
                                {
                                    "event": "step",
                                    "step": {
                                        "name": f"dag_node_{spec.id}",
                                        "status": "error",
                                        "error": str(e)[:400],
                                    },
                                }
                            )

            await asyncio.gather(*[_one(s) for s in ready])
