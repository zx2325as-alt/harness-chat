"""
运行时配置叠加：在 config.yaml 之上合并 config.runtime.yaml，
并通过 API 持久化修改（不写回密钥字段）。
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, Set

import yaml

SECRET_KEY_PREFIXES: tuple[str, ...] = ("api_key", "authorization", "password")
SECRET_KEY_SUFFIXES: tuple[str, ...] = ("_api_key", "_secret", "_token")
SECRET_EXACT: Set[str] = {
    "api_key",
    "tavily_api_key",
    "extra_headers",  # 可能含鉴权头
}


def _is_secret_key(name: str) -> bool:
    k = str(name or "").strip()
    low = k.lower()
    if low in SECRET_EXACT:
        return True
    for p in SECRET_KEY_PREFIXES:
        if low.startswith(p):
            return True
    for s in SECRET_KEY_SUFFIXES:
        if low.endswith(s):
            return True
    return False


def deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """将 src 合并进 dst（就地修改 dst）。dict 递归，其它类型覆盖。"""
    for k, v in (src or {}).items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def strip_secrets_from_mapping(obj: Any) -> Any:
    """用于 API 响应：删除密钥类字段。"""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if _is_secret_key(str(k)):
                continue
            out[k] = strip_secrets_from_mapping(v)
        return out
    if isinstance(obj, list):
        return [strip_secrets_from_mapping(x) for x in obj]
    return obj


def sanitize_harness_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """写入前剔除客户端传入的密钥字段（防止误覆盖）。"""
    return strip_secrets_from_mapping(patch)  # type: ignore[return-value]


def load_yaml_if_exists(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def dump_yaml(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )


def apply_runtime_file_to_cfg(cfg: Dict[str, Any], runtime_path: str) -> None:
    rt = load_yaml_if_exists(runtime_path)
    if rt:
        deep_merge(cfg, rt)


def persist_harness_patch(runtime_path: str, harness_patch: Dict[str, Any]) -> None:
    """将本次 harness 增量合并进磁盘上的 runtime 文件。"""
    root = load_yaml_if_exists(runtime_path)
    deep_merge(root.setdefault("harness", {}), harness_patch)
    dump_yaml(runtime_path, root)


def redact_models_for_api(models_cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, mc in (models_cfg or {}).items():
        if not isinstance(mc, dict):
            continue
        pub = {k: v for k, v in mc.items() if not _is_secret_key(str(k))}
        out[str(key)] = pub
    return out
