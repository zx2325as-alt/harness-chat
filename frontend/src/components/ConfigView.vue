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
}
.spacer {
  flex: 1;
}
.btn {
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  color: #e6e9f2;
  cursor: pointer;
}
.hint {
  opacity: 0.7;
  font-size: 13px;
}
.error {
  padding: 12px;
  border-radius: 12px;
  border: 1px solid rgba(255, 77, 109, 0.55);
  background: rgba(255, 77, 109, 0.12);
  line-height: 1.5;
}
.grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}
.card {
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  padding: 12px;
}
.cardTitle {
  font-weight: 700;
  margin-bottom: 8px;
}
.cardValue {
  font-weight: 700;
  opacity: 0.9;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.5;
  opacity: 0.92;
}
</style>

