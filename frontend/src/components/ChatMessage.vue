<template>
  <div class="msg" :class="{ user: role === 'user', assistant: role !== 'user' }">
    <div class="bubble" :class="{ 'user-bubble': role === 'user' }">
      <div class="meta" v-if="role !== 'user' && meta && !meta.pending">
        <span v-if="meta.track" class="pill">{{ meta.track }}</span>
        <span v-if="meta.provider" class="pill">{{ meta.provider }}</span>
        <span v-if="meta.model" class="pill">{{ meta.model }}</span>
        <span v-if="meta.latency_ms != null" class="pill">{{ meta.latency_ms }}ms</span>
        <span v-if="meta.streaming" class="pill active">生成中</span>
        <span v-if="meta.success === false" class="pill danger">失败</span>
      </div>

      <div class="content markdown-body">
        <div v-if="Array.isArray(content)" class="multimodal-content">
          <template v-for="(part, idx) in content" :key="idx">
            <div v-if="part.type === 'text'" v-html="renderMarkdown(part.text)"></div>
            <img v-if="part.type === 'image_url'" :src="part.image_url.url" class="chat-img" alt="" />
          </template>
        </div>
        <div v-else v-html="renderedContent"></div>
      </div>

      <div class="actions" v-if="!meta?.pending && !meta?.streaming && role !== 'system'">
        <button class="action-btn" v-if="role === 'user'" @click="$emit('edit')">编辑</button>
        <button class="action-btn" v-if="role === 'assistant'" @click="$emit('regenerate')">重新生成</button>
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
      if (typeof this.content === "string") return this.content;
      return "";
    },
    renderedContent() {
      return this.renderMarkdown(this.textContent || "");
    },
  },
  methods: {
    renderMarkdown(text) {
      if (!text) return "";
      const rawHtml = marked(text);
      return DOMPurify.sanitize(rawHtml);
    },
  },
};
</script>

<style scoped>
.msg {
  display: flex;
  margin: 18px 0;
}
.msg.user {
  justify-content: flex-end;
}
.msg.assistant {
  justify-content: flex-start;
}
.bubble {
  max-width: min(92%, 720px);
}
.bubble.user-bubble {
  background: #2f3a4d;
  padding: 10px 16px;
  border-radius: 18px;
  border-bottom-right-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
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
.content {
  line-height: 1.7;
  font-size: 15px;
  color: #e2e8f0;
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
.actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  opacity: 0;
  transition: opacity 0.15s;
}
.msg:hover .actions {
  opacity: 1;
}
.action-btn {
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #232a38;
  color: #94a3b8;
  cursor: pointer;
}
.action-btn:hover {
  border-color: rgba(129, 140, 248, 0.45);
  color: #c7d2fe;
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
.content.markdown-body :deep(pre) {
  background: #161b26;
  padding: 10px 12px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13px;
  border: 1px solid #2f3a4d;
}
.content.markdown-body :deep(pre code) {
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
</style>
