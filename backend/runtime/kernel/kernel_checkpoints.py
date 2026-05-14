from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from runtime.kernel.kernel_models import CheckpointRef


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [_json_ready(v) for v in value]
    if isinstance(value, set):
        return [_json_ready(v) for v in sorted(value, key=lambda x: str(x))]
    return value


def snapshot_execution_state(ctx: Any) -> Dict[str, Any]:
    st = getattr(ctx, "st", None)
    if st and hasattr(st, "to_public_dict"):
        state = st.to_public_dict()
    elif st and hasattr(st, "__dict__"):
        state = _json_ready(st.__dict__)
    else:
        state = {}
    return {
        "execution_state": state,
        "draft": str(getattr(ctx, "draft", "") or "")[:20000],
        "phase": str(state.get("current_phase") or getattr(ctx, "options", {}).get("_runtime_phase") or "intake"),
        "should_stop": bool(getattr(ctx, "should_stop", False)),
    }


def restore_execution_state(ctx: Any, snapshot: Dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    state = snapshot.get("execution_state") if isinstance(snapshot.get("execution_state"), dict) else {}
    st = getattr(ctx, "st", None)
    if st and isinstance(state, dict):
        for key, value in state.items():
            if hasattr(st, key):
                try:
                    setattr(st, key, value)
                except Exception:
                    continue
    if snapshot.get("draft") is not None:
        ctx.draft = str(snapshot.get("draft") or "")
    if isinstance(getattr(ctx, "options", None), dict) and state.get("current_phase"):
        ctx.options["_runtime_phase"] = str(state.get("current_phase"))


def checkpoint_meta(ref: Optional[CheckpointRef]) -> Dict[str, Any]:
    return ref.to_dict() if isinstance(ref, CheckpointRef) else {}
