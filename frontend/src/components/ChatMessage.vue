<template>
  <div class="msg" :class="{ user: role === 'user', assistant: role !== 'user' }">
    <div class="bubble">
      <div class="meta" v-if="role !== 'user' && meta">
        <span v-if="meta.track" class="pill">{{ meta.track }}</span>
        <span v-if="meta.provider" class="pill">{{ meta.provider }}</span>
        <span v-if="meta.model" class="pill">{{ meta.model }}</span>
        <span v-if="meta.latency_ms != null" class="pill">{{ meta.latency_ms }}ms</span>
        <span v-if="meta.success === false" class="pill danger">失败</span>
      </div>
      <pre class="content">{{ content }}</pre>
    </div>
  </div>
</template>

<script>
export default {
  name: "ChatMessage",
  props: {
    role: { type: String, required: true },
    content: { type: String, required: true },
    meta: { type: Object, default: null },
  },
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
.content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
  font-size: 14px;
}
</style>

