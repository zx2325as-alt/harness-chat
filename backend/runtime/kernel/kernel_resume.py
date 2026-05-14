from __future__ import annotations

from typing import Any, Dict, Optional

from runtime.kernel.kernel_checkpoints import restore_execution_state


async def resume_kernel_context(ctx: Any, store: Any, run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    checkpoint = await store.load_latest_checkpoint(run_id)
    if not isinstance(checkpoint, dict):
        return None
    restore_execution_state(ctx, checkpoint.get("state") if isinstance(checkpoint.get("state"), dict) else {})
    return checkpoint
