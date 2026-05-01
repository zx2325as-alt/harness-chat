<template>
  <div class="wrap">
    <div v-if="runs.length" class="summary-bar">
      <span>{{ runs.length }} 轮对话</span>
      <span class="sep">·</span>
      <span class="track-pill">{{ latestRun?.track || "—" }}</span>
      <span class="sep">·</span>
      <span>共 {{ latestRun?.steps?.length || 0 }} 步</span>
    </div>

    <div class="list">
      <div v-if="!runs || runs.length === 0" class="empty">发送问题后，执行过程会显示在这里。</div>

      <section
        v-for="(run, idx) in runs"
        :key="`${run.id}-${run.steps?.length || 0}-${run.status}`"
        class="run-card"
        :class="{ open: isOpen(run, idx), running: run.status === 'running' }"
      >
        <button type="button" class="run-header" @click="toggle(run)">
          <div class="run-main">
            <div class="run-title">{{ run.title || "本轮执行" }}</div>
            <div class="run-sub">{{ formatTime(run.createdAt) }} · {{ shortTrace(run.traceId) }}</div>
          </div>
          <div class="run-badges">
            <span class="badge soft" :class="run.status">{{ statusText(run.status) }}</span>
            <span class="badge outline">{{ run.steps?.length || 0 }} 步</span>
            <span class="chev">{{ isOpen(run, idx) ? "收起" : "展开" }}</span>
          </div>
        </button>

        <div v-if="isOpen(run, idx)" class="run-body">
          <div v-if="run.documents && run.documents.length" class="doc-line">
            文档：
            <span v-for="d in run.documents" :key="d.name" class="doc-pill">{{ d.name }}</span>
          </div>

          <div class="steps-col">
            <details
              v-for="(s, stepIdx) in run.steps"
              :key="`${s.name}-${stepIdx}-${s.status}-${renderTick}-${(s.output || '').length}`"
              class="step-one"
              :open="stepFoldOpen(run, stepIdx, s)"
            >
              <summary class="step-sum">
                <span class="sum-chev" aria-hidden="true">◇</span>
                <span class="sum-name">{{ humanStepName(s.name) }}</span>
                <span class="sum-st" :class="s.status">{{ statusShort(s.status) }}</span>
                <span v-if="s.provider" class="sum-meta">{{ s.provider }}</span>
                <span v-if="s.latency_ms != null" class="sum-meta">{{ s.latency_ms }}ms</span>
              </summary>

              <div class="step-inner">
                <div v-if="inlineFacts(s).length" class="facts-muted">
                  <div v-for="(row, ri) in inlineFacts(s)" :key="ri" class="fact-line">
                    <span class="fk">{{ row.label }}</span>
                    <span class="fv">{{ row.value }}</span>
                  </div>
                </div>

                <div v-if="sourceList(s).length" class="src-block">
                  <div class="src-h">链接来源</div>
                  <ul class="src-ul">
                    <li v-for="(src, si) in sourceList(s)" :key="si">
                      <span class="src-n">[{{ src.index }}]</span>
                      <a :href="src.url" target="_blank" rel="noopener noreferrer" class="src-a">{{ src.title || "打开" }}</a>
                    </li>
                  </ul>
                </div>

                <template v-if="hasJsonMeta(s)">
                  <div class="sec-h">调度与元数据</div>
                  <pre class="sec-pre">{{ pretty(compactMeta(s.meta)) }}</pre>
                </template>

                <template v-if="s.input_preview">
                  <div class="sec-h">输入摘要</div>
                  <pre class="sec-pre sm">{{ s.input_preview }}</pre>
                </template>

                <template v-if="s.output">
                  <div class="sec-h">阶段输出</div>
                  <pre class="sec-pre">{{ s.output }}</pre>
                </template>

                <div v-if="s.error" class="err-line">{{ s.error }}</div>
              </div>
            </details>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
const STEP_LABELS = {
  complexity_analyze: "复杂度分析",
  track_select: "轨道选择",
  web_search: "联网搜索",
  fast_route: "快速轨路由",
  refine_layer1_draft: "初稿层",
  refine_layer2_review: "审查层",
  refine_layer3_polish: "润色层",
  refine_disabled_fallback_fast: "精化关闭 · 降级快速轨",
};

export default {
  name: "StepDisplay",
  props: {
    runs: { type: Array, default: () => [] },
    renderTick: { type: Number, default: 0 },
  },
  data() {
    return {
      /** 用户点击历史轮次时，只展开该 id；为空则按「最后一轮默认展开」规则 */
      manualOpenId: "",
      /** 用户手动收起的轮次 id（解决：点「收起」清空 manualOpenId 后最后一轮又被强制展开） */
      collapsedRunIds: [],
    };
  },
  computed: {
    latestRun() {
      void this.renderTick;
      return this.runs && this.runs.length ? this.runs[this.runs.length - 1] : null;
    },
  },
  watch: {
    runs(newRuns, oldRuns) {
      if ((newRuns?.length || 0) !== (oldRuns?.length || 0)) {
        this.manualOpenId = "";
        const ids = new Set((newRuns || []).map((r) => r.id));
        this.collapsedRunIds = (this.collapsedRunIds || []).filter((id) => ids.has(id));
      }
    },
  },
  methods: {
    humanStepName(name) {
      return STEP_LABELS[name] || name || "步骤";
    },
    shortTrace(t) {
      if (!t) return "trace …";
      return t.length > 18 ? `${t.slice(0, 14)}…` : t;
    },
    isOpen(run, idx) {
      if (this.manualOpenId) return this.manualOpenId === run.id;
      const isLast = idx === this.runs.length - 1;
      if (!isLast) return false;
      return !this.collapsedRunIds.includes(run.id);
    },
    toggle(run) {
      const lastId = this.runs.length ? this.runs[this.runs.length - 1].id : null;
      if (this.manualOpenId) {
        if (this.manualOpenId === run.id) {
          this.manualOpenId = "";
          if (lastId && run.id === lastId && !this.collapsedRunIds.includes(run.id)) {
            this.collapsedRunIds = [...this.collapsedRunIds, run.id];
          }
        } else {
          this.manualOpenId = run.id;
        }
        return;
      }
      if (lastId && run.id === lastId) {
        const i = this.collapsedRunIds.indexOf(run.id);
        if (i >= 0) {
          this.collapsedRunIds = this.collapsedRunIds.filter((id) => id !== run.id);
        } else {
          this.collapsedRunIds = [...this.collapsedRunIds, run.id];
        }
        return;
      }
      this.manualOpenId = run.id;
    },
    statusText(status) {
      if (status === "ok") return "完成";
      if (status === "error") return "失败";
      if (status === "stopped") return "已停止";
      return "进行中";
    },
    statusShort(st) {
      if (st === "ok") return "完成";
      if (st === "error") return "失败";
      return "⋯";
    },
    formatTime(value) {
      if (!value) return "—";
      try {
        return new Date(value).toLocaleString();
      } catch {
        return value;
      }
    },
    sourceList(step) {
      const m = step?.meta;
      if (!m || !Array.isArray(m.sources)) return [];
      return m.sources.filter((x) => x && (x.url || x.title));
    },
    compactMeta(meta) {
      if (!meta || typeof meta !== "object") return {};
      const o = { ...meta };
      if (typeof o.raw_llm_response === "string" && o.raw_llm_response.length > 400) {
        o.raw_llm_response = o.raw_llm_response.slice(0, 400) + "…（已截断）";
      }
      if (o.sources && Array.isArray(o.sources)) {
        o.sources = `共 ${o.sources.length} 条，见上方链接`;
      }
      if (o.results_preview && typeof o.results_preview === "string" && o.results_preview.length > 260) {
        o.results_preview = o.results_preview.slice(0, 260) + "…";
      }
      if (o.attempts && Array.isArray(o.attempts) && o.attempts.length > 5) {
        o.attempts = o.attempts.slice(0, 5).concat([{ note: `其余 ${o.attempts.length - 5} 条省略` }]);
      }
      return o;
    },
    pretty(obj) {
      try {
        return JSON.stringify(obj, null, 2);
      } catch {
        return String(obj);
      }
    },
    inlineFacts(step) {
      const rows = [];
      const m = step.meta || {};
      if (step.name === "web_search") {
        const q = m.query_effective || m.query;
        if (q) rows.push({ label: "查询", value: q });
        if (m.failure_code) rows.push({ label: "失败码", value: m.failure_code });
        if (m.degraded) rows.push({ label: "降级", value: "是" });
        if (m.result_count != null) rows.push({ label: "命中", value: String(m.result_count) });
      }
      return rows;
    },
    stepFoldOpen(run, stepIdx, step) {
      void this.renderTick;
      const runIdx = this.runs.findIndex((r) => r.id === run.id);
      const isLatestCard = runIdx >= 0 && runIdx === this.runs.length - 1;
      if (isLatestCard) return true;
      if (step.status === "running") return true;
      return stepIdx === (run.steps?.length || 0) - 1;
    },
    hasJsonMeta(step) {
      const m = step.meta;
      if (!m || typeof m !== "object") return false;
      return Object.keys(this.compactMeta(m)).length > 0;
    },
  },
};
</script>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
  height: 100%;
  background: #1c2230;
  color: #cbd5e1;
}
.summary-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 14px;
  font-size: 12px;
  color: #94a3b8;
  border-bottom: 1px solid #2f3a4d;
  background: #232a38;
}
.sep {
  opacity: 0.45;
}
.track-pill {
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.2);
  color: #a5b4fc;
  font-weight: 600;
}
.list {
  padding: 10px 12px 16px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.empty {
  color: #64748b;
  font-size: 13px;
  text-align: center;
  padding: 36px 12px;
}
.run-card {
  border: 1px solid #2f3a4d;
  border-radius: 12px;
  background: #232a38;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  overflow: hidden;
}
.run-card.running {
  border-color: rgba(129, 140, 248, 0.45);
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.15);
}
.run-header {
  width: 100%;
  border: 0;
  background: transparent;
  padding: 11px 14px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
  text-align: left;
}
.run-header:hover {
  background: rgba(255, 255, 255, 0.04);
}
.run-title {
  font-size: 14px;
  font-weight: 600;
  color: #f1f5f9;
}
.run-sub {
  margin-top: 4px;
  font-size: 11px;
  color: #64748b;
}
.run-badges {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.badge {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 999px;
}
.badge.soft {
  background: #2f3a4d;
  color: #cbd5e1;
}
.badge.soft.running {
  background: rgba(99, 102, 241, 0.22);
  color: #c7d2fe;
}
.badge.soft.error {
  background: rgba(248, 113, 113, 0.12);
  color: #fca5a5;
}
.badge.outline {
  border: 1px solid #3d4d64;
  color: #94a3b8;
  background: transparent;
}
.chev {
  font-size: 12px;
  color: #a5b4fc;
  font-weight: 500;
}
.run-body {
  padding: 0 10px 12px;
  max-height: min(54vh, 440px);
  overflow-y: auto;
}
.doc-line {
  font-size: 12px;
  color: #94a3b8;
  margin: 4px 4px 10px;
}
.doc-pill {
  display: inline-block;
  margin: 3px 4px 0 0;
  padding: 2px 8px;
  border-radius: 999px;
  background: #2f3a4d;
  color: #e2e8f0;
  font-size: 11px;
}
.steps-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.step-one {
  border: 1px solid #2f3a4d;
  border-radius: 10px;
  background: #1e2433;
  overflow: hidden;
}
.step-one[open] {
  border-color: #3d4d64;
  background: #252d3d;
}
.step-sum {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 10px 12px;
  font-size: 13px;
  color: #cbd5e1;
  user-select: none;
}
.step-sum::-webkit-details-marker {
  display: none;
}
.sum-chev {
  color: #64748b;
  font-size: 10px;
  transform: rotate(0deg);
  transition: transform 0.15s;
}
.step-one[open] .sum-chev {
  transform: rotate(90deg);
}
.sum-name {
  font-weight: 600;
  color: #f1f5f9;
}
.sum-st {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(52, 211, 153, 0.15);
  color: #6ee7b7;
}
.sum-st.error {
  background: rgba(248, 113, 113, 0.12);
  color: #fca5a5;
}
.sum-meta {
  font-size: 11px;
  color: #94a3b8;
}
.step-inner {
  padding: 0 12px 12px;
  border-top: 1px solid #2f3a4d;
}
.facts-muted {
  margin-top: 10px;
  font-size: 12px;
  color: #94a3b8;
}
.fact-line {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 8px;
  padding: 3px 0;
}
.fk {
  color: #64748b;
}
.fv {
  color: #cbd5e1;
  word-break: break-word;
}
.src-block {
  margin-top: 10px;
  padding: 8px 0;
}
.src-h {
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  margin-bottom: 6px;
}
.src-ul {
  margin: 0;
  padding-left: 1.15em;
  font-size: 12px;
  color: #cbd5e1;
}
.src-n {
  color: #64748b;
  margin-right: 4px;
}
.src-a {
  color: #a5b4fc;
  text-decoration: none;
  word-break: break-all;
}
.src-a:hover {
  text-decoration: underline;
}
.sec-h {
  margin-top: 12px;
  margin-bottom: 6px;
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.sec-pre {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #1a1f2b;
  border: none;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  line-height: 1.5;
  color: #94a3b8;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}
.sec-pre.sm {
  max-height: 120px;
}
.err-line {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(248, 113, 113, 0.12);
  color: #fca5a5;
  font-size: 12px;
}
</style>
