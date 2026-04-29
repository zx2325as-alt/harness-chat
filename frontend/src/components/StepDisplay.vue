<template>
  <div class="wrap">
    <div class="summary">
      <div class="k">
        <div class="label">trace_id</div>
        <div class="value mono">{{ traceId || "-" }}</div>
      </div>
      <div class="k">
        <div class="label">track</div>
        <div class="value">{{ track || "-" }}</div>
      </div>
      <div class="k">
        <div class="label">steps</div>
        <div class="value">{{ (steps && steps.length) || 0 }}</div>
      </div>
    </div>

    <div class="list">
      <div v-if="!steps || steps.length === 0" class="empty">发送问题后，这里会显示每一步执行细节。</div>

      <div v-for="(s, idx) in steps" :key="idx" class="card">
        <div class="row">
          <div class="name">{{ s.name }}</div>
          <div class="status" :class="s.status">{{ s.status }}</div>
        </div>

        <div class="chips">
          <span v-if="s.provider" class="chip">{{ s.provider }}</span>
          <span v-if="s.model" class="chip">{{ s.model }}</span>
          <span v-if="s.latency_ms != null" class="chip">{{ s.latency_ms }}ms</span>
        </div>

        <details v-if="s.meta && Object.keys(s.meta).length" class="block">
          <summary>meta</summary>
          <pre class="mono">{{ pretty(s.meta) }}</pre>
        </details>

        <details v-if="s.input_preview" class="block">
          <summary>input preview</summary>
          <pre class="mono">{{ s.input_preview }}</pre>
        </details>

        <details v-if="s.output" class="block">
          <summary>output</summary>
          <pre class="mono">{{ s.output }}</pre>
        </details>

        <div v-if="s.error" class="error">
          {{ s.error }}
        </div>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: "StepDisplay",
  props: {
    traceId: { type: String, default: "" },
    track: { type: String, default: "" },
    steps: { type: Array, default: () => [] },
  },
  methods: {
    pretty(obj) {
      try {
        return JSON.stringify(obj, null, 2);
      } catch {
        return String(obj);
      }
    },
  },
};
</script>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.summary {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.k {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.label {
  opacity: 0.75;
  font-size: 12px;
}
.value {
  font-weight: 600;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}
.list {
  padding: 12px;
  overflow: auto;
  min-height: 0;
}
.empty {
  opacity: 0.7;
  font-size: 13px;
  padding: 10px 8px;
}
.card {
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  padding: 10px 10px;
  margin-bottom: 10px;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.name {
  font-weight: 700;
}
.status {
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
}
.status.ok {
  border-color: rgba(94, 234, 212, 0.45);
  background: rgba(94, 234, 212, 0.12);
}
.status.error {
  border-color: rgba(255, 77, 109, 0.55);
  background: rgba(255, 77, 109, 0.15);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.chip {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  opacity: 0.9;
}
.block {
  margin-top: 10px;
}
.block summary {
  cursor: pointer;
  opacity: 0.9;
  font-size: 12px;
}
.block pre {
  margin: 8px 0 0 0;
  padding: 10px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.08);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.5;
}
.error {
  margin-top: 10px;
  padding: 10px;
  border-radius: 10px;
  border: 1px solid rgba(255, 77, 109, 0.55);
  background: rgba(255, 77, 109, 0.12);
  font-size: 12px;
  line-height: 1.5;
}
</style>

