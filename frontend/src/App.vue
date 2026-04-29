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

      <main class="main">
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

        <aside class="right">
          <div class="panelHeader">
            <div class="panelTitle">执行过程</div>
            <div class="panelHint">每一步都会显示：轨道/路由/模型/耗时/降级</div>
          </div>
          <StepDisplay :traceId="latestTraceId" :track="latestTrack" :steps="latestSteps" />
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
      latestSteps: [],
      latestTraceId: "",
      latestTrack: "",
      config: null,
      configLoading: false,
      configError: "",
      abortController: null,
    };
  },
  computed: {
    currentSession() {
      return this.sessions.find(s => s.id === this.currentSessionId) || null;
    },
    currentMessages() {
      return this.currentSession ? this.currentSession.messages : [];
    }
  },
  mounted() {
    this.loadConfig();
    this.loadSessions();
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
        messages: []
      };
      this.sessions.unshift(newSession);
      this.currentSessionId = newSession.id;
      this.saveSessions();
      this.latestSteps = [];
      this.latestTraceId = "";
      this.latestTrack = "";
    },
    selectSession(id) {
      if (this.busy) return;
      this.currentSessionId = id;
      this.latestSteps = [];
      this.latestTraceId = "";
      this.latestTrack = "";
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
    async onSend(payload) {
      if (!payload || this.busy) return;
      
      // Ensure we have a valid current session before sending
      if (!this.currentSession) {
        this.createNewSession();
      }
      
      if (!this.currentSession.messages) {
        this.currentSession.messages = [];
      }

      const userMsg = { id: `u-${Date.now()}`, role: "user", content: payload };
      this.currentSession.messages.push(userMsg);
      this.scrollToBottom();
      this.saveSessions();

      await this._triggerStream();
    },
    async _triggerStream() {
      this.busy = true;
      this.latestSteps = [];
      this.latestTraceId = "";
      this.latestTrack = "";
      this.abortController = new AbortController();

      const pending = {
        id: `a-pending-${Date.now()}`,
        role: "assistant",
        content: "处理中…",
        meta: { pending: true },
      };
      this.currentSession.messages.push(pending);
      this.scrollToBottom();

      try {
        // Build the messages history to send (excluding the pending one we just added)
        const history = this.currentSession.messages
          .filter((m) => (m.role === "user" || m.role === "assistant") && !m.meta?.pending && !m.meta?.error && m.id !== "sys-1" && m.id !== pending.id)
          .map((m) => ({ role: m.role, content: m.content }));
          
        const r = await fetch(`${API_BASE}/api/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
            session_id: this.currentSession.id,
            prompt: "", 
            messages: history, 
            mode: this.mode, 
            options: {} 
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
        let finalReasoning = "";
        let finalMeta = { track: "", provider: "", model: "", success: true, latency_ms: 0 };
        
        // Remove "处理中…" and set empty content
        pending.content = "";
        pending.meta = { ...pending.meta, streaming: true, track: "" };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          
          const lines = buffer.split("\n");
          buffer = lines.pop(); // keep the incomplete line in buffer
          
          for (let line of lines) {
            line = line.trim();
            if (!line) continue;
            if (line.startsWith("data: ")) {
              const dataStr = line.substring(6);
              if (dataStr === "[DONE]") break;
              
              try {
                const event = JSON.parse(dataStr);
                
                if (event.event === "step") {
                  const step = event.step;
                  const existingIdx = this.latestSteps.findIndex(s => s.name === step.name);
                  if (existingIdx >= 0) {
                    this.latestSteps.splice(existingIdx, 1, step);
                  } else {
                    this.latestSteps.push(step);
                  }
                } else if (event.event === "stream_start") {
                  this.latestTrack = event.track;
                  this.latestTraceId = event.trace_id;
                  pending.meta.track = event.track;
                } else if (event.event === "model_start") {
                  finalMeta.model = event.model;
                  finalMeta.provider = event.provider;
                } else if (event.event === "chunk") {
                  const data = event.data;
                  if (data.content) {
                    finalContent += data.content;
                    pending.content = finalContent;
                    this.scrollToBottom();
                  }
                  if (data.reasoning_content) {
                    finalReasoning += data.reasoning_content;
                    pending.meta.reasoning_content = finalReasoning;
                    this.scrollToBottom();
                  }
                } else if (event.event === "error") {
                  throw new Error(event.error);
                }
              } catch (e) {
                console.error("SSE parse error", e, dataStr);
              }
            }
          }
        }
        
        // Done streaming
        pending.meta.streaming = false;
        pending.meta = { ...pending.meta, ...finalMeta };

      } catch (e) {
        if (e.name === 'AbortError') {
          pending.meta.streaming = false;
          pending.content += "\n\n[用户已中断响应]";
          pending.meta.error = false;
        } else {
          const errText = String(e && e.message ? e.message : e);
          const idx = this.currentSession.messages.findIndex((m) => m.id === pending.id);
          const msg = { id: `a-err-${Date.now()}`, role: "assistant", content: `请求失败：${errText}`, meta: { error: true } };
          if (idx >= 0) this.currentSession.messages.splice(idx, 1, msg);
          else this.currentSession.messages.push(msg);
        }
      } finally {
        this.busy = false;
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
        this.currentSession.messages.splice(idx);
        this.saveSessions();
        this._triggerStream();
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
  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "PingFang SC",
    "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
.topbar {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  background: linear-gradient(180deg, #121a2b, #0b0f17);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: #7c5cff;
  box-shadow: 0 0 18px rgba(124, 92, 255, 0.8);
}
.title {
  font-weight: 700;
  letter-spacing: 0.2px;
}
.subtitle {
  opacity: 0.7;
  font-size: 12px;
}
.actions {
  display: flex;
  gap: 8px;
}
.tab {
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.06);
  color: #e6e9f2;
  cursor: pointer;
}
.tab.active {
  background: rgba(124, 92, 255, 0.18);
  border-color: rgba(124, 92, 255, 0.5);
}
.app-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.sidebar {
  width: 240px;
  background: rgba(255, 255, 255, 0.02);
  border-right: 1px solid rgba(255, 255, 255, 0.08);
  display: flex;
  flex-direction: column;
}
.new-chat-btn {
  margin: 16px;
  padding: 10px;
  background: rgba(124, 92, 255, 0.15);
  border: 1px solid rgba(124, 92, 255, 0.4);
  border-radius: 8px;
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: all 0.2s;
}
.new-chat-btn:hover {
  background: rgba(124, 92, 255, 0.25);
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}
.session-item {
  padding: 12px 16px;
  margin-bottom: 4px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #a1a1aa;
  transition: all 0.2s;
}
.session-item:hover {
  background: rgba(255, 255, 255, 0.05);
}
.session-item.active {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
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
  color: #ef4444;
  cursor: pointer;
  opacity: 0;
  font-size: 16px;
  padding: 0 4px;
}
.session-item:hover .delete-btn {
  opacity: 1;
}

.main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 420px;
  gap: 12px;
  padding: 12px;
}
.left,
.right {
  min-height: 0;
}
.left {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  overflow: hidden;
}
.right {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.panelHeader {
  padding: 12px 12px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.panelTitle {
  font-weight: 700;
}
.panelHint {
  margin-top: 6px;
  opacity: 0.7;
  font-size: 12px;
  line-height: 1.4;
}
.chat {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.messages {
  flex: 1;
  overflow: auto;
  padding: 14px;
}
.config {
  height: 100%;
  overflow: auto;
  padding: 14px;
}
@media (max-width: 980px) {
  .main {
    grid-template-columns: 1fr;
  }
  .right {
    height: 360px;
  }
}
</style>
