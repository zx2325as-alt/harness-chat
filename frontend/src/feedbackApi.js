import { API_BASE } from "./apiBase.js";

/** 隐式反馈埋点（失败静默） */
export function sendFeedback(event, meta = {}) {
  try {
    fetch(`${API_BASE}/api/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event: String(event || "unknown"),
        meta: meta && typeof meta === "object" ? meta : {},
        ts: Date.now(),
      }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {
    /* ignore */
  }
}
