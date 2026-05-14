export const HISTORY_MAX_MESSAGES = 12;
export const HISTORY_MAX_CHARS = 12000;
export const DEFAULT_RUNTIME = "adaptive_dag_v3";

export function createSessionId() {
  try {
    return crypto.randomUUID();
  } catch (_) {
    return `session-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }
}

export function createRunId() {
  try {
    return `run-${crypto.randomUUID()}`;
  } catch (_) {
    return `run-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }
}

export function createMessageId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function normalizePayload(payload) {
  if (
    payload &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    Object.prototype.hasOwnProperty.call(payload, "content")
  ) {
    return payload;
  }
  return { content: payload, documents: [], searchMode: "auto" };
}

export function getSessionTitle(session) {
  if (!session || !Array.isArray(session.messages)) return "新对话";
  const userMsg = session.messages.find((m) => m.role === "user");
  if (!userMsg || userMsg.content == null) return "新对话";
  if (typeof userMsg.content === "string") {
    return userMsg.content.substring(0, 15) + (userMsg.content.length > 15 ? "..." : "");
  }
  if (Array.isArray(userMsg.content)) {
    const textPart = userMsg.content.find((t) => t && t.type === "text");
    if (textPart && textPart.text) {
      return textPart.text.substring(0, 15) + (textPart.text.length > 15 ? "..." : "");
    }
    return "[多模态消息]";
  }
  return "新对话";
}

export function getRunTitle(content) {
  if (typeof content === "string") return content.substring(0, 28) || "新请求";
  if (Array.isArray(content)) {
    const text = content.find((p) => p && p.type === "text")?.text || "";
    return text.substring(0, 28) || "多模态请求";
  }
  return "新请求";
}

export function createStepRun(userMsg, documents = [], searchMode = "auto", runtime = DEFAULT_RUNTIME) {
  return {
    id: createRunId(),
    title: getRunTitle(userMsg.content),
    createdAt: new Date().toISOString(),
    traceId: "",
    runtime,
    status: "running",
    steps: [],
    documents: documents.map((d) => ({ name: d.name, status: d.status, meta: d.meta })),
    searchMode,
    phaseMessage: "",
    phase: "",
    serverRunId: "",
    lastEventSeq: 0,
  };
}

export function createPendingAssistant(runId) {
  return {
    id: createMessageId("a-pending"),
    role: "assistant",
    content: "处理中…",
    meta: { pending: true, runId },
  };
}

export function createUserMessage(content, documents = [], searchMode = "auto") {
  return {
    id: createMessageId("u"),
    role: "user",
    content,
    meta: {
      documents,
      searchMode,
    },
  };
}

export function createResumeContext(userMsg, documents, searchMode) {
  return { userMsg, documents, searchMode };
}

export function snapshotSessionState(session) {
  if (!session) return null;
  return {
    messages: JSON.parse(JSON.stringify(session.messages || [])),
    stepRuns: JSON.parse(JSON.stringify(session.stepRuns || [])),
    useServerHistoryOnly: Boolean(session.useServerHistoryOnly),
  };
}

export function restoreSessionState(session, snapshot) {
  if (!session || !snapshot) return;
  session.messages = snapshot.messages;
  session.stepRuns = snapshot.stepRuns;
  session.useServerHistoryOnly = snapshot.useServerHistoryOnly;
}

export function removeSessionLocally(sessions, currentSessionId, id, createSession) {
  const nextSessions = Array.isArray(sessions) ? sessions.filter((s) => s.id !== id) : [];
  let nextCurrentSessionId = currentSessionId;
  if (nextSessions.length === 0) {
    const newSession = createSession();
    nextSessions.unshift(newSession);
    nextCurrentSessionId = newSession.id;
  } else if (currentSessionId === id) {
    nextCurrentSessionId = nextSessions[0].id;
  }
  return { sessions: nextSessions, currentSessionId: nextCurrentSessionId };
}

export function createStreamContext() {
  return {
    buffer: "",
    receivedDone: false,
    streamFailed: false,
    finalContent: "",
    finalMeta: { runtime: DEFAULT_RUNTIME, provider: "", model: "", success: true, latency_ms: 0, model_chain: "" },
    lastErrorEvent: null,
    modelErrors: [],
    contentResetCount: 0,
    lastEventSeq: 0,
    lastRunId: "",
  };
}

export function buildSseParseTerminalEvent(dataStr) {
  return {
    event: "error_terminal",
    error: "SSE 事件解析失败，已终止当前响应。",
    error_code: "SSE_PARSE_ERROR",
    raw_preview: String(dataStr || "").slice(0, 200),
  };
}

export function buildStreamRequestPayload({
  sessionId,
  prompt,
  history,
  runtime,
  documents,
  searchMode,
  preferServerHistory,
  clientRunId,
  streamConnectAttempt,
  runId,
  afterSeq,
}) {
  return JSON.stringify({
    session_id: sessionId,
    prompt,
    messages: history,
    runtime,
    options: {
      documents,
      search_mode: searchMode,
      stream_slice_chars: 24,
      prefer_server_history: Boolean(preferServerHistory),
      client_run_id: clientRunId,
      stream_connect_attempt: streamConnectAttempt,
      run_id: runId,
      after_seq: Math.max(0, Number(afterSeq || 0)),
    },
  });
}

export function stepSignature(step) {
  const n = step.name || "";
  const m = step.meta || {};
  if (step.step_id) return step.step_id;
  if (m.step_id) return m.step_id;
  if (n === "review_web_search" && m.review_round != null) return `${n}#r${m.review_round}`;
  if (n === "agent_iteration" && m.i != null) return `${n}#${m.i}`;
  return n;
}

export function userContentToPrompt(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content;
  return String(content ?? "");
}

export function clipHistoryMessages(messages, maxMessages = HISTORY_MAX_MESSAGES, maxChars = HISTORY_MAX_CHARS) {
  const rows = Array.isArray(messages) ? messages.slice(-maxMessages) : [];
  const picked = [];
  let used = 0;
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const row = rows[i];
    const text = typeof row?.content === "string" ? row.content : JSON.stringify(row?.content ?? "");
    const size = text.length;
    if (picked.length > 0 && used + size > maxChars) break;
    used += size;
    picked.push(row);
  }
  return picked.reverse();
}

export function shouldIncludeHistoryMessage(message, excludedUserId = "", pendingId = "") {
  if (!message || (message.role !== "user" && message.role !== "assistant")) return false;
  if (message.id === pendingId) return false;
  if (excludedUserId && message.id === excludedUserId) return false;
  if (message.meta?.pending) return false;
  if (message.meta?.error) return false;
  if (message.meta?.stopped) return false;
  return true;
}

export function splitSseFrames(buffer) {
  const normalized = String(buffer || "").replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  return {
    frames: frames.slice(0, -1),
    rest: frames[frames.length - 1] || "",
  };
}

export function isSendableComposerState({ draft, attachments, busy, hasParsingAttachment, hasImageAttachment }) {
  if (busy || hasParsingAttachment || hasImageAttachment) return false;
  const text = String(draft || "").trim();
  const okDocuments = (attachments || []).filter((a) => a.kind === "document" && a.status === "ok" && a.doc);
  return Boolean(text || okDocuments.length > 0);
}

export function computeModelChain(run) {
  const labels = {
    dag_draft: "起草",
    dag_parallel_critic: "评估",
    dag_repair: "修复",
    dag_verify: "验证",
    dag_finalize: "终稿",
    refine_layer1_draft: "起草",
    refine_draft: "起草",
    refine_runtime_generate: "起草",
    refine_layer2_review: "评估",
    refine_quality_review: "结构化评估",
    refine_runtime_critic: "评估",
    refine_runtime_repair: "修复",
    refine_runtime_verify: "验证",
    refine_layer3_polish: "终稿润色",
    refine_finalize: "终稿",
    refine_runtime_finalize: "终稿",
    agent_iteration: "推理",
  };
  const parts = [];
  (run.steps || []).forEach((s) => {
    if (s.status === "ok" && s.model) {
      const lb = labels[s.name] || s.name;
      parts.push(`${lb}:${s.model}`);
    }
  });
  return parts.length ? parts.join(" → ") : "";
}

export function sseReconnectDelayMs(attemptIdx) {
  const base = 700 * Math.pow(2, attemptIdx);
  return Math.min(32000, base + Math.random() * 500);
}

export function buildStreamInterruptedMessage() {
  return "\n\n[流传输中断；为避免重复生成，已停止自动重连。请点击「重试」或重新发送。]";
}

export function buildStreamErrorDetail({ errorText, run, lastErrorEvent, modelErrors }) {
  return [
    `请求失败：${errorText}`,
    "",
    "排查信息：",
    `- 运行时：${run.runtime || DEFAULT_RUNTIME}`,
    `- 阶段：${run.phaseMessage || run.phase || "未知"}`,
    `- Trace：${run.traceId || "无"}`,
    lastErrorEvent?.error_code ? `- 错误码：${lastErrorEvent.error_code}` : "",
    modelErrors.length ? `- 模型错误：${modelErrors.join("；")}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}
