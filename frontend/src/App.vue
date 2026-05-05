<template>
  <div class="layout">
    <header class="topbar">
      <div class="brand">
        <div class="dot" />
        <div class="title">Harness Chat</div>
        <div class="subtitle">Harness：Fast / Refine / Agent</div>
      </div>
      <div class="actions">
        <button class="tab" :class="{ active: view === 'chat' }" @click="view = 'chat'">聊天</button>
        <button class="tab" :class="{ active: view === 'config' }" @click="view = 'config'">配置</button>
        <button
          v-if="view === 'chat'"
          type="button"
          class="tab panel-toggle"
          :class="{ active: showStepsPanel }"
          @click="showStepsPanel = !showStepsPanel"
        >
          {{ showStepsPanel ? "隐藏执行过程" : "显示执行过程" }}
        </button>
      </div>
    </header>

    <div class="app-body">
      <aside class="sidebar" v-if="view === 'chat'">
        <div class="sidebar-actions">
          <button class="new-chat-btn" :disabled="interactionBusy" @click="createNewSession">
            <span class="icon">+</span> 新建对话
          </button>
          <button type="button" class="clear-ctx-btn" :disabled="interactionBusy" @click="clearSessionContext">清空上下文</button>
        </div>
        <div class="session-list">
          <div 
            class="session-item" 
            v-for="s in sessions" 
            :key="s.id" 
            :class="{ active: currentSessionId === s.id }"
            @click="selectSession(s.id)"
          >
            <div class="session-title">{{ getSessionTitle(s) }}</div>
            <button class="delete-btn" :disabled="interactionBusy" @click.stop="deleteSession(s.id)">×</button>
          </div>
        </div>
      </aside>

      <main class="main" :class="{ 'no-right': hideRightPanel }" :style="mainGridStyle">
        <section class="left">
          <div v-if="view === 'chat'" class="chat">
            <div v-if="chatBanner" class="chat-banner" role="status">{{ chatBanner }}</div>
            <div class="messages" ref="msgRef">
              <ChatMessage
                v-for="m in currentMessages"
                :key="m.id"
                :role="m.role"
                :content="m.content"
                :meta="m.meta"
                @edit="onEditMessage(m)"
                @regenerate="onRegenerateMessage(m)"
                @copy="onMessageCopy(m, $event)"
                @retry="onRetryFromMessage(m)"
              />
            </div>
            <ChatInput
              ref="chatInput"
              :busy="interactionBusy"
              :mode="mode"
              @update:mode="mode = $event"
              @send="onSend"
              @stop="onStop"
            />
          </div>

          <div v-else class="config">
            <ConfigView :config="config" :loading="configLoading" :error="configError" @reload="loadConfig" />
          </div>
        </section>

        <div
          v-if="view === 'chat' && showStepsPanel"
          class="col-resizer"
          title="拖拽调整执行面板宽度"
          @mousedown.prevent="onPanelResizeStart"
        />
        <aside v-if="view === 'chat' && showStepsPanel" class="right">
          <div class="panelHeader">
            <div class="panelTitle">执行过程</div>
          </div>
          <StepDisplay
            ref="stepDisplay"
            class="steps-panel-inner"
            :runs="currentStepRuns"
            :render-tick="stepUiTick"
          />
        </aside>
      </main>
    </div>
  </div>
</template>

<script>
import ChatInput from "./components/ChatInput.vue";
import ChatMessage from "./components/ChatMessage.vue";
import StepDisplay from "./components/StepDisplay.vue";
import ConfigView from "./components/ConfigView.vue";
import { API_BASE } from "./apiBase.js";
import { sendFeedback } from "./feedbackApi.js";
import {
  buildStreamErrorDetail,
  buildSseParseTerminalEvent,
  buildStreamInterruptedMessage,
  buildStreamRequestPayload,
  clipHistoryMessages,
  computeModelChain,
  createPendingAssistant,
  createResumeContext,
  createSessionId,
  createStepRun as createStepRunEntry,
  createStreamContext,
  createUserMessage,
  getSessionTitle as deriveSessionTitle,
  getRunTitle as deriveRunTitle,
  normalizePayload as normalizeChatPayload,
  removeSessionLocally,
  restoreSessionState as restoreSessionStateHelper,
  snapshotSessionState as snapshotSessionStateHelper,
  shouldIncludeHistoryMessage,
  splitSseFrames,
  sseReconnectDelayMs,
  stepSignature as getStepSignature,
  userContentToPrompt as contentToPrompt,
} from "./chatShared.js";
import {
  loadSessionsState,
  persistSessionsState,
  pruneSessionsForQuota,
  shouldAutoPersistRecoveredEmptySession,
} from "./sessionPersistence.js";

/** SSE 在这么长时间内完全无事件则客户端主动断开（有 step/status 等即会刷新） */
const CHAT_STREAM_IDLE_MS = 180000;

export default {
  name: "App",
  components: { ChatInput, ChatMessage, StepDisplay, ConfigView },
  data() {
    return {
      view: "chat",
      mode: "auto",
      busy: false,
      sessions: [],
      currentSessionId: null,
      activeRunId: null,
      config: null,
      configLoading: false,
      configError: "",
      abortController: null,
      stepUiTick: 0,
      showStepsPanel: true,
      rightPanelWidth: 320,
      chatBanner: "",
      /** 服务端 Redis 已持久化本轮后，后续请求可省略 messages 体积 */
      _saveSessionsTimer: null,
      _saveSessionsPromise: null,
      _saveSessionsQueued: false,
      _lastPersistedSessionsPayload: "",
      _scrollBottomPending: false,
      _panelResize: null,
      _streamIdleTimer: null,
      _dwellTimer: null,
      _lastChunkAt: 0,
      _sessionTxnDepth: 0,
    };
  },
  computed: {
    mainGridStyle() {
      if (this.view !== "chat" || !this.showStepsPanel) return {};
      const w = Math.min(520, Math.max(240, this.rightPanelWidth));
      return { gridTemplateColumns: `minmax(0, 1fr) 5px ${w}px` };
    },
    hideRightPanel() {
      return this.view !== "chat" || !this.showStepsPanel;
    },
    interactionBusy() {
      return this.busy || this._sessionTxnDepth > 0;
    },
    currentSession() {
      return this.sessions.find(s => s.id === this.currentSessionId) || null;
    },
    currentMessages() {
      return this.currentSession ? this.currentSession.messages : [];
    },
    currentStepRuns() {
      void this.stepUiTick;
      if (!this.currentSession || !this.currentSession.stepRuns) return [];
      return this.currentSession.stepRuns;
    },
  },
  async mounted() {
    this.loadConfig();
    await this.loadSessionsFromStorage();
    try {
      const v = localStorage.getItem("harness_show_steps");
      if (v === "0") this.showStepsPanel = false;
      const pw = parseInt(localStorage.getItem("harness_panel_w") || "", 10);
      if (!Number.isNaN(pw) && pw >= 200) this.rightPanelWidth = pw;
    } catch (_) {
      /* ignore */
    }
    this._dwellTimer = setInterval(() => {
      if (this.view === "chat" && this.currentSessionId && !this.busy) {
        sendFeedback("session_tick", { session_id: this.currentSessionId, idle: true });
      }
    }, 120000);
  },
  beforeUnmount() {
    if (this._dwellTimer) clearInterval(this._dwellTimer);
    if (this._streamIdleTimer) clearInterval(this._streamIdleTimer);
    if (this._saveSessionsTimer) clearTimeout(this._saveSessionsTimer);
    this._detachPanelResize();
  },
  watch: {
    showStepsPanel(val) {
      try {
        localStorage.setItem("harness_show_steps", val ? "1" : "0");
      } catch (_) {
        /* ignore */
      }
    },
  },
  methods: {
    async loadSessionsFromStorage() {
      const loaded = await loadSessionsState();
      this.sessions = loaded.sessions;
      if (this.sessions.length === 0) {
        if ((loaded.parseFailures || []).length >= 2) {
          this.chatBanner = "本地历史加载失败，已自动创建新会话；原有本地存储数据可能已损坏。";
        }
        this.createNewSession({ persist: shouldAutoPersistRecoveredEmptySession(loaded) });
      } else {
        const preferredId = loaded.currentSessionId;
        this.currentSessionId =
          this.sessions.find((s) => s.id === preferredId)?.id || this.sessions[0].id;
        this.sessions.forEach((s) => this.syncStepRunsWithMessages(s));
        if ((loaded.parseFailures || []).length > 0 && loaded.recoveredFromSource === "localStorage") {
          this.chatBanner = "检测到 IndexedDB 会话数据损坏，已自动回退并恢复本地历史。";
        }
      }
    },
    _buildSessionsPersistPayload() {
      return JSON.stringify({
        currentSessionId: this.currentSessionId || "",
        sessions: (this.sessions || []).map((session) => ({
          ...session,
          // 仅保留运行期标志，避免刷新后粘住“只用服务端历史”状态。
          useServerHistoryOnly: false,
        })),
      });
    },
    async saveSessions() {
      const persistOnce = async (payload) => {
        await persistSessionsState(payload, async () => {
          let retryPayload = payload;
          for (let i = 0; i < 12; i += 1) {
            const beforePayload = this._buildSessionsPersistPayload();
            this._pruneSessionsForQuota();
            retryPayload = this._buildSessionsPersistPayload();
            if (retryPayload === beforePayload) {
              throw new Error("Session persistence quota fallback exhausted");
            }
            try {
              await persistSessionsState(retryPayload);
              this._lastPersistedSessionsPayload = retryPayload;
              return;
            } catch (e) {
              if (!(e && e.name === "QuotaExceededError")) {
                throw e;
              }
            }
          }
          throw new Error("Session persistence quota fallback exhausted");
        });
      };
      let payload = this._buildSessionsPersistPayload();
      if (!this._saveSessionsPromise && payload === this._lastPersistedSessionsPayload) return;
      if (this._saveSessionsPromise) {
        this._saveSessionsQueued = true;
        return this._saveSessionsPromise;
      }
      this._saveSessionsPromise = (async () => {
        while (payload) {
          this._saveSessionsQueued = false;
          try {
            await persistOnce(payload);
            this._lastPersistedSessionsPayload = payload;
          } catch (e) {
            console.error("Failed to save sessions", e);
            const msg = String(e && e.message ? e.message : e);
            this.chatBanner = msg.includes("quota fallback exhausted")
              ? "本地会话已尽力裁剪，但仍无法完整保存；建议手动清理部分旧会话后重试。"
              : "本地会话保存失败，本次改动可能不会在刷新后保留。";
          }
          if (!this._saveSessionsQueued) break;
          payload = this._buildSessionsPersistPayload();
          if (payload === this._lastPersistedSessionsPayload) break;
        }
      })()
        .catch((e) => console.error("saveSessions persist", e))
        .finally(() => {
          this._saveSessionsPromise = null;
        });
      return this._saveSessionsPromise;
    },
    _pruneSessionsForQuota() {
      const pruned = pruneSessionsForQuota(this.sessions, this.currentSessionId);
      this.sessions = pruned.sessions;
      this.currentSessionId = pruned.currentSessionId;
    },
    scheduleSaveSessions() {
      if (this._saveSessionsTimer) clearTimeout(this._saveSessionsTimer);
      this._saveSessionsTimer = setTimeout(() => {
        this._saveSessionsTimer = null;
        void this.saveSessions();
      }, 80);
    },
    flushSaveSessions() {
      if (this._saveSessionsTimer) {
        clearTimeout(this._saveSessionsTimer);
        this._saveSessionsTimer = null;
      }
      void this.saveSessions();
    },
    scheduleScrollBottom() {
      if (this._scrollBottomPending) return;
      this._scrollBottomPending = true;
      requestAnimationFrame(() => {
        this._scrollBottomPending = false;
        this.scrollToBottom();
      });
    },
    createNewSession(options = {}) {
      if (this.interactionBusy) return;
      const newSession = this._makeBlankSession();
      this.sessions.unshift(newSession);
      this.currentSessionId = newSession.id;
      if (options.persist !== false) {
        this.scheduleSaveSessions();
      }
    },
    selectSession(id) {
      if (this.interactionBusy) return;
      this.currentSessionId = id;
      this.scheduleSaveSessions();
      this.scrollToBottom();
    },
    _makeBlankSession() {
      return {
        id: createSessionId(),
        messages: [],
        stepRuns: [],
        useServerHistoryOnly: false,
      };
    },
    async _runSessionTransaction(work) {
      this._sessionTxnDepth += 1;
      try {
        return await work();
      } finally {
        this._sessionTxnDepth = Math.max(0, this._sessionTxnDepth - 1);
      }
    },
    async deleteSession(id) {
      if (this.interactionBusy) return;
      await this._runSessionTransaction(async () => {
        const exists = this.sessions.find((s) => s.id === id);
        if (!exists) return;
        const snapshotSessions = JSON.parse(JSON.stringify(this.sessions || []));
        const snapshotCurrentSessionId = this.currentSessionId;
        const next = removeSessionLocally(
          this.sessions,
          this.currentSessionId,
          id,
          () => this._makeBlankSession()
        );
        this.sessions = next.sessions;
        this.currentSessionId = next.currentSessionId;
        const clearResult = await this.clearServerHistory(id);
        if (clearResult && clearResult.ok === false) {
          this.sessions = snapshotSessions;
          this.currentSessionId = snapshotCurrentSessionId;
          this.chatBanner = "服务端历史清理失败，已取消删除，避免前后端会话语义不一致。";
          this.flushSaveSessions();
          return;
        }
        this.scheduleSaveSessions();
      });
    },
    getSessionTitle(session) {
      return deriveSessionTitle(session);
    },
    async loadConfig() {
      this.configLoading = true;
      this.configError = "";
      try {
        const r = await fetch(`${API_BASE}/api/config`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        this.config = await r.json();
      } catch (e) {
        this.configError = String(e && e.message ? e.message : e);
      } finally {
        this.configLoading = false;
      }
    },
    scrollToBottom() {
      this.$nextTick(() => {
        const el = this.$refs.msgRef;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    normalizePayload(payload) {
      return normalizeChatPayload(payload);
    },
    getRunTitle(content) {
      return deriveRunTitle(content);
    },
    createStepRun(userMsg, documents = [], searchMode = "auto", session = this.currentSession) {
      if (!session) return null;
      if (!session.stepRuns) session.stepRuns = [];
      const run = createStepRunEntry(userMsg, documents, searchMode);
      session.stepRuns.push(run);
      this.activeRunId = run.id;
      this.scheduleSaveSessions();
      return run;
    },
    syncStepRunsWithMessages(session) {
      if (!session) return;
      const validRunIds = new Set(
        (session.messages || [])
          .filter((m) => m.role === "assistant" && m.meta?.runId)
          .map((m) => m.meta.runId)
      );
      session.stepRuns = (session.stepRuns || []).filter((run) => validRunIds.has(run.id));
      if (this.activeRunId && !validRunIds.has(this.activeRunId)) {
        this.activeRunId = "";
      }
    },
    removeMessageRangeFromSession(session, startIdx) {
      if (!session || startIdx < 0) return;
      session.messages.splice(startIdx);
      session.useServerHistoryOnly = false;
      this.syncStepRunsWithMessages(session);
    },
    removeAssistantMessageById(session, messageId) {
      if (!session || !messageId) return;
      const idx = session.messages.findIndex((m) => m.id === messageId);
      if (idx < 0) return;
      session.messages.splice(idx, 1);
      this.syncStepRunsWithMessages(session);
    },
    snapshotSessionState(session) {
      return snapshotSessionStateHelper(session);
    },
    restoreSessionState(session, snapshot) {
      restoreSessionStateHelper(session, snapshot);
      this.syncStepRunsWithMessages(session);
    },
    /** 同一步 name 可能多次出现（如审查多轮联网），用签名区分，避免互相覆盖 */
    stepSignature(step) {
      return getStepSignature(step);
    },
    upsertStep(run, step) {
      const sig = this.stepSignature(step);
      const idx = run.steps.findIndex((s) => this.stepSignature(s) === sig);
      if (idx >= 0) run.steps.splice(idx, 1, step);
      else run.steps.push(step);
      this.stepUiTick += 1;
    },
    bumpStepUi() {
      this.stepUiTick += 1;
    },
    _userContentToPrompt(content) {
      return contentToPrompt(content);
    },
    /** 供 API 的历史条：不含当前用户句。 */
    buildHistoryForApi(session, lastUser, pendingId) {
      const uid = lastUser && lastUser.id;
      const rows = (session?.messages || []).filter((m) => shouldIncludeHistoryMessage(m, uid, pendingId));
      const mapped = rows.map((m) => ({ role: m.role, content: m.content }));
      return clipHistoryMessages(mapped);
    },
    setPendingTerminal(pending, meta = {}) {
      pending.meta = {
        ...pending.meta,
        pending: false,
        streaming: false,
        ...meta,
      };
    },
    _computeModelChainFromRun(run) {
      return computeModelChain(run);
    },
    _sseReconnectDelayMs(attemptIdx) {
      return sseReconnectDelayMs(attemptIdx);
    },
    async _applyStreamSseEvent(event, ctx, bundle) {
      const { run, pending, session } = bundle;
      // 任意 SSE 业务事件都应刷新空闲计时：仅在有 chunk 正文时刷新会导致
      // 预判/联网/Agent 推理/精化层等长时间无流式正文时被误判为断流并 abort。
      this._lastChunkAt = Date.now();
      if (event.event === "trace") {
        run.traceId = event.trace_id;
        if (event.track) {
          run.track = event.track;
          pending.meta.track = event.track;
        }
        this.bumpStepUi();
        await this.$nextTick();
      } else if (event.event === "status") {
        run.phase = event.phase || "";
        run.phaseMessage = event.message || "";
        this.bumpStepUi();
        await this.$nextTick();
      } else if (event.event === "step") {
        this.upsertStep(run, event.step);
        const st = event.step || {};
        const meta = st.meta || {};
        if (st.name === "track_select" && meta.agent_disabled_fallback) {
          this.chatBanner = "您选择了 Agent 轨，但当前配置已关闭 Agent，已自动使用精化轨。";
        }
        if (st.name === "web_search" && (meta.degraded || meta.failure_code) && st.status === "ok") {
          this.chatBanner =
            "联网检索未完全成功（已降级或跳过），回答中的实时信息可能不完整。详见右侧「执行过程」。";
        }
        await this.$nextTick();
        this.scheduleScrollBottom();
      } else if (event.event === "stream_start") {
        run.track = event.track;
        run.traceId = event.trace_id;
        pending.meta.track = event.track;
        this.bumpStepUi();
        await this.$nextTick();
      } else if (event.event === "model_start") {
        ctx.finalMeta.model = event.model;
        ctx.finalMeta.provider = event.provider;
      } else if (event.event === "model_end") {
        ctx.finalMeta.latency_ms = event.latency_ms || 0;
      } else if (event.event === "model_error") {
        ctx.modelErrors.push(`${event.model || "?"}: ${event.error || "失败"}`);
        run.phaseMessage = `模型重试中…（${ctx.modelErrors.length} 次失败）`;
        this.bumpStepUi();
      } else if (event.event === "chunk") {
        const data = event.data || {};
        if (data.content) {
          ctx.finalContent += data.content;
          pending.content = ctx.finalContent;
          this.scheduleScrollBottom();
        }
      } else if (event.event === "content_reset") {
        ctx.finalContent = "";
        ctx.contentResetCount += 1;
        pending.content = "";
        this.scheduleScrollBottom();
      } else if (event.event === "history_stored") {
        if (session) session.useServerHistoryOnly = true;
      } else if (event.event === "history_miss") {
        if (session) session.useServerHistoryOnly = false;
        this.chatBanner = "服务端历史缺失，已回退为携带本地上下文请求。";
      } else if (event.event === "error") {
        ctx.lastErrorEvent = event;
        run.phaseMessage = event.error || "服务端返回错误";
        this.bumpStepUi();
      } else if (event.event === "error_terminal") {
        ctx.streamFailed = true;
        ctx.lastErrorEvent = event;
        run.status = "error";
        run.phaseMessage = event.error || "服务端流式处理失败";
        this.bumpStepUi();
      }
    },
    async _pumpChatSseReader(reader, decoder, ctx, bundle) {
      const handleData = async (dataStr) => {
        if (dataStr === "[DONE]") {
          ctx.receivedDone = true;
          return true;
        }
        let event;
        try {
          event = JSON.parse(dataStr);
        } catch (e) {
          console.error("SSE parse error", e, dataStr);
          const terminalEvent = buildSseParseTerminalEvent(dataStr);
          ctx.lastErrorEvent = terminalEvent;
          ctx.streamFailed = true;
          await this._applyStreamSseEvent(terminalEvent, ctx, bundle);
          return true;
        }
        await this._applyStreamSseEvent(event, ctx, bundle);
        return !!(ctx.receivedDone || ctx.streamFailed);
      };
      while (!ctx.receivedDone && !ctx.streamFailed) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (e) {
          if (e && e.name === "AbortError") throw e;
          throw new Error((e && e.message) || String(e));
        }
        if (chunk.done) {
          const rest = ctx.buffer.trim();
          if (rest) {
            const lines = rest.split("\n").map((x) => x.trim()).filter(Boolean);
            const dataLines = lines
              .filter((line) => line.startsWith("data:") || line.startsWith("data: "))
              .map((line) => line.replace(/^data:\s?/, ""));
            if (dataLines.length && (await handleData(dataLines.join("\n")))) return true;
          }
          return false;
        }
        ctx.buffer += decoder.decode(chunk.value, { stream: true });
        const { frames, rest } = splitSseFrames(ctx.buffer);
        ctx.buffer = rest;
        for (const frame of frames) {
          const dataLines = frame
            .split("\n")
            .map((x) => x.trim())
            .filter((line) => line.startsWith("data:") || line.startsWith("data: "))
            .map((line) => line.replace(/^data:\s?/, ""));
          if (!dataLines.length) continue;
          if (await handleData(dataLines.join("\n"))) return true;
        }
      }
      return !!ctx.receivedDone;
    },
    onPanelResizeStart(e) {
      const startX = e.clientX;
      const startW = this.rightPanelWidth;
      const onMove = (ev) => {
        const dx = startX - ev.clientX;
        this.rightPanelWidth = Math.min(520, Math.max(240, startW + dx));
      };
      const onUp = () => {
        this._detachPanelResize();
        try {
          localStorage.setItem("harness_panel_w", String(this.rightPanelWidth));
        } catch (_) {
          /* ignore */
        }
      };
      this._panelResize = { onMove, onUp };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    },
    _detachPanelResize() {
      if (!this._panelResize) return;
      window.removeEventListener("pointermove", this._panelResize.onMove);
      this._panelResize = null;
    },
    async clearServerHistory(sessionId) {
      if (!sessionId) return { ok: true, cleared: false, reason: "no_session" };
      try {
        const res = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/history`, {
          method: "DELETE",
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false) {
          throw new Error(data.error || `HTTP ${res.status}`);
        }
        return data;
      } catch (e) {
        return { ok: false, cleared: false, error: String(e?.message || e) };
      }
    },
    async clearSessionContext() {
      if (this.interactionBusy || !this.currentSession) return;
      await this._runSessionTransaction(async () => {
        const session = this.currentSession;
        const sessionId = session.id;
        const snapshot = this.snapshotSessionState(session);
        session.messages = [];
        session.stepRuns = [];
        session.useServerHistoryOnly = false;
        this.chatBanner = "";
        const clearResult = await this.clearServerHistory(sessionId);
        if (clearResult && clearResult.ok === false) {
          this.restoreSessionState(session, snapshot);
          this.chatBanner = "服务端历史清理失败，已恢复当前会话，避免前后端上下文不一致。";
        }
        this.flushSaveSessions();
        sendFeedback("clear_context", { session_id: sessionId });
      });
    },
    onMessageCopy(msg, detail) {
      sendFeedback("copy_answer", {
        session_id: this.currentSession && this.currentSession.id,
        message_id: msg && msg.id,
        ...(detail || {}),
      });
    },
    async onRetryFromMessage(msg) {
      const ctx = msg && msg.meta && msg.meta.resumeContext;
      if (!ctx || this.interactionBusy) return;
      if (!ctx.userMsg) {
        this.chatBanner = "无法重试：缺少原始用户问题。";
        return;
      }
      const session = this.currentSession;
      if (session && msg && msg.id) {
        this.removeAssistantMessageById(session, msg.id);
        this.scheduleSaveSessions();
      }
      await this._triggerStream({ ...ctx, upgradeTrack: false, session });
    },
    async onSend(payload) {
      if (!payload || this.interactionBusy) return;
      if (!this.currentSession) this.createNewSession();
      if (!this.currentSession.messages) this.currentSession.messages = [];

      const normalized = this.normalizePayload(payload);
      const text = typeof normalized.content === "string" ? normalized.content.trim() : normalized.content;
      const documents = (normalized.documents || []).filter((d) => d && d.status === "ok");
      if ((!text || text.length === 0) && documents.length === 0) return;
      const session = this.currentSession;
      const userMsg = createUserMessage(
        normalized.content,
        documents,
        normalized.searchMode || "auto"
      );
      session.messages.push(userMsg);
      this.scrollToBottom();
      this.scheduleSaveSessions();

      await this._triggerStream({
        documents: userMsg.meta.documents,
        searchMode: userMsg.meta.searchMode,
        session,
        userMsg,
        upgradeTrack: false,
      });
    },
    async _triggerStream(context = null) {
      const session = context?.session || this.currentSession;
      if (!session) return;
      this.busy = true;
      this.abortController = new AbortController();
      this.chatBanner = "";

      const lastUser = context?.userMsg || [...session.messages].reverse().find((m) => m.role === "user");
      if (!lastUser) {
        this.busy = false;
        this.chatBanner = "无法继续：当前会话中缺少用户消息。";
        return;
      }
      const documents = context?.documents || lastUser?.meta?.documents || [];
      const searchMode = context?.searchMode || lastUser?.meta?.searchMode || "auto";
      const upgradeTrack = Boolean(context?.upgradeTrack);
      const run = this.createStepRun(lastUser || { content: "" }, documents, searchMode, session);
      if (!run) {
        this.busy = false;
        return;
      }

      const pending = createPendingAssistant(run.id);
      session.messages.push(pending);
      this.scrollToBottom();

      const promptBody = this._userContentToPrompt(lastUser && lastUser.content);
      const ctx = createStreamContext();
      const { modelErrors } = ctx;
      const preferServerHistory = Boolean(session.useServerHistoryOnly);
      session.useServerHistoryOnly = false;
      this.scheduleSaveSessions();

      try {
        const history = this.buildHistoryForApi(session, lastUser, pending.id);

        const bundle = { run, pending, session, lastUser, documents, searchMode };
        const MAX_SSE_CONNECTS = 8;

        pending.content = "";
        pending.meta = { ...pending.meta, streaming: true, track: "" };
        this._lastChunkAt = Date.now();
        if (this._streamIdleTimer) clearInterval(this._streamIdleTimer);
        this._streamIdleTimer = setInterval(() => {
          if (Date.now() - this._lastChunkAt > CHAT_STREAM_IDLE_MS) {
            if (this.abortController) this.abortController.abort();
          }
        }, 4000);

        let lastConnectError = null;
        for (let attempt = 0; attempt < MAX_SSE_CONNECTS && !ctx.receivedDone && !ctx.streamFailed; attempt++) {
          if (attempt > 0) {
            if (ctx.finalContent.length > 0) {
              pending.content += buildStreamInterruptedMessage();
              this.setPendingTerminal(pending, {
                stopped: true,
                resumeContext: createResumeContext(lastUser, documents, searchMode),
                stop_reason: "stream_interrupted_after_content",
              });
              run.status = "stopped";
              run.phaseMessage = "流传输中断";
              this.bumpStepUi();
              this.flushSaveSessions();
              break;
            }
            const waitMs = this._sseReconnectDelayMs(attempt - 1);
            run.phaseMessage = `网络中断，${Math.round(waitMs / 100) / 10}s 后自动重连 (${attempt}/${MAX_SSE_CONNECTS - 1})…`;
            this.chatBanner = "连接不稳定，正在自动重连…";
            this.bumpStepUi();
            await new Promise((r) => setTimeout(r, waitMs));
            ctx.buffer = "";
          }

          const reqBody = buildStreamRequestPayload({
            sessionId: session.id,
            prompt: promptBody,
            history,
            mode: this.mode,
            documents,
            searchMode,
            upgradeTrack,
            preferServerHistory,
            clientRunId: run.id,
            streamConnectAttempt: attempt,
          });

          let res;
          try {
            res = await fetch(`${API_BASE}/api/chat/stream`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: reqBody,
              signal: this.abortController.signal,
            });
          } catch (fe) {
            lastConnectError = fe;
            if (fe && fe.name === "AbortError") throw fe;
            if (attempt === MAX_SSE_CONNECTS - 1) throw fe;
            continue;
          }

          if (!res.ok) {
            const t = await res.text().catch(() => "");
            lastConnectError = new Error(`HTTP ${res.status}: ${t}`);
            if (attempt === MAX_SSE_CONNECTS - 1) throw lastConnectError;
            continue;
          }

          const contentType = String(res.headers.get("content-type") || "").toLowerCase();
          if (!contentType.includes("text/event-stream")) {
            const bodyText = await res.text().catch(() => "");
            lastConnectError = new Error(
              `接口未返回 SSE 数据流（content-type=${contentType || "unknown"}）: ${bodyText.slice(0, 200)}`
            );
            if (attempt === MAX_SSE_CONNECTS - 1) throw lastConnectError;
            continue;
          }

          const reader = res.body && res.body.getReader();
          if (!reader) {
            lastConnectError = new Error("响应无可读流");
            if (attempt === MAX_SSE_CONNECTS - 1) throw lastConnectError;
            continue;
          }

          const decoder = new TextDecoder();
          try {
            const completed = await this._pumpChatSseReader(reader, decoder, ctx, bundle);
            if (ctx.receivedDone || ctx.streamFailed) break;
            if (!completed && ctx.finalContent.length > 0) {
              pending.content += buildStreamInterruptedMessage();
              this.setPendingTerminal(pending, {
                stopped: true,
                resumeContext: createResumeContext(lastUser, documents, searchMode),
                stop_reason: "stream_interrupted_after_content",
              });
              run.status = "stopped";
              run.phaseMessage = "流传输中断";
              this.bumpStepUi();
              this.flushSaveSessions();
              break;
            }
            if (!completed && ctx.finalContent.length === 0) {
              lastConnectError = new Error("连接在收到内容前关闭");
              if (attempt === MAX_SSE_CONNECTS - 1) throw lastConnectError;
            }
          } catch (readErr) {
            lastConnectError = readErr;
            if (readErr && readErr.name === "AbortError") throw readErr;
            if (ctx.finalContent.length > 0) {
              pending.content += buildStreamInterruptedMessage();
              this.setPendingTerminal(pending, {
                stopped: true,
                resumeContext: createResumeContext(lastUser, documents, searchMode),
                stop_reason: "stream_read_error_after_content",
              });
              run.status = "stopped";
              run.phaseMessage = "流传输中断";
              this.bumpStepUi();
              this.flushSaveSessions();
              break;
            }
            if (attempt === MAX_SSE_CONNECTS - 1) throw readErr;
          } finally {
            try {
              await reader.cancel("close_or_reconnect");
            } catch (_) {
              /* ignore */
            }
          }
        }

        if (lastConnectError && !ctx.receivedDone && !ctx.streamFailed && ctx.finalContent.length === 0) {
          throw lastConnectError;
        }
        if (ctx.streamFailed) {
          const terminalError = ctx.lastErrorEvent || {};
          throw new Error(
            terminalError.error_code
              ? `${terminalError.error || "服务端流式处理失败"} (${terminalError.error_code})`
              : (terminalError.error || "服务端流式处理失败")
          );
        }

        ctx.finalMeta.model_chain = this._computeModelChainFromRun(run) || ctx.finalMeta.model_chain;
        this.setPendingTerminal(pending, { ...ctx.finalMeta, success: true });
        if (!ctx.streamFailed && run.status !== "error" && run.status !== "stopped") {
          run.status = "ok";
        }
        this.bumpStepUi();
        this.flushSaveSessions();
      } catch (e) {
        if (this._streamIdleTimer) {
          clearInterval(this._streamIdleTimer);
          this._streamIdleTimer = null;
        }
        if (e.name === "AbortError") {
          const idle = Date.now() - this._lastChunkAt > CHAT_STREAM_IDLE_MS - 10000;
          pending.content += idle
            ? "\n\n[长时间无任何服务端推送，已中止连接；若模型仍在推理，可稍后重试。]"
            : "\n\n[用户已中断响应]";
          this.setPendingTerminal(pending, {
            stopped: true,
            stop_reason: idle ? "client_idle_timeout" : "user_abort",
            resumeContext: createResumeContext(lastUser, documents, searchMode),
          });
          run.status = "stopped";
          run.phaseMessage = idle ? "长时间无服务端推送" : "用户已中断";
          this.bumpStepUi();
          this.flushSaveSessions();
        } else {
          const errText = String(e && e.message ? e.message : e);
          pending.content = buildStreamErrorDetail({
            errorText: errText,
            run,
            lastErrorEvent: ctx.lastErrorEvent,
            modelErrors,
          });
          this.setPendingTerminal(pending, {
            error: true,
            success: false,
            error_message: errText,
            resumeContext: createResumeContext(lastUser, documents, searchMode),
          });
          run.status = "error";
          run.phaseMessage = errText;
          this.bumpStepUi();
          this.flushSaveSessions();
        }
      } finally {
        if (this._streamIdleTimer) {
          clearInterval(this._streamIdleTimer);
          this._streamIdleTimer = null;
        }
        this.busy = false;
        this.bumpStepUi();
        this.flushSaveSessions();
        this.scrollToBottom();
      }
    },
    onStop() {
      if (this.abortController) {
        this.abortController.abort();
      }
    },
    async onEditMessage(msg) {
      if (this.interactionBusy || !this.currentSession) return;
      let prefillPayload = null;
      await this._runSessionTransaction(async () => {
        const session = this.currentSession;
        const sessionId = session.id;
        const idx = session.messages.findIndex((m) => m.id === msg.id);
        if (idx < 0) return;
        const snapshot = this.snapshotSessionState(session);
        let text = "";
        const images = [];
        if (typeof msg.content === "string") text = msg.content;
        else if (Array.isArray(msg.content)) {
          msg.content.forEach((c) => {
            if (c && c.type === "text") text += (text ? "\n" : "") + (c.text || "");
            if (c && c.type === "image_url" && c.image_url && c.image_url.url) {
              images.push({ url: c.image_url.url, name: "图片", type: "image/png" });
            }
          });
        }
        this.removeMessageRangeFromSession(session, idx);
        const clearResult = await this.clearServerHistory(sessionId);
        if (clearResult && clearResult.ok === false) {
          this.restoreSessionState(session, snapshot);
          this.chatBanner = "服务端历史清理失败，已恢复当前会话，避免前后端上下文不一致。";
          this.flushSaveSessions();
          return;
        }
        this.flushSaveSessions();
        prefillPayload = {
          text,
          images,
          documents: (msg.meta && msg.meta.documents) || [],
        };
      });
      if (prefillPayload && this.$refs.chatInput && this.$refs.chatInput.prefillFromUserEdit) {
        this.$refs.chatInput.prefillFromUserEdit(prefillPayload);
      }
    },
    async onRegenerateMessage(msg) {
      if (this.interactionBusy || !this.currentSession) return;
      let rerunContext = null;
      await this._runSessionTransaction(async () => {
        const session = this.currentSession;
        const sessionId = session.id;
        const idx = session.messages.findIndex((m) => m.id === msg.id);
        if (idx < 0) return;
        const snapshot = this.snapshotSessionState(session);
        const lastUser = session.messages.slice(0, idx).reverse().find((m) => m.role === "user");
        if (!lastUser) {
          this.chatBanner = "无法重新生成：缺少对应的用户消息。";
          return;
        }
        this.removeMessageRangeFromSession(session, idx);
        const clearResult = await this.clearServerHistory(sessionId);
        if (clearResult && clearResult.ok === false) {
          this.restoreSessionState(session, snapshot);
          this.chatBanner = "服务端历史清理失败，已恢复当前会话，避免前后端上下文不一致。";
          this.flushSaveSessions();
          return;
        }
        this.flushSaveSessions();
        sendFeedback("regenerate", { session_id: sessionId, message_id: msg.id });
        rerunContext = {
          userMsg: lastUser,
          documents: lastUser?.meta?.documents || [],
          searchMode: lastUser?.meta?.searchMode || "auto",
          session,
          upgradeTrack: true,
        };
      });
      if (rerunContext) {
        await this._triggerStream(rerunContext);
      }
    },
  },
};
</script>

<style scoped>
.layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #161b26;
  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "PingFang SC",
    "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
.topbar {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  background: #1e2433;
  border-bottom: 1px solid #2f3a4d;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #818cf8;
  box-shadow: 0 0 12px rgba(129, 140, 248, 0.45);
}
.title {
  font-weight: 700;
  font-size: 15px;
  color: #f1f5f9;
  letter-spacing: 0.2px;
}
.subtitle {
  color: #64748b;
  font-size: 12px;
}
.actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}
.tab.panel-toggle:not(.active) {
  opacity: 0.9;
}
.tab {
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid #3d4d64;
  background: #252d3d;
  color: #94a3b8;
  cursor: pointer;
  font-size: 13px;
}
.tab:hover {
  border-color: #4b5c78;
  color: #e2e8f0;
}
.tab.active {
  background: rgba(99, 102, 241, 0.2);
  border-color: rgba(129, 140, 248, 0.45);
  color: #c7d2fe;
  font-weight: 600;
}
.app-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.sidebar {
  width: 236px;
  background: #1e2433;
  border-right: 1px solid #2f3a4d;
  display: flex;
  flex-direction: column;
}
.sidebar-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 14px;
}
.new-chat-btn {
  margin: 0;
  padding: 10px 14px;
  background: linear-gradient(135deg, #4f46e5, #6366f1);
  border: none;
  border-radius: 10px;
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  transition: filter 0.15s;
}
.new-chat-btn:hover {
  filter: brightness(1.06);
}
.new-chat-btn:disabled,
.delete-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.new-chat-btn .icon {
  font-size: 16px;
  line-height: 1;
}
.clear-ctx-btn {
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid #3d4d64;
  background: #252d3d;
  color: #94a3b8;
  font-size: 12px;
  cursor: pointer;
}
.clear-ctx-btn:hover:not(:disabled) {
  border-color: #64748b;
  color: #e2e8f0;
}
.clear-ctx-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 10px 12px;
}
.session-item {
  padding: 10px 12px;
  margin-bottom: 4px;
  border-radius: 10px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #94a3b8;
  transition: background 0.15s;
}
.session-item:hover {
  background: rgba(255, 255, 255, 0.05);
}
.session-item.active {
  background: rgba(99, 102, 241, 0.18);
  color: #e0e7ff;
}
.session-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.delete-btn {
  background: none;
  border: none;
  color: #64748b;
  cursor: pointer;
  opacity: 0;
  font-size: 16px;
  padding: 0 4px;
  line-height: 1;
}
.delete-btn:hover {
  color: #f87171;
}
.session-item:hover .delete-btn {
  opacity: 1;
}

.main {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
  min-height: 0;
}
.col-resizer {
  width: 5px;
  cursor: col-resize;
  background: #1e2433;
  border-left: 1px solid #2f3a4d;
  border-right: 1px solid #2f3a4d;
  flex-shrink: 0;
}
.col-resizer:hover {
  background: rgba(99, 102, 241, 0.15);
}
.main.no-right {
  grid-template-columns: 1fr;
}
.left,
.right {
  min-height: 0;
}
.left {
  background: #1a1f2b;
  border-right: 1px solid #2f3a4d;
  overflow: hidden;
}
.main.no-right .left {
  border-right: none;
}
.right {
  background: #1c2230;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.panelHeader {
  padding: 12px 14px;
  border-bottom: 1px solid #2f3a4d;
  background: #232a38;
  flex-shrink: 0;
}
.panelTitle {
  font-weight: 700;
  font-size: 14px;
  color: #f1f5f9;
}
.steps-panel-inner {
  flex: 1;
  min-height: 0;
}
.chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chat-banner {
  flex-shrink: 0;
  margin: 0 16px 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(251, 191, 36, 0.12);
  border: 1px solid rgba(251, 191, 36, 0.35);
  color: #fcd34d;
  font-size: 13px;
  line-height: 1.45;
}
.messages {
  flex: 1;
  overflow: auto;
  padding: 20px 24px;
  background: linear-gradient(180deg, #161b26 0%, #1a1f2b 160px);
}
.config {
  height: 100%;
  overflow: auto;
  padding: 18px;
  background: #1a1f2b;
}
@media (max-width: 980px) {
  .main {
    grid-template-columns: 1fr;
  }
  .left {
    border-right: none;
  }
  .right {
    max-height: 380px;
    border-top: 1px solid #2f3a4d;
  }
}
</style>
