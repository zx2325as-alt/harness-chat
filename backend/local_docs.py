from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

from document_extract import SUPPORTED_DOCUMENT_EXTS, detect_document_ext, extract_document

# 与 app.py 保持一致
ALLOWED_EXTS = set(SUPPORTED_DOCUMENT_EXTS)
DEFAULT_MAX_FILES = 0
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10MB 单文件上限
DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100MB 总量上限


def _is_hidden(path: Path) -> bool:
    """跳过隐藏文件和目录（以 . 开头）。"""
    return any(part.startswith(".") for part in path.parts)


def scan_folder(
    folder_path: str,
    *,
    recursive: bool = True,
    allowed_exts: set[str] = ALLOWED_EXTS,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    skip_hidden: bool = True,
) -> List[Dict[str, Any]]:
    """
    扫描文件夹，返回 [(绝对路径, 文件名, 大小)] 列表。
    不做 IO 读取，只做文件发现与过滤。
    """
    root = Path(folder_path).resolve()
    if not root.exists():
        raise ValueError(f"路径不存在：{folder_path}")
    if not root.is_dir():
        raise ValueError(f"路径不是文件夹：{folder_path}")

    found: List[Dict[str, Any]] = []
    total_bytes = 0

    walker = root.rglob("*") if recursive else root.glob("*")
    for p in sorted(walker):
        if not p.is_file():
            continue
        rel_path = p.relative_to(root)
        if skip_hidden and _is_hidden(rel_path):
            continue
        ext = detect_document_ext(p.name)
        if ext not in allowed_exts:
            continue

        size = p.stat().st_size
        if size > max_file_bytes:
            continue  # 单文件超限，跳过
        if total_bytes + size > max_total_bytes:
            break  # 总量超限，停止

        total_bytes += size
        found.append(
            {
                "abs_path": str(p),
                "name": str(rel_path),  # 保留相对路径作为文件名，方便溯源
                "size": size,
                "ext": ext,
            }
        )
        if max_files > 0 and len(found) >= max_files:
            break

    return found


def _extract_one(file_info: Dict[str, Any]) -> Dict[str, Any]:
    """同步提取单个文件（在线程里运行）。"""
    abs_path = file_info["abs_path"]
    name = file_info["name"]
    try:
        data = Path(abs_path).read_bytes()
        doc = extract_document(name, data).to_dict()
    except Exception as exc:
        doc = {
            "name": name,
            "ext": file_info["ext"],
            "status": "error",
            "content": "",
            "chunks": [],
            "error": str(exc),
            "meta": {},
        }
    return doc


async def load_folder_documents(
    folder_path: str,
    *,
    recursive: bool = True,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    skip_hidden: bool = True,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """
    扫描并提取文件夹内所有支持的文档。
    返回与 /api/documents/parse 相同的结构。
    """
    file_list = scan_folder(
        folder_path,
        recursive=recursive,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        skip_hidden=skip_hidden,
    )

    if not file_list:
        return {
            "documents": [],
            "meta": {
                "folder": folder_path,
                "scanned": 0,
                "ok": 0,
                "error": 0,
                "skipped": 0,
            },
        }

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded_extract(fi: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            return await asyncio.to_thread(_extract_one, fi)

    docs = await asyncio.gather(*[_bounded_extract(fi) for fi in file_list])

    ok_count = sum(1 for d in docs if d.get("status") == "ok")
    err_count = sum(1 for d in docs if d.get("status") == "error")

    return {
        "documents": list(docs),
        "meta": {
            "folder": folder_path,
            "scanned": len(file_list),
            "ok": ok_count,
            "error": err_count,
            "skipped": 0,
            "total_bytes": sum(fi["size"] for fi in file_list),
        },
    }
