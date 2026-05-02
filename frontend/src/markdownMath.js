/**
 * Markdown + KaTeX：在交给 marked 之前抽出数学片段，避免转义破坏公式；
 * 支持 \[...\]、\(...\)、含 LaTeX 命令的 [ ... ]、( ...\... )。
 */
import "katex/dist/katex.min.css";
import katex from "katex";
import { marked } from "marked";
import DOMPurify from "dompurify";

const PH = (i) => `<!--__KX${i}__-->`;

function extractMathSegments(raw) {
  const chunks = [];
  let md = raw;

  // 1. 标准 LaTeX 显示公式 \[ ... \]
  md = md.replace(/\\\[([\s\S]*?)\\\]/g, (_, inner) => {
    const i = chunks.length;
    chunks.push({ inner: inner.trim(), display: true });
    return PH(i);
  });

  // 2. 标准行内公式 \( ... \)
  md = md.replace(/\\\(([\s\S]*?)\\\)/g, (_, inner) => {
    const i = chunks.length;
    chunks.push({ inner: inner.trim(), display: false });
    return PH(i);
  });

  // 3. 模型常用：方括号包裹且含反斜杠（如 [ S\ge 6 ]、[ a\le b ]）
  md = md.replace(/\[(?=.*\\)[\s\S]*?\]/g, (full) => {
    const inner = full.slice(1, -1).trim();
    const i = chunks.length;
    chunks.push({ inner, display: true });
    return PH(i);
  });

  // 4. 圆括号行内含 LaTeX（如 (a\le b)，不含嵌套括号场景）
  md = md.replace(/\(([^\(\)\n]*\\[^\(\)\n]*)\)/g, (_, inner) => {
    const i = chunks.length;
    chunks.push({ inner: inner.trim(), display: false });
    return PH(i);
  });

  return { md, chunks };
}

function katexToHtml(chunks) {
  return chunks.map((c) => {
    try {
      return katex.renderToString(c.inner, {
        displayMode: c.display,
        throwOnError: false,
        strict: false,
        trust: true,
      });
    } catch {
      return `<span class="katex-fallback">${escapeHtml(c.inner)}</span>`;
    }
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** DOMPurify：保留 KaTeX 输出的 span/svg 及常用属性 */
function sanitizeMathHtml(html) {
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true, svg: true, svgFilters: true },
    ADD_TAGS: [
      "annotation",
      "semantics",
      "math",
      "mrow",
      "mi",
      "mo",
      "mn",
      "msup",
      "msub",
      "mfrac",
      "mtext",
      "menclose",
      "svg",
      "path",
      "line",
      "rect",
      "g",
      "defs",
      "use",
      "symbol",
      "clipPath",
    ],
    ADD_ATTR: [
      "class",
      "style",
      "xmlns",
      "width",
      "height",
      "viewBox",
      "fill",
      "stroke",
      "stroke-width",
      "d",
      "aria-hidden",
      "aria-*",
      "role",
      "tabindex",
      "focusable",
      "accent",
      "accentunder",
      "mathvariant",
      "encoding",
      "href",
      "x",
      "y",
      "x1",
      "y1",
      "x2",
      "y2",
      "stroke-linecap",
      "stroke-linejoin",
      "fill-rule",
      "clip-path",
    ],
  });
}

export function renderMarkdownWithMath(markdownText) {
  if (!markdownText) return "";
  const { md, chunks } = extractMathSegments(markdownText);
  let html =
    typeof marked.parse === "function"
      ? marked.parse(md, { async: false })
      : marked(md);
  const rendered = katexToHtml(chunks);
  rendered.forEach((fragment, i) => {
    html = html.split(PH(i)).join(fragment);
  });
  return sanitizeMathHtml(html);
}
