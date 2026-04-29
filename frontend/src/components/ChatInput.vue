<template>
  <div class="wrap">
    <div class="attachments" v-if="attachments.length > 0">
      <div class="att-item" v-for="(att, idx) in attachments" :key="idx">
        <span class="att-name">{{ att.name }}</span>
        <button class="att-del" @click="removeAttachment(idx)">×</button>
      </div>
    </div>
    <div class="row">
      <label class="label">模式</label>
      <select class="select" :disabled="busy" :value="mode" @change="$emit('update:mode', $event.target.value)">
        <option value="refine">refine（精化轨 / 默认）</option>
        <option value="auto">auto（自动）</option>
        <option value="fast">fast（快速轨）</option>
      </select>
      <div class="spacer" />
      <input type="file" ref="fileInput" @change="onFileChange" style="display: none" />
      <button class="btn" :disabled="busy" @click="$refs.fileInput.click()">📎</button>
      <button class="btn stop" v-if="busy" @click="$emit('stop')">停止响应</button>
      <button class="btn" :disabled="busy || (!draft.trim() && attachments.length === 0)" @click="send">发送</button>
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
  emits: ["send", "stop", "update:mode"],
  data() {
    return { draft: "", attachments: [] };
  },
  methods: {
    onFileChange(e) {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        this.attachments.push({
          name: file.name,
          type: file.type,
          data: ev.target.result, // data URL for image or text content for text
        });
      };
      if (file.type.startsWith('image/')) {
        reader.readAsDataURL(file);
      } else {
        reader.readAsText(file);
      }
      this.$refs.fileInput.value = '';
    },
    removeAttachment(idx) {
      this.attachments.splice(idx, 1);
    },
    send() {
      const t = this.draft.trim();
      if (!t && this.attachments.length === 0) return;
      if (this.busy) return;

      let payload = t;
      if (this.attachments.length > 0) {
        payload = [];
        if (t) {
          payload.push({ type: "text", text: t });
        }
        for (const att of this.attachments) {
          if (att.type.startsWith('image/')) {
            payload.push({ type: "image_url", image_url: { url: att.data } });
          } else {
            payload.push({ type: "text", text: `\n\n[文件: ${att.name}]\n\`\`\`\n${att.data}\n\`\`\`\n` });
          }
        }
      }
      
      this.$emit("send", payload);
      this.draft = "";
      this.attachments = [];
    },
  },
};
</script>

<style scoped>
.attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.att-item {
  display: flex;
  align-items: center;
  background: rgba(255, 255, 255, 0.1);
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 12px;
  color: #e6e9f2;
}
.att-name {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.att-del {
  background: none;
  border: none;
  color: #fca5a5;
  margin-left: 6px;
  cursor: pointer;
  font-size: 14px;
}
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
.btn.stop {
  border-color: rgba(239, 68, 68, 0.5);
  background: rgba(239, 68, 68, 0.18);
  color: #fca5a5;
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

