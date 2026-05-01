<template>
  <div class="layout">
    <header class="topbar">
      <div class="brand">
        <div class="dot" />
        <div class="title">Harness Chat</div>
        <div class="subtitle">双轨 Harness（Fast / Refine）</div>
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
        <button class="new-chat-btn" @click="createNewSession">
          <span class="icon">+</span> 新建对话
        </button>
        <div class="session-list">
          <div 
            class="session-item" 
            v-for="s in sessions" 
            :key="s.id" 
            :class="{ active: currentSessionId === s.id }"
            @click="selectSession(s.id)"
          >
            <div class="session-title">{{ getSessionTitle(s) }}</div>
            <button class="delete-btn" @click.stop="deleteSession(s.id)">×</button>
          </div>
        </div>
      </aside>

      <main class="main" :class="{ 'no-right': hideRightPanel }">
        <section class="left">
          <div v-if="view === 'chat'" class="chat">
            <div class="messages" ref="msgRef">
              <ChatMessage
                v-for="m in currentMessages"
                :key="m.id"
                :role="m.role"
                :content="m.content"
                :meta="m.meta"
                @edit="onEditMessage(m)"
                @regenerate="onRegenerateMessage(m)"
              />
            </div>
            <ChatInput
              ref="chatInput"
              :busy="busy"
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

        <aside v-if="view === 'chat' && showStepsPanel" class="right">
          <div class="panelHeader">
            <div class="panelTitle">执行过程</div>
          </div>
          <StepDisplay class="steps-panel-inner" :runs="currentStepRuns" :render-tick="stepUiTick" />
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

const API_BASE = "http://localhost:8000";

export default {
  name: "App",
  components: { ChatInput, ChatMessage, StepDisplay, ConfigView },
  data() {
    return {
      view: "chat",
      mode: "refine",
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
    };
  },
  computed: {
    hideRightPanel() {
      return this.view !== "chat" || !this.showStepsPanel;
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
  mounted() {
    this.loadConfig();
    this.loadSessions();
    try {
      const v = localStorage.getItem("harness_show_steps");
      if (v === "0") this.showStepsPanel = false;
    } catch (_) {
      /* ignore */
    }
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
    loadSessions() {
      try {
        const saved = localStorage.getItem("harness_sessions");
        if (saved) {
          this.sessions = JSON.parse(saved);
        }
      } catch(e) {
        console.error("Failed to load sessions", e);
      }
      if (this.sessions.length === 0) {
        this.createNewSession();
      } else {
        this.currentSessionId = this.sessions[0].id;
      }
      this.sessions.forEach((s) => {
        if (!s.stepRuns) s.stepRuns = [];
      });
    },
    saveSessions() {
      try {
        localStorage.setItem("harness_sessions", JSON.stringify(this.sessions));
      } catch(e) {
        console.error("Failed to save sessions", e);
      }
    },
    createNewSession() {
      const newSession = {
        id: `session-${Date.now()}`,
        messages: [],
        stepRuns: [],
      };
      this.sessions.unshift(newSession);
      this.currentSessionId = newSession.id;
      this.saveSessions();
    },
    selectSession(id) {
      if (this.busy) return;
      this.currentSessionId = id;
      this.scrollToBottom();
    },
    deleteSession(id) {
      if (this.busy && this.currentSessionId === id) return;
      this.sessions = this.sessions.filter(s => s.id !== id);
      if (this.sessions.length === 0) {
        this.createNewSession();
      } else if (this.currentSessionId === id) {
        this.currentSessionId = this.sessions[0].id;
      }
      this.saveSessions();
    },
    getSessionTitle(session) {
      if (!session || !session.messages) return '新对话';
      const userMsg = session.messages.find(m => m.role === 'user');
      if (userMsg && userMsg.content) {
        let title = userMsg.content;
        if (typeof title === 'string') {
           return title.substring(0, 15) + (title.length > 15 ? '...' : '');
        } else if (Array.isArray(title)) {
           const textPart = title.find(t => t.type === 'text');
           if (textPart && textPart.text) {
              return textPart.text.substring(0, 15) + '...';
           }
           return '[多模态消息]';
        }
      }
      return '新对话';
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
      if (payload && typeof payload === "object" && !Array.isArray(payload) && Object.prototype.hasOwnProperty.call(payload, "content")) {
        return payload;
      }
      return { content: payload, documents: [], searchMode: "auto" };
    },
    getRunTitle(content) {
      if (typeof content === "string") return content.substring(0, 28) || "新请求";
      if (Array.isArray(content)) {
        const text = content.find((p) => p.type === "text")?.text || "";
        return text.substring(0, 28) || "多模态请求";
      }
      return "新请求";
    },
    createStepRun(userMsg, documents = [], searchMode = "auto") {
      if (!this.currentSession.stepRuns) this.currentSession.stepRuns = [];
      const run = {
        id: `run-${Date.now()}`,
        title: this.getRunTitle(userMsg.content),
        createdAt: new Date().toISOString(),
        traceId: "",
        track: "",
        status: "running",
        steps: [],
        documents: documents.map((d) => ({ name: d.name, status: d.status, meta: d.meta })),
        searchMode,
      };
      this.currentSession.stepRuns.push(run);
      this.activeRunId = run.id;
      this.saveSessions();
      return run;
    },
    upsertStep(run, step) {
      const idx = run.steps.findIndex((s) => s.name === step.name);
      if (idx >= 0) run.steps.splice(idx, 1, step);
      else run.steps.push(step);
      this.stepUiTick += 1;
      this.saveSessions();
    },
    bumpStepUi() {
      this.stepUiTick += 1;
    },
    async onSend(payload) {
      if (!payload || this.busy) return;
      if (!this.currentSession) this.createNewSession();
      if (!this.currentSession.messages) this.currentSession.messages = [];

      const normalized = this.normalizePayload(payload);
      const userMsg = {
        id: `u-${Date.now()}`,
        role: "user",
        content: normalized.content,
        meta: {
          documents: normalized.documents || [],
          searchMode: normalized.searchMode || "auto",
        },
      };
      this.currentSession.messages.push(userMsg);
      this.scrollToBottom();
      this.saveSessions();

      await this._triggerStream({
        documents: userMsg.meta.documents,
        searchMode: userMsg.meta.searchMode,
        userMsg,
      });
    },
    async _triggerStream(context = null) {
      this.busy = true;
      this.abortController = new AbortController();

      const lastUser = context?.userMsg || [...this.currentSession.messages].reverse().find((m) => m.role === "user");
      const documents = context?.documents || lastUser?.meta?.documents || [];
      const searchMode = context?.searchMode || lastUser?.meta?.searchMode || "auto";
      const run = this.createStepRun(lastUser || { content: "" }, documents, searchMode);

      const pending = {
        id: `a-pending-${Date.now()}`,
        role: "assistant",
        content: "处理中…",
        meta: { pending: true, runId: run.id },
      };
      this.currentSession.messages.push(pending);
      this.scrollToBottom();

      try {
        const history = this.currentSession.messages
          .filter((m) => (m.role === "user" || m.role === "assistant") && !m.meta?.pending && !m.meta?.error && m.id !== pending.id)
          .map((m) => ({ role: m.role, content: m.content }));

        const r = await fetch(`${API_BASE}/api/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: this.currentSession.id,
            prompt: "",
            messages: history,
            mode: this.mode,
            options: {
              documents,
              search_mode: searchMode,
              stream_slice_chars: 6,
            },
          }),
          signal: this.abortController.signal,
        });
        if (!r.ok) {
          const t = await r.text();
          throw new Error(`HTTP ${r.status}: ${t}`);
        }

        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let finalContent = "";
        let finalMeta = { track: "", provider: "", model: "", success: true, latency_ms: 0 };
        let streamFailed = false;
        let receivedDone = false;

        pending.content = "";
        pending.meta = { ...pending.meta, streaming: true, track: "" };

        while (!receivedDone) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (let line of lines) {
            line = line.trim();
            if (!line) continue;
            if (!line.startsWith("data: ")) continue;
            const dataStr = line.substring(6);
            if (dataStr === "[DONE]") {
              receivedDone = true;
              break;
            }

            let event;
            try {
              event = JSON.parse(dataStr);
            } catch (e) {
              console.error("SSE parse error", e);
              continue;
            }

            if (event.event === "trace") {
              run.traceId = event.trace_id;
              if (event.track) {
                run.track = event.track;
                pending.meta.track = event.track;
              }
              this.bumpStepUi();
              this.saveSessions();
            } else if (event.event === "step") {
              this.upsertStep(run, event.step);
              this.scrollToBottom();
            } else if (event.event === "stream_start") {
              run.track = event.track;
              run.traceId = event.trace_id;
              pending.meta.track = event.track;
              this.bumpStepUi();
              this.saveSessions();
            } else if (event.event === "model_start") {
              finalMeta.model = event.model;
              finalMeta.provider = event.provider;
            } else if (event.event === "model_end") {
              finalMeta.latency_ms = event.latency_ms || 0;
            } else if (event.event === "chunk") {
              const data = event.data || {};
              if (data.content) {
                finalContent += data.content;
                pending.content = finalContent;
                this.scrollToBottom();
              }
            } else if (event.event === "error") {
              streamFailed = true;
              run.status = "error";
              this.bumpStepUi();
              this.saveSessions();
              throw new Error(event.error);
            }
          }
        }

        pending.meta.streaming = false;
        pending.meta = { ...pending.meta, ...finalMeta };
        if (!streamFailed && run.status !== "error" && run.status !== "stopped") {
          run.status = "ok";
        }
        this.bumpStepUi();
        this.saveSessions();
      } catch (e) {
        if (e.name === "AbortError") {
          pending.meta.streaming = false;
          pending.content += "\n\n[用户已中断响应]";
          run.status = "stopped";
          this.bumpStepUi();
          this.saveSessions();
        } else {
          const errText = String(e && e.message ? e.message : e);
          const idx = this.currentSession.messages.findIndex((m) => m.id === pending.id);
          const msg = { id: `a-err-${Date.now()}`, role: "assistant", content: `请求失败：${errText}`, meta: { error: true } };
          if (idx >= 0) this.currentSession.messages.splice(idx, 1, msg);
          else this.currentSession.messages.push(msg);
          run.status = "error";
          this.bumpStepUi();
          this.saveSessions();
        }
      } finally {
        this.busy = false;
        this.bumpStepUi();
        this.saveSessions();
        this.scrollToBottom();
      }
    },
    onStop() {
      if (this.abortController) {
        this.abortController.abort();
      }
    },
    onEditMessage(msg) {
      if (this.busy || !this.currentSession) return;
      const idx = this.currentSession.messages.findIndex((m) => m.id === msg.id);
      if (idx >= 0) {
        let text = "";
        if (typeof msg.content === 'string') text = msg.content;
        else if (Array.isArray(msg.content)) text = msg.content.filter(c=>c.type==='text').map(c=>c.text).join('\n');
        
        this.currentSession.messages.splice(idx);
        this.saveSessions();
        if (this.$refs.chatInput) {
          this.$refs.chatInput.draft = text;
        }
      }
    },
    onRegenerateMessage(msg) {
      if (this.busy || !this.currentSession) return;
      const idx = this.currentSession.messages.findIndex((m) => m.id === msg.id);
      if (idx >= 0) {
        const lastUser = this.currentSession.messages.slice(0, idx).reverse().find((m) => m.role === "user");
        this.currentSession.messages.splice(idx);
        this.saveSessions();
        this._triggerStream({
          userMsg: lastUser,
          documents: lastUser?.meta?.documents || [],
          searchMode: lastUser?.meta?.searchMode || "auto",
        });
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
.new-chat-btn {
  margin: 14px;
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
.new-chat-btn .icon {
  font-size: 16px;
  line-height: 1;
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
  grid-template-columns: minmax(0, 1fr) 392px;
  gap: 0;
  min-height: 0;
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
