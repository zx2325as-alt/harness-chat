from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List


MAX_TEXT_CHARS = 180_000
CHUNK_SIZE = 6_000
MAX_PDF_PAGES = 80
MAX_SHEET_ROWS = 2_000

TEXT_DOCUMENT_EXTS = {
    "txt",
    "md",
    "markdown",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "conf",
    "env",
    "log",
    "xml",
    "html",
    "htm",
    "css",
    "scss",
    "less",
    "js",
    "mjs",
    "cjs",
    "jsx",
    "ts",
    "tsx",
    "py",
    "java",
    "kt",
    "kts",
    "scala",
    "go",
    "rs",
    "c",
    "cc",
    "cpp",
    "cxx",
    "h",
    "hh",
    "hpp",
    "cs",
    "php",
    "rb",
    "swift",
    "sh",
    "bash",
    "zsh",
    "ps1",
    "sql",
    "r",
    "lua",
    "pl",
    "pm",
    "proto",
    "properties",
    "gradle",
    "vue",
    "svelte",
    "dart",
}
SPECIAL_TEXT_FILENAMES = {
    "dockerfile",
    "makefile",
    "jenkinsfile",
    ".env",
    ".gitignore",
    ".npmrc",
    ".yarnrc",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
    "cmakelists.txt",
}
SUPPORTED_DOCUMENT_EXTS = TEXT_DOCUMENT_EXTS | {
    "csv",
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
} | SPECIAL_TEXT_FILENAMES


@dataclass
class ExtractedDocument:
    name: str
    ext: str
    status: str
    content: str
    chunks: List[Dict[str, Any]]
    error: str | None = None
    meta: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["meta"] = data["meta"] or {}
        return data


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    for idx, start in enumerate(range(0, len(text), chunk_size), start=1):
        part = text[start : start + chunk_size]
        chunks.append({"index": idx, "content": part, "start": start, "end": start + len(part)})
    return chunks


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def detect_document_ext(filename: str) -> str:
    name = os.path.basename(filename or "").strip()
    lower_name = name.lower()
    if lower_name in SPECIAL_TEXT_FILENAMES:
        return lower_name
    return os.path.splitext(lower_name)[1].lower().lstrip(".")


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("缺少 pypdf 依赖，无法解析 PDF。") from exc

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page_no, page in enumerate(reader.pages, start=1):
        if page_no > MAX_PDF_PAGES:
            pages.append(f"【已截断：仅解析前 {MAX_PDF_PAGES} 页】")
            break
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"【第 {page_no} 页】\n{text}")
    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except Exception as exc:
        raise RuntimeError("缺少 python-docx 依赖，无法解析 DOCX。") from exc

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table_idx, table in enumerate(document.tables, start=1):
        rows = []
        for row in table.rows:
            rows.append(" | ".join(cell.text.strip() for cell in row.cells))
        if rows:
            parts.append(f"【表格 {table_idx}】\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        raise RuntimeError("缺少 openpyxl 依赖，无法解析 XLSX。") from exc

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        lines = [f"【工作表：{ws.title}】"]
        for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if row_idx > MAX_SHEET_ROWS:
                lines.append(f"【已截断：仅解析前 {MAX_SHEET_ROWS} 行】")
                break
            values = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in values):
                lines.append(" | ".join(values))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _extract_xls(data: bytes) -> str:
    try:
        import xlrd
    except Exception as exc:
        raise RuntimeError("缺少 xlrd 依赖，无法解析 XLS。") from exc

    book = xlrd.open_workbook(file_contents=data)
    parts = []
    for sheet in book.sheets():
        lines = [f"【工作表：{sheet.name}】"]
        for row_idx in range(min(sheet.nrows, MAX_SHEET_ROWS)):
            values = [str(sheet.cell_value(row_idx, col_idx)) for col_idx in range(sheet.ncols)]
            if any(v.strip() for v in values):
                lines.append(" | ".join(values))
        if sheet.nrows > MAX_SHEET_ROWS:
            lines.append(f"【已截断：仅解析前 {MAX_SHEET_ROWS} 行】")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _extract_csv(data: bytes) -> str:
    text = _decode_text(data)
    reader = csv.reader(io.StringIO(text))
    lines = [" | ".join(row) for row in reader if any(cell.strip() for cell in row)]
    return "\n".join(lines)


def extract_document(filename: str, data: bytes) -> ExtractedDocument:
    name = filename or "未命名文件"
    ext = detect_document_ext(name)
    try:
        if ext in TEXT_DOCUMENT_EXTS or ext in SPECIAL_TEXT_FILENAMES or ext == "csv":
            content = _extract_csv(data) if ext == "csv" else _decode_text(data)
        elif ext == "pdf":
            content = _extract_pdf(data)
        elif ext == "docx":
            content = _extract_docx(data)
        elif ext == "doc":
            content = _decode_text(data)
        elif ext == "xlsx":
            content = _extract_xlsx(data)
        elif ext == "xls":
            content = _extract_xls(data)
        else:
            raise RuntimeError(f"暂不支持的文件格式：.{ext or 'unknown'}")

        content = (content or "").strip()
        truncated = False
        if len(content) > MAX_TEXT_CHARS:
            content = content[:MAX_TEXT_CHARS]
            truncated = True
        if not content:
            raise RuntimeError("文件中没有提取到可用文本。")

        chunks = _chunk_text(content)
        return ExtractedDocument(
            name=name,
            ext=ext,
            status="ok",
            content=content,
            chunks=chunks,
            meta={"chars": len(content), "chunks": len(chunks), "truncated": truncated},
        )
    except Exception as exc:
        return ExtractedDocument(
            name=name,
            ext=ext,
            status="error",
            content="",
            chunks=[],
            error=str(exc),
            meta={},
        )
