<template>
  <div class="wrap">
    <div class="row">
      <label class="label">模式</label>
      <select class="select" :disabled="busy" :value="mode" @change="$emit('update:mode', $event.target.value)">
        <option value="auto">auto（自动）</option>
        <option value="fast">fast（快速轨）</option>
        <option value="refine">refine（精化轨）</option>
      </select>
      <div class="spacer" />
      <button class="btn" :disabled="busy || !draft.trim()" @click="send">发送</button>
    </div>
    <textarea
      class="input"
      :disabled="busy"
      v-model="draft"
      placeholder="输入你的问题（可用 /refine 强制精化轨）…"
      @keydown.enter.exact.prevent="send"
    />
    <div class="hint">
      <span v-if="busy">后端处理中…</span>
      <span v-else>Enter 发送；Shift+Enter 换行</span>
    </div>
  </div>
</template>

<script>
export default {
  name: "ChatInput",
  props: {
    busy: { type: Boolean, default: false },
    mode: { type: String, default: "auto" },
  },
  emits: ["send", "update:mode"],
  data() {
    return { draft: "" };
  },
  methods: {
    send() {
      const t = this.draft.trim();
      if (!t || this.busy) return;
      this.$emit("send", t);
      this.draft = "";
    },
  },
};
</script>

<style scoped>
.wrap {
  padding: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.label {
  opacity: 0.8;
  font-size: 12px;
}
.select {
  background: rgba(255, 255, 255, 0.06);
  color: #e6e9f2;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 10px;
  padding: 8px 10px;
}
.spacer {
  flex: 1;
}
.btn {
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid rgba(124, 92, 255, 0.5);
  background: rgba(124, 92, 255, 0.18);
  color: #e6e9f2;
  cursor: pointer;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.input {
  width: 100%;
  min-height: 92px;
  resize: vertical;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(0, 0, 0, 0.25);
  color: #e6e9f2;
  outline: none;
  line-height: 1.5;
}
.hint {
  margin-top: 8px;
  opacity: 0.7;
  font-size: 12px;
}
</style>

