/**
 * 后端 API 根地址。
 * - 默认 ""：请求与当前页面同源（线上需 Nginx/Caddy 等将 /api 反代到 Uvicorn）。
 * - 构建前设置环境变量 VUE_APP_API_BASE（如 https://api.example.com）可指向独立 API 域名。
 */
function trimSlash(s) {
  return s.replace(/\/$/, "");
}

const raw = process.env.VUE_APP_API_BASE;
export const API_BASE =
  raw != null && String(raw).trim() !== "" ? trimSlash(String(raw).trim()) : "";
