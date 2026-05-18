"""Tool Capability DAG Node：三种真实工具（web_search / document_search / calculator）。

工具执行结果写入 EvidenceGraph 与 ExecutionState，供后续 critic/verify 节点使用。
"""
from __future__ import annotations

import ast
import operator
import re
from typing import Any, Dict, List, Optional, Tuple

from refine_shared import _pg
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.state.evidence_state import EvidenceGraph, EvidenceNode
from runtime_state import GoalExecutionState


# ---------------------------------------------------------------------------
# 安全计算器（白名单 AST eval，禁止任意代码执行）
# ---------------------------------------------------------------------------

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str) -> Tuple[bool, str]:
    """白名单 AST 计算器：仅允许数学运算，返回 (ok, result_or_error)。"""
    expr = str(expr or "").strip()[:256]
    if not expr:
        return False, "empty expression"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return False, f"syntax error: {e}"

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            op_fn = _SAFE_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"unsupported op: {type(node.op)}")
            return op_fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_fn = _SAFE_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"unsupported unary op: {type(node.op)}")
            return op_fn(_eval(node.operand))
        raise ValueError(f"unsupported node: {type(node)}")

    try:
        result = _eval(tree)
        return True, str(result)
    except (ValueError, ZeroDivisionError, OverflowError) as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

class ToolResult:
    """统一工具返回体。"""
    __slots__ = ("tool", "query", "ok", "content", "source", "trust_score", "freshness_score")

    def __init__(
        self,
        tool: str,
        query: str,
        ok: bool,
        content: str,
        source: str = "",
        trust_score: float = 0.7,
        freshness_score: float = 0.6,
    ) -> None:
        self.tool = tool
        self.query = query
        self.ok = ok
        self.content = content
        self.source = source
        self.trust_score = trust_score
        self.freshness_score = freshness_score


async def _tool_web_search(ctx: DAGRuntimeContext, query: str) -> ToolResult:
    """调用 harness.perform_web_search，结果归一化为 ToolResult。"""
    h = ctx.harness
    opts = {**ctx.options, **(ctx.overrides or {})}
    try:
        sr = await h.perform_web_search(query, opts)
        if not isinstance(sr, dict):
            return ToolResult("web_search", query, False, "search returned no dict")
        results = sr.get("results") or []
        snippets: List[str] = []
        for r in results[:6]:
            title = str(r.get("title") or "")
            url = str(r.get("url") or "")
            body = str(r.get("content") or r.get("snippet") or "")[:400]
            if body:
                snippets.append(f"[{title}]({url}): {body}")
        content = "\n\n".join(snippets) if snippets else str(sr.get("answer") or "")[:800]
        source = "web_search:" + query[:120]
        return ToolResult("web_search", query, bool(content), content[:2400], source=source,
                          trust_score=0.72, freshness_score=0.85)
    except Exception as e:
        return ToolResult("web_search", query, False, f"error: {e}")


async def _tool_document_search(ctx: DAGRuntimeContext, query: str) -> ToolResult:
    """在已上传文档内检索：优先 BM25+embedding，降级到 substring 扫描。"""
    opts = ctx.options
    doc_block = str(opts.get("_documents_context_block") or "").strip()
    if not doc_block:
        return ToolResult("document_search", query, False, "no documents uploaded")
    try:
        from semantic_utils import ngram_overlap_ratio
        q_low = query.lower()[:200]
        lines = doc_block.split("\n")
        scored: List[Tuple[float, str]] = []
        window = 600
        for i in range(0, min(len(doc_block), 12000), 120):
            seg = doc_block[i: i + window]
            score = ngram_overlap_ratio(q_low, seg.lower()[:window])
            if score > 0.05:
                scored.append((score, seg))
        scored.sort(key=lambda x: -x[0])
        top = "\n---\n".join(s for _, s in scored[:4])
        content = top[:2000] if top.strip() else doc_block[:1000]
        return ToolResult("document_search", query, bool(content.strip()), content,
                          source="uploaded_documents", trust_score=0.88, freshness_score=0.5)
    except Exception as e:
        return ToolResult("document_search", query, False, f"error: {e}")


async def _tool_calculator(ctx: DAGRuntimeContext, expr: str) -> ToolResult:
    ok, result = _safe_eval(expr)
    content = f"{expr} = {result}" if ok else f"calc error: {result}"
    return ToolResult("calculator", expr, ok, content,
                      source="calculator", trust_score=1.0, freshness_score=1.0)


_TOOL_REGISTRY: Dict[str, Any] = {
    "web_search": _tool_web_search,
    "document_search": _tool_document_search,
    "calculator": _tool_calculator,
}


async def execute_tool(ctx: DAGRuntimeContext, tool_name: str, query: str) -> ToolResult:
    """分派到具体工具；未知工具返回 ok=False。"""
    fn = _TOOL_REGISTRY.get(str(tool_name or "").lower().strip())
    if fn is None:
        return ToolResult(tool_name, query, False, f"unknown tool: {tool_name!r}")
    return await fn(ctx, query)


def tool_result_to_evidence_node(tr: ToolResult) -> EvidenceNode:
    """将 ToolResult 写入 EvidenceGraph 节点。"""
    from datetime import datetime, timezone
    return EvidenceNode(
        source=tr.source or f"tool:{tr.tool}",
        claim=tr.content[:240].split("\n", 1)[0].strip() or tr.query[:120],
        content_excerpt=tr.content[:800],
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
        trust_score=tr.trust_score,
        freshness_score=tr.freshness_score,
        relevance_score=0.8,
    )


# ---------------------------------------------------------------------------
# DAG 节点入口（gate 模式：plan.use_tool_gate=True 时插入）
# ---------------------------------------------------------------------------

async def execute_tool_gate(ctx: DAGRuntimeContext) -> None:
    """Tool Capability Gate：启用能力层并执行初始工具调用（若 intent 需要工具）。"""
    ctx.runtime.enable("tool_use", reason="runtime_planner_tool_gate")

    intent = ctx.intent
    tool_queries: List[str] = []

    # 从 analysis 的 search_queries 或 prompt 派生初始工具查询
    raw_queries = (ctx.analysis or {}).get("search_queries") or []
    if isinstance(raw_queries, list):
        tool_queries = [str(q).strip() for q in raw_queries[:2] if str(q).strip()]
    if not tool_queries and ctx.prompt:
        tool_queries = [ctx.prompt[:200]]

    results: List[ToolResult] = []
    if tool_queries and intent is not None and (intent.search_score >= 0.4 or intent.tool_requirement):
        await ctx.emit({"event": "step", "step": {
            "name": "dag_tool_capability_gate",
            "status": "running",
            "meta": _pg({"tools": list(_TOOL_REGISTRY.keys()), "queries": tool_queries},
                        "reasoning", "Tool Capability Gate：执行初始工具调用"),
        }})
        import asyncio
        tasks = [execute_tool(ctx, "web_search", q) for q in tool_queries[:2]]
        results = list(await asyncio.gather(*tasks))

        # 结果写入 EvidenceGraph
        new_nodes = [tool_result_to_evidence_node(r) for r in results if r.ok]
        if new_nodes:
            if ctx.evidence_graph is None:
                ctx.evidence_graph = EvidenceGraph(nodes=new_nodes)
            else:
                ctx.evidence_graph.nodes.extend(new_nodes)
            ctx.evidence_objs.extend(new_nodes)  # type: ignore[arg-type]

        # 结果写入 ExecutionState
        st = ctx.st
        if st:
            for r in results:
                st.runtime_memory.append({
                    "phase": "tool_gate",
                    "tool": r.tool,
                    "query": r.query[:120],
                    "ok": r.ok,
                    "chars": len(r.content),
                })

    gate = {
        "enabled": True,
        "mode": "dag_capability",
        "tools_executed": len(results),
        "tools_ok": sum(1 for r in results if r.ok),
        "available_tools": list(_TOOL_REGISTRY.keys()),
    }
    ctx.options["_tool_runtime_gate"] = gate
    goal_exec = ctx.options.get("_goal_execution_state")
    if isinstance(goal_exec, GoalExecutionState):
        goal_exec.tool_results["tool_gate"] = gate

    await ctx.emit({"event": "step", "step": {
        "name": "dag_tool_capability_gate",
        "status": "ok",
        "meta": _pg(gate, "reasoning", f"Tool Capability Gate 完成（执行 {len(results)} 个工具调用）"),
    }})
