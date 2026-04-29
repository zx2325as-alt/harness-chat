<template>
  <div class="msg" :class="{ user: role === 'user', assistant: role !== 'user' }">
    <div class="bubble">
      <div class="meta" v-if="role !== 'user' && meta">
        <span v-if="meta.track" class="pill">{{ meta.track }}</span>
        <span v-if="meta.provider" class="pill">{{ meta.provider }}</span>
        <span v-if="meta.model" class="pill">{{ meta.model }}</span>
        <span v-if="meta.latency_ms != null" class="pill">{{ meta.latency_ms }}ms</span>
        <span v-if="meta.streaming" class="pill active">正在生成...</span>
        <span v-if="meta.success === false" class="pill danger">失败</span>
      </div>
      
      <div v-if="meta && meta.reasoning_content" class="reasoning">
        <details>
          <summary>深度思考 <span v-if="meta.streaming" class="dots">...</span></summary>
          <div class="reasoning-content markdown-body" v-html="renderedReasoning"></div>
        </details>
      </div>

      <div class="content markdown-body">
        <div v-if="Array.isArray(content)" class="multimodal-content">
          <template v-for="(part, idx) in content" :key="idx">
            <div v-if="part.type === 'text'" v-html="renderMarkdown(part.text)"></div>
            <img v-if="part.type === 'image_url'" :src="part.image_url.url" class="chat-img" />
          </template>
        </div>
        <div v-else v-html="renderedContent"></div>
      </div>
      
      <div class="actions" v-if="!meta?.pending && !meta?.streaming && role !== 'system'">
        <button class="action-btn" v-if="role === 'user'" @click="$emit('edit')">✏️ 编辑</button>
        <button class="action-btn" v-if="role === 'assistant'" @click="$emit('regenerate')">🔄 重新生成</button>
      </div>
    </div>
  </div>
</template>

<script>
import { marked } from "marked";
import DOMPurify from "dompurify";

export default {
  name: "ChatMessage",
  props: {
    role: { type: String, required: true },
    content: { type: [String, Array], required: true },
    meta: { type: Object, default: null },
  },
  emits: ["edit", "regenerate"],
  computed: {
    textContent() {
      if (typeof this.content === 'string') return this.content;
      return "";
    },
    renderedContent() {
      return this.renderMarkdown(this.textContent || "");
    },
    renderedReasoning() {
      if (!this.meta || !this.meta.reasoning_content) return "";
      return this.renderMarkdown(this.meta.reasoning_content);
    }
  },
  methods: {
    renderMarkdown(text) {
      if (!text) return "";
      const rawHtml = marked(text);
      return DOMPurify.sanitize(rawHtml);
    }
  }
};
</script>

<style scoped>
.msg {
  display: flex;
  margin: 10px 0;
}
.msg.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 880px;
  border-radius: 14px;
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.04);
}
.msg.user .bubble {
  background: rgba(124, 92, 255, 0.12);
  border-color: rgba(124, 92, 255, 0.28);
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.pill {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  opacity: 0.9;
}
.pill.danger {
  border-color: rgba(255, 77, 109, 0.55);
  background: rgba(255, 77, 109, 0.15);
}
.pill.active {
  border-color: rgba(124, 92, 255, 0.55);
  background: rgba(124, 92, 255, 0.15);
  color: #c7b9ff;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0% { opacity: 0.6; }
  50% { opacity: 1; }
  100% { opacity: 0.6; }
}
.reasoning {
  margin-bottom: 12px;
}
.reasoning details {
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-left: 3px solid rgba(124, 92, 255, 0.5);
  border-radius: 6px;
  overflow: hidden;
}
.reasoning summary {
  padding: 8px 12px;
  cursor: pointer;
  font-size: 13px;
  color: #a1a1aa;
  user-select: none;
  outline: none;
}
.reasoning summary:hover {
  background: rgba(255, 255, 255, 0.02);
  color: #d4d4d8;
}
.reasoning-content {
  margin: 0;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
  font-size: 13px;
  color: #a1a1aa;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  background: rgba(0, 0, 0, 0.3);
}
.dots {
  display: inline-block;
  animation: dots 1.5s infinite steps(4, end);
  width: 1em;
  text-align: left;
}
@keyframes dots {
  0% { content: ""; }
  25% { content: "."; }
  50% { content: ".."; }
  75% { content: "..."; }
  100% { content: ""; }
}
.content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
  font-size: 14px;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  opacity: 0;
  transition: opacity 0.2s;
}
.bubble:hover .actions {
  opacity: 1;
}
.action-btn {
  background: none;
  border: none;
  color: #a1a1aa;
  cursor: pointer;
  font-size: 12px;
  padding: 4px;
  border-radius: 4px;
}
.action-btn:hover {
  background: rgba(255, 255, 255, 0.05);
  color: #fff;
}
</style>

