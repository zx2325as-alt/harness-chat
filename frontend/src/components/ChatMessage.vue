<template>
  <div class="msg" :class="{ user: role === 'user', assistant: role !== 'user' }">
    <div class="msg-stack">
      <div class="bubble" :class="{ 'user-bubble': role === 'user' }">
        <div class="meta" v-if="role !== 'user' && meta && (!meta.pending || meta.streaming)">
          <span v-if="meta.track" class="pill" :title="metaTitle">{{ meta.track }}</span>
          <span v-if="meta.provider" class="pill">{{ meta.provider }}</span>
          <span v-if="meta.model" class="pill" :title="'末段模型: ' + meta.model">{{ meta.model }}</span>
          <span v-if="meta.latency_ms != null" class="pill">{{ meta.latency_ms }}ms</span>
          <span v-if="meta.streaming" class="pill active">生成中</span>
          <span v-if="meta.success === false" class="pill danger">失败</span>
        </div>
        <div v-if="role === 'assistant' && meta?.model_chain && !meta?.pending" class="model-chain">
          {{ meta.model_chain }}
        </div>

        <ThinkingPane
          v-if="role === 'assistant' && liveRun && showThinkingPane"
          :run="liveRun"
          :tick="stepUiTick"
          :streaming="Boolean(meta?.streaming)"
          :latency-ms="meta?.latency_ms != null ? meta.latency_ms : null"
        />

        <div class="content markdown-body">
          <div v-if="Array.isArray(content)" class="multimodal-content">
            <template v-for="(part, idx) in content" :key="idx">
              <div v-if="part.type === 'text'" v-html="safeMarkdown(part.text)"></div>
              <img v-if="part.type === 'image_url'" :src="part.image_url.url" class="chat-img" alt="" />
            </template>
          </div>
          <!-- 流式阶段：不用 code-block 的 pre 样式；空内容时不渲染大块深色 pre（避免「黑框」） -->
          <div v-else-if="meta?.streaming" class="stream-live">
            <pre v-if="textContent.trim()" class="stream-plain">{{ textContent }}</pre>
            <p v-else class="stream-placeholder">等待模型输出…</p>
          </div>
          <div v-else v-html="renderedContent"></div>
        </div>
      </div>

      <!-- 参考网页聊天：操作区在气泡下方；用户消息右对齐 -->
      <div
        v-if="showUnderActions"
        class="under-actions"
        :class="{ 'under-user': role === 'user', 'under-assistant': role !== 'user' }"
      >
        <template v-if="role === 'user'">
          <button type="button" class="under-btn" title="复制" aria-label="复制" @click="onCopy">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" />
              <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
            </svg>
          </button>
          <button type="button" class="under-btn under-btn-text" title="编辑" @click="$emit('edit')">编辑</button>
        </template>
        <template v-else>
          <button type="button" class="under-btn" title="复制全文" aria-label="复制" @click="onCopy">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" />
              <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
            </svg>
          </button>
          <button type="button" class="under-btn under-btn-text" @click="$emit('regenerate')">重新生成</button>
          <button v-if="meta?.stopped" type="button" class="under-btn under-btn-text" @click="$emit('retry')">
            重试
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<script>
import { renderMarkdownWithMath } from "../markdownMath.js";
import ThinkingPane from "./ThinkingPane.vue";

export default {
  name: "ChatMessage",
  components: { ThinkingPane },
  props: {
    role: { type: String, required: true },
    content: { type: [String, Array], required: true },
    meta: { type: Object, default: null },
    /** 与本轮助手消息绑定的执行过程（用于气泡内「思考过程」） */
    liveRun: { type: Object, default: null },
    stepUiTick: { type: Number, default: 0 },
  },
  emits: ["edit", "regenerate", "copy", "retry"],
  computed: {
    metaTitle() {
      const m = this.meta || {};
      const parts = [];
      if (m.track) parts.push("轨道: " + m.track);
      if (m.model_chain) parts.push(m.model_chain);
      return parts.join("\n") || "";
    },
    textContent() {
      if (typeof this.content === "string") return this.content;
      return "";
    },
    renderedContent() {
      return this.safeMarkdown(this.textContent || "");
    },
    showThinkingPane() {
      if (!this.liveRun) return false;
      // 流式期间助手消息一直保持 meta.pending=true，若据此隐藏则气泡内「思考过程」永远不出现
      if (this.meta?.pending && !this.meta?.streaming) return false;
      const steps = this.liveRun.steps || [];
      return (
        steps.length > 0 ||
        this.liveRun.status === "running" ||
        Boolean(this.meta?.streaming)
      );
    },
    showUnderActions() {
      return !this.meta?.pending && !this.meta?.streaming && this.role !== "system";
    },
  },
  methods: {
    escapeHtml(s) {
      return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    },
    safeMarkdown(text) {
      try {
        return renderMarkdownWithMath(text || "");
      } catch (e) {
        console.warn("ChatMessage: markdown render failed", e);
        const t = this.escapeHtml(text || "");
        return `<pre class="md-fallback">${t}</pre>`;
      }
    },
    renderMarkdown(text) {
      return this.safeMarkdown(text || "");
    },
    plainTextFromContent() {
      const c = this.content;
      if (typeof c === "string") return c;
      if (!Array.isArray(c)) return "";
      return c
        .filter((p) => p && p.type === "text")
        .map((p) => p.text || "")
        .join("\n");
    },
    onCopy() {
      const t = this.plainTextFromContent();
      if (!t) return;
      navigator.clipboard?.writeText(t).then(
        () => this.$emit("copy", { chars: t.length }),
        () => {}
      );
    },
  },
};
</script>

<style scoped>
.msg {
  display: flex;
  margin: 14px 0;
}
.msg.user {
  justify-content: flex-end;
}
.msg.assistant {
  justify-content: flex-start;
}
.msg-stack {
  display: flex;
  flex-direction: column;
  max-width: 100%;
  align-items: stretch;
}
.msg.user .msg-stack {
  align-items: flex-end;
}
.bubble {
  max-width: min(88%, 680px);
}
.msg.assistant .bubble:not(.user-bubble) {
  padding: 0 4px 0 0;
}
.msg.user .bubble {
  max-width: min(82%, 520px);
}
.bubble.user-bubble {
  background: #2d3a52;
  padding: 8px 14px;
  border-radius: 16px;
  border-bottom-right-radius: 5px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.18);
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.pill {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: none;
  background: #2f3a4d;
  color: #94a3b8;
}
.pill.danger {
  background: rgba(248, 113, 113, 0.12);
  color: #fca5a5;
}
.pill.active {
  background: rgba(99, 102, 241, 0.22);
  color: #c7d2fe;
}
.model-chain {
  font-size: 11px;
  color: #64748b;
  margin: -4px 0 10px;
  line-height: 1.45;
  max-width: 100%;
  word-break: break-word;
}
.content {
  line-height: 1.65;
  font-size: 14px;
  color: #e2e8f0;
}
.stream-live {
  margin-top: 2px;
}
.stream-placeholder {
  margin: 0;
  padding: 2px 0;
  font-size: 13px;
  color: #64748b;
}
.stream-plain {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
  color: inherit;
  background: transparent;
  border: none;
  padding: 0;
}
.msg.user .content {
  color: #f1f5f9;
}
.msg.assistant .content {
  color: #cbd5e1;
}
.chat-img {
  max-width: 100%;
  border-radius: 10px;
  margin-top: 10px;
}

.under-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
  padding: 0 2px;
}
.under-user {
  justify-content: flex-end;
}
.under-assistant {
  justify-content: flex-start;
}
.under-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  height: 28px;
  padding: 0 6px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
}
.under-btn:hover {
  color: #94a3b8;
  background: rgba(255, 255, 255, 0.06);
}
.under-btn-text {
  min-width: unset;
  padding: 0 8px;
  font-size: 12px;
  color: #64748b;
}
.under-btn-text:hover {
  color: #a5b4fc;
}

.content.markdown-body :deep(h1),
.content.markdown-body :deep(h2),
.content.markdown-body :deep(h3) {
  color: #f1f5f9;
  margin: 1em 0 0.5em;
}
.content.markdown-body :deep(p) {
  margin: 0.55em 0;
}
.content.markdown-body :deep(ul),
.content.markdown-body :deep(ol) {
  margin: 0.5em 0;
  padding-left: 1.35em;
}
.content.markdown-body :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.88em;
  background: #1e2433;
  padding: 0.15em 0.35em;
  border-radius: 4px;
  color: #a5b4fc;
}
/* 勿作用于流式 plain 文本 pre（.stream-plain），否则空块也会出现代码块样式「黑框」 */
.content.markdown-body :deep(pre:not(.stream-plain)) {
  background: #161b26;
  padding: 10px 12px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13px;
  border: 1px solid #2f3a4d;
}
.content.markdown-body :deep(pre:not(.stream-plain) code) {
  background: transparent;
  padding: 0;
}
.content.markdown-body :deep(blockquote) {
  margin: 0.75em 0;
  padding-left: 14px;
  border-left: 3px solid #3d4d64;
  color: #94a3b8;
}
.content.markdown-body :deep(a) {
  color: #a5b4fc;
}

.content.markdown-body :deep(.katex),
.content.markdown-body :deep(.katex-display) {
  color: #e8eef7;
}
.content.markdown-body :deep(.katex .mord),
.content.markdown-body :deep(.katex .mrel),
.content.markdown-body :deep(.katex .mbin),
.content.markdown-body :deep(.katex .mop) {
  color: inherit;
}
.content.markdown-body :deep(.katex-display) {
  margin: 0.85em 0;
  overflow-x: auto;
  overflow-y: hidden;
  max-width: 100%;
}
.content.markdown-body :deep(.katex-fallback) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
  color: #fca5a5;
}
</style>
