import {
  idbSessionsSupported,
  loadSessionsJsonFromIdb,
  saveSessionsJsonToIdb,
} from "./idbSessions.js";

export async function loadSessionsState() {
  let raw = null;
  try {
    if (await idbSessionsSupported()) {
      raw = await loadSessionsJsonFromIdb();
    }
  } catch (e) {
    console.warn("IndexedDB load skipped:", e);
  }
  if (!raw) {
    try {
      raw = localStorage.getItem("harness_sessions");
    } catch (_) {
      /* ignore */
    }
  }

  let sessions = [];
  let currentSessionId = "";
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        sessions = parsed;
      } else {
        sessions = Array.isArray(parsed?.sessions) ? parsed.sessions : [];
        currentSessionId = typeof parsed?.currentSessionId === "string" ? parsed.currentSessionId : "";
      }
    } catch (e) {
      console.error("Failed to parse sessions", e);
      sessions = [];
    }
    try {
      if (await idbSessionsSupported()) {
        await saveSessionsJsonToIdb(raw);
        try {
          localStorage.setItem("harness_sessions_idb", "1");
          localStorage.removeItem("harness_sessions");
        } catch (_) {
          /* ignore */
        }
      }
    } catch (e) {
      console.warn("Migrate sessions to IDB failed:", e);
    }
  }

  sessions.forEach((s) => {
    if (!s.stepRuns) s.stepRuns = [];
    if (s.useServerHistoryOnly == null) s.useServerHistoryOnly = false;
  });
  return { sessions, currentSessionId };
}

export function pruneSessionsForQuota(sessions, currentSessionId) {
  const next = Array.isArray(sessions) ? sessions : [];
  let currentId = currentSessionId;
  while (next.length > 1) {
    const rm = next.pop();
    if (rm && rm.id === currentId && next[0]) {
      currentId = next[0].id;
    }
  }
  const s = next[0];
  if (s && Array.isArray(s.messages) && s.messages.length > 40) {
    s.messages = s.messages.slice(-40);
  }
  if (s && Array.isArray(s.stepRuns) && s.stepRuns.length > 15) {
    s.stepRuns = s.stepRuns.slice(-15);
  }
  return { sessions: next, currentSessionId: currentId };
}

export async function persistSessionsState(payload, onQuotaExceeded) {
  try {
    if (await idbSessionsSupported()) {
      await saveSessionsJsonToIdb(payload);
      try {
        localStorage.setItem("harness_sessions_idb", "1");
        localStorage.removeItem("harness_sessions");
      } catch (_) {
        /* ignore */
      }
      return;
    }
  } catch (e) {
    console.warn("IndexedDB save failed, fallback localStorage:", e);
  }
  try {
    localStorage.setItem("harness_sessions", payload);
  } catch (e) {
    if (e && e.name === "QuotaExceededError") {
      if (onQuotaExceeded) {
        await onQuotaExceeded();
      }
    } else {
      throw e;
    }
  }
}
