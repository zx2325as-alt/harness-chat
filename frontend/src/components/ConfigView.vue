<template>
  <div class="wrap">
    <div class="row">
      <div class="title">当前生效配置（后端 `config.yaml`）</div>
      <div class="spacer" />
      <button class="btn" :disabled="loading" @click="$emit('reload')">刷新</button>
    </div>

    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else class="grid">
      <div class="card">
        <div class="cardTitle">默认模式</div>
        <div class="cardValue">{{ config?.harness?.default_mode }}</div>
      </div>
      <div class="card">
        <div class="cardTitle">复杂度识别</div>
        <pre class="mono">{{ pretty(config?.harness?.complexity) }}</pre>
      </div>
      <div class="card">
        <div class="cardTitle">快速路由规则</div>
        <pre class="mono">{{ pretty(config?.harness?.routing) }}</pre>
      </div>
      <div class="card">
        <div class="cardTitle">精化链配置</div>
        <pre class="mono">{{ pretty(config?.harness?.refine_chain) }}</pre>
      </div>
      <div class="card">
        <div class="cardTitle">已注册模型 keys</div>
        <pre class="mono">{{ pretty(config?.models) }}</pre>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: "ConfigView",
  props: {
    config: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    error: { type: String, default: "" },
  },
  emits: ["reload"],
  methods: {
    pretty(obj) {
      try {
        return JSON.stringify(obj ?? null, null, 2);
      } catch {
        return String(obj);
      }
    },
  },
};
</script>

<style scoped>
.wrap {
  height: 100%;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.title {
  font-weight: 800;
  color: #f1f5f9;
}
.spacer {
  flex: 1;
}
.btn {
  padding: 8px 14px;
  border-radius: 10px;
  border: 1px solid #3d4d64;
  background: #232a38;
  color: #cbd5e1;
  cursor: pointer;
  font-weight: 500;
}
.btn:hover:not(:disabled) {
  border-color: #4b5c78;
  background: #2a3344;
}
.btn:disabled {
  opacity: 0.5;
}
.hint {
  color: #94a3b8;
  font-size: 13px;
}
.error {
  padding: 12px;
  border-radius: 12px;
  border: 1px solid rgba(248, 113, 113, 0.35);
  background: rgba(248, 113, 113, 0.1);
  color: #fca5a5;
  line-height: 1.5;
}
.grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}
.card {
  border: 1px solid #2f3a4d;
  background: #232a38;
  border-radius: 12px;
  padding: 14px;
}
.cardTitle {
  font-weight: 700;
  margin-bottom: 8px;
  color: #94a3b8;
  font-size: 13px;
}
.cardValue {
  font-weight: 700;
  color: #f1f5f9;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.5;
  color: #94a3b8;
}
</style>

