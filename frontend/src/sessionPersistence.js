import {
  idbSessionsSupported,
  loadSessionsJsonFromIdb,
  saveSessionsJsonToIdb,
} from "./idbSessions.js";

export async function loadSessionsState() {
  let idbRaw = null;
  let localRaw = null;
  try {
    if (await idbSessionsSupported()) {
      idbRaw = await loadSessionsJsonFromIdb();
    }
  } catch (e) {
    console.warn("IndexedDB load skipped:", e);
  }
  try {
    localRaw = localStorage.getItem("harness_sessions");
  } catch (_) {
    /* ignore */
  }

  const parsedState = resolveLoadedSessionsState(idbRaw, localRaw);

  const sessions = parsedState.sessions;
  const currentSessionId = parsedState.currentSessionId;
  if (parsedState.ok && parsedState.source === "localStorage") {
    try {
      if (await idbSessionsSupported()) {
        await saveSessionsJsonToIdb(parsedState.raw);
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
  return {
    sessions,
    currentSessionId,
    parseFailures: parsedState.parseFailures || [],
    recoveredFromSource: parsedState.source || "",
  };
}

export function tryParseSessionsState(raw, source = "") {
  if (!raw) {
    return { ok: false, sessions: [], currentSessionId: "", raw: "", source };
  }
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return { ok: true, sessions: parsed, currentSessionId: "", raw, source };
    }
    return {
      ok: true,
      sessions: Array.isArray(parsed?.sessions) ? parsed.sessions : [],
      currentSessionId: typeof parsed?.currentSessionId === "string" ? parsed.currentSessionId : "",
      raw,
      source,
    };
  } catch (e) {
    console.error(`Failed to parse sessions from ${source}`, e);
    return { ok: false, sessions: [], currentSessionId: "", raw, source };
  }
}

export function resolveLoadedSessionsState(idbRaw, localRaw) {
  const parseFailures = [];
  let parsedState = tryParseSessionsState(idbRaw, "idb");
  if (idbRaw && !parsedState.ok) {
    parseFailures.push("idb");
  }
  if (!parsedState.ok) {
    parsedState = tryParseSessionsState(localRaw, "localStorage");
    if (localRaw && !parsedState.ok) {
      parseFailures.push("localStorage");
    }
  }
  return { ...parsedState, parseFailures };
}

function cloneSessionsForPrune(sessions) {
  return Array.isArray(sessions)
    ? sessions.map((session) => ({
        ...session,
        messages: Array.isArray(session?.messages) ? session.messages.slice() : [],
        stepRuns: Array.isArray(session?.stepRuns) ? session.stepRuns.slice() : [],
      }))
    : [];
}

function trimSessionCollection(sessions, key, maxItems) {
  let changed = false;
  sessions.forEach((session) => {
    const rows = Array.isArray(session?.[key]) ? session[key] : [];
    if (rows.length > maxItems) {
      session[key] = maxItems > 0 ? rows.slice(-maxItems) : [];
      changed = true;
    }
  });
  return changed;
}

function removeOldestSession(sessions, currentSessionId) {
  if (!Array.isArray(sessions) || sessions.length <= 1) {
    return { removed: false, currentSessionId };
  }
  let removeIdx = -1;
  for (let i = sessions.length - 1; i >= 0; i -= 1) {
    if (sessions[i]?.id !== currentSessionId) {
      removeIdx = i;
      break;
    }
  }
  if (removeIdx < 0) removeIdx = sessions.length - 1;
  const removed = sessions.splice(removeIdx, 1)[0];
  let nextCurrentSessionId = currentSessionId;
  if (removed?.id === currentSessionId) {
    nextCurrentSessionId = sessions[0]?.id || "";
  }
  return { removed: true, currentSessionId: nextCurrentSessionId };
}

export function pruneSessionsForQuota(sessions, currentSessionId) {
  const next = cloneSessionsForPrune(sessions);
  let currentId = currentSessionId;
  const reducers = [
    () => trimSessionCollection(next, "stepRuns", 15),
    () => trimSessionCollection(next, "messages", 40),
    () => trimSessionCollection(next, "stepRuns", 8),
    () => trimSessionCollection(next, "messages", 24),
    () => trimSessionCollection(next, "stepRuns", 3),
    () => trimSessionCollection(next, "messages", 12),
    () => {
      const result = removeOldestSession(next, currentId);
      currentId = result.currentSessionId;
      return result.removed;
    },
    () => trimSessionCollection(next, "messages", 6),
    () => trimSessionCollection(next, "stepRuns", 0),
    () => trimSessionCollection(next, "messages", 3),
    () => {
      const result = removeOldestSession(next, currentId);
      currentId = result.currentSessionId;
      return result.removed;
    },
  ];

  for (const reduce of reducers) {
    if (reduce()) {
      break;
    }
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
      } else {
        throw e;
      }
    } else {
      throw e;
    }
  }
}
