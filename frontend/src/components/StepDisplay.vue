<template>
  <div class="wrap">
    <div class="summary">
      <div class="k">
        <div class="label">Trace ID</div>
        <div class="value mono">{{ traceId || "-" }}</div>
      </div>
      <div class="k">
        <div class="label">Track</div>
        <div class="value track-badge" :class="track">{{ track || "-" }}</div>
      </div>
      <div class="k">
        <div class="label">Steps</div>
        <div class="value">{{ (steps && steps.length) || 0 }}</div>
      </div>
    </div>

    <div class="list">
      <div v-if="!steps || steps.length === 0" class="empty">发送问题后，这里会显示每一步执行细节。</div>

      <div class="timeline">
        <div v-for="(s, idx) in steps" :key="idx" class="timeline-item">
          <div class="timeline-marker" :class="s.status"></div>
          <div class="card">
            <div class="row">
              <div class="name">{{ s.name }}</div>
              <div class="status-icon" :class="s.status">
                <span v-if="s.status === 'ok'">✓</span>
                <span v-else-if="s.status === 'error'">✗</span>
                <span v-else>•</span>
              </div>
            </div>

            <div class="chips" v-if="s.provider || s.model || s.latency_ms != null">
              <span v-if="s.provider" class="chip provider">{{ s.provider }}</span>
              <span v-if="s.model" class="chip model">{{ s.model }}</span>
              <span v-if="s.latency_ms != null" class="chip latency">{{ s.latency_ms }}ms</span>
            </div>

            <div class="details-group">
              <details v-if="s.meta && Object.keys(s.meta).length" class="block">
                <summary>Meta Data</summary>
                <pre class="mono">{{ pretty(s.meta) }}</pre>
              </details>

              <details v-if="s.input_preview" class="block">
                <summary>Input Preview</summary>
                <pre class="mono">{{ s.input_preview }}</pre>
              </details>

              <details v-if="s.output" class="block">
                <summary>Output</summary>
                <pre class="mono">{{ s.output }}</pre>
              </details>
            </div>

            <div v-if="s.error" class="error">
              {{ s.error }}
            </div>
          </div>
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
  padding: 16px;
  background: rgba(0, 0, 0, 0.1);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.k {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.label {
  opacity: 0.6;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.value {
  font-weight: 600;
  font-size: 13px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}
.track-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.1);
}
.track-badge.fast {
  background: rgba(56, 189, 248, 0.15);
  color: #38bdf8;
}
.track-badge.refine {
  background: rgba(167, 139, 250, 0.15);
  color: #a78bfa;
}
.list {
  padding: 16px;
  overflow: auto;
  min-height: 0;
}
.empty {
  opacity: 0.5;
  font-size: 13px;
  text-align: center;
  padding: 32px 0;
}
.timeline {
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
}
.timeline::before {
  content: '';
  position: absolute;
  left: 11px;
  top: 10px;
  bottom: 10px;
  width: 2px;
  background: rgba(255, 255, 255, 0.05);
  z-radius: 2px;
}
.timeline-item {
  display: flex;
  gap: 16px;
  position: relative;
}
.timeline-marker {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #1e293b;
  border: 2px solid rgba(255, 255, 255, 0.2);
  flex-shrink: 0;
  z-index: 1;
  margin-top: 6px;
}
.timeline-marker.ok {
  border-color: #10b981;
  background: rgba(16, 185, 129, 0.1);
}
.timeline-marker.error {
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}
.card {
  flex: 1;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
  border-radius: 8px;
  padding: 12px;
  transition: background 0.2s;
  min-width: 0;
}
.card:hover {
  background: rgba(255, 255, 255, 0.04);
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}
.name {
  font-weight: 600;
  font-size: 14px;
  color: #f8fafc;
}
.status-icon {
  font-size: 14px;
  font-weight: bold;
}
.status-icon.ok { color: #10b981; }
.status-icon.error { color: #ef4444; }

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.05);
  color: #cbd5e1;
  border: 1px solid rgba(255, 255, 255, 0.1);
}
.chip.provider { border-color: rgba(168, 85, 247, 0.3); color: #c084fc; }
.chip.model { border-color: rgba(56, 189, 248, 0.3); color: #7dd3fc; }
.chip.latency { border-color: rgba(52, 211, 153, 0.3); color: #6ee7b7; font-family: ui-monospace, monospace; }

.details-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.block summary {
  cursor: pointer;
  opacity: 0.7;
  font-size: 12px;
  user-select: none;
  transition: opacity 0.2s;
  padding: 4px 0;
}
.block summary:hover {
  opacity: 1;
}
.block pre {
  margin: 6px 0 0 0;
  padding: 10px;
  border-radius: 6px;
  background: #0f172a;
  border: 1px solid rgba(255, 255, 255, 0.05);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.5;
  color: #94a3b8;
  max-height: 200px;
  overflow-y: auto;
}
.error {
  margin-top: 10px;
  padding: 10px;
  border-radius: 6px;
  border: 1px solid rgba(239, 68, 68, 0.3);
  background: rgba(239, 68, 68, 0.1);
  color: #fca5a5;
  font-size: 12px;
  line-height: 1.5;
}
</style>

