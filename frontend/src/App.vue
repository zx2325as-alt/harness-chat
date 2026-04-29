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

    <main class="main">
      <section class="left">
        <div v-if="view === 'chat'" class="chat">
          <div class="messages" ref="msgRef">
            <ChatMessage
              v-for="m in messages"
              :key="m.id"
              :role="m.role"
              :content="m.content"
              :meta="m.meta"
            />
          </div>
          <ChatInput
            :busy="busy"
            :mode="mode"
            @update:mode="mode = $event"
            @send="onSend"
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
      mode: "auto",
      busy: false,
      messages: [
        {
          id: "sys-1",
          role: "assistant",
          content:
            "你好！这是“双轨 Harness”网页端演示。\n\n- auto：自动判断复杂度（默认）\n- fast：快速轨（单模型路由+降级）\n- refine：精化轨（三层链：初稿/审查/润色）\n\n你可以直接输入问题并观察右侧每一步。",
        },
      ],
      latestSteps: [],
      latestTraceId: "",
      latestTrack: "",
      config: null,
      configLoading: false,
      configError: "",
    };
  },
  mounted() {
    this.loadConfig();
  },
  methods: {
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
    async onSend(text) {
      if (!text || this.busy) return;

      const userMsg = { id: `u-${Date.now()}`, role: "user", content: text };
      this.messages.push(userMsg);
      this.scrollToBottom();

      this.busy = true;
      this.latestSteps = [];
      this.latestTraceId = "";
      this.latestTrack = "";

      const pending = {
        id: `a-pending-${Date.now()}`,
        role: "assistant",
        content: "处理中…",
        meta: { pending: true },
      };
      this.messages.push(pending);
      this.scrollToBottom();

      try {
        const r = await fetch(`${API_BASE}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: text, mode: this.mode, options: {} }),
        });
        if (!r.ok) {
          const t = await r.text();
          throw new Error(`HTTP ${r.status}: ${t}`);
        }
        const data = await r.json();
        this.latestSteps = data.steps || [];
        this.latestTraceId = data.trace_id || "";
        this.latestTrack = data.track || "";

        const final = data.final || {};
        const content = final.content || "(空)";
        const meta = {
          track: data.track,
          provider: final.provider,
          model: final.model,
          latency_ms: final.latency_ms,
          success: final.success,
        };

        const idx = this.messages.findIndex((m) => m.id === pending.id);
        const msg = { id: `a-${Date.now()}`, role: "assistant", content, meta };
        if (idx >= 0) this.messages.splice(idx, 1, msg);
        else this.messages.push(msg);
      } catch (e) {
        const errText = String(e && e.message ? e.message : e);
        const idx = this.messages.findIndex((m) => m.id === pending.id);
        const msg = { id: `a-err-${Date.now()}`, role: "assistant", content: `请求失败：${errText}`, meta: { error: true } };
        if (idx >= 0) this.messages.splice(idx, 1, msg);
        else this.messages.push(msg);
      } finally {
        this.busy = false;
        this.scrollToBottom();
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
