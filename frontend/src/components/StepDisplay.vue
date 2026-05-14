<template>
  <div class="wrap">
    <div v-if="runs.length" class="summary-bar">
      <span>{{ runs.length }} 轮对话</span>
      <span class="sep">·</span>
      <span class="runtime-pill">{{ runtimeLabel(latestRun?.runtime) }}</span>
      <span class="sep">·</span>
      <span>共 {{ latestRun ? flatSteps(latestRun).length : 0 }} 步</span>
      <template v-if="latestRun?.phaseMessage">
        <span class="sep">·</span>
        <span class="phase-hint">{{ latestRun.phaseMessage }}</span>
      </template>
      <template v-if="runningElapsedSec > 0">
        <span class="sep">·</span>
        <span class="phase-hint">已进行 {{ runningElapsedSec }}s</span>
      </template>
    </div>

    <div class="list" ref="stepsList">
      <div v-if="!runs || runs.length === 0" class="empty">发送问题后，执行过程会显示在这里。</div>

      <section
        v-for="(run, idx) in runs"
        :key="run.id"
        class="run-card"
        :class="{ open: isOpen(run, idx), running: run.status === 'running' }"
      >
        <button type="button" class="run-header" @click="toggle(run)">
          <div class="run-main">
            <div class="run-title">{{ run.title || "本轮执行" }}</div>
            <div class="run-sub">{{ formatTime(run.createdAt) }} · {{ shortTrace(run.traceId) }}</div>
          </div>
          <div class="run-badges">
            <span class="badge soft runtime">{{ runtimeLabel(run.runtime) }}</span>
            <span class="badge soft" :class="run.status">{{ statusText(run.status) }}</span>
            <span class="badge outline">{{ flatSteps(run).length || 0 }} 步</span>
            <span class="chev">{{ isOpen(run, idx) ? "收起" : "展开" }}</span>
          </div>
        </button>

        <div v-if="isOpen(run, idx)" class="run-body">
          <div v-if="run.documents && run.documents.length" class="doc-line">
            文档：
            <span v-for="doc in run.documents" :key="doc.name" class="doc-pill">{{ doc.name }}</span>
          </div>

          <div class="phases-col">
            <section v-for="phase in groupedPhases(run)" :key="phase.key" class="phase-card">
              <div class="phase-head">
                <span class="phase-title">{{ phase.title }}</span>
                <span class="phase-count">{{ phase.steps.length }} 步</span>
              </div>

              <div class="steps-col">
                <details
                  v-for="item in phase.steps"
                  :key="`${run.id}-${item.globalIdx}-${item.step.name}-${item.step.status}-${renderTick}`"
                  class="step-one"
                  :open="stepFoldOpen(run, item.step, item.globalIdx)"
                >
                  <summary class="step-sum">
                    <span class="sum-chev" aria-hidden="true">◇</span>
                    <span class="sum-name">{{ stepName(item.step) }}</span>
                    <span class="sum-st" :class="item.step.status">{{ statusShort(item.step.status) }}</span>
                    <span v-if="item.step.provider" class="sum-meta">{{ item.step.provider }}</span>
                    <span v-if="item.step.latency_ms != null" class="sum-meta">{{ item.step.latency_ms }}ms</span>
                  </summary>

                  <div class="step-inner">
                    <p v-if="item.step.meta?.event_summary" class="event-summary">
                      {{ item.step.meta.event_summary }}
                    </p>

                    <div v-if="workflowLines(item.step).length" class="flow-block">
                      <div class="flow-h">技术细节</div>
                      <ul class="flow-ul">
                        <li v-for="(row, ri) in workflowLines(item.step)" :key="ri">
                          <span class="flow-k">{{ row.label }}</span>
                          <span class="flow-v">{{ row.text }}</span>
                        </li>
                      </ul>
                    </div>

                    <div v-if="sourceList(item.step).length" class="src-block">
                      <div class="src-h">引用链接</div>
                      <ul class="src-ul">
                        <li v-for="(src, si) in sourceList(item.step)" :key="si">
                          <a :href="src.url" target="_blank" rel="noopener noreferrer" class="src-a">
                            {{ src.title || src.url }}
                          </a>
                        </li>
                      </ul>
                    </div>

                    <template v-if="item.step.input_preview">
                      <div class="sec-h">输入摘要</div>
                      <div class="sec-text sm">{{ item.step.input_preview }}</div>
                    </template>

                    <template v-if="item.step.output">
                      <div class="sec-h">阶段产出</div>
                      <div class="sec-text out">{{ truncateOut(item.step.output) }}</div>
                    </template>

                    <div v-if="item.step.error" class="err-line">{{ item.step.error }}</div>
                  </div>
                </details>
              </div>
            </section>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import { displayStepsForRun, humanStepName as humanRuntimeStepName, inferPhaseGroup } from "../thinkingFromRun.js";

const PHASE_ORDER = ["intake", "search", "reasoning", "draft", "evaluate", "repair", "verify", "finalize", "other"];
const PHASE_TITLES = {
  intake: "分析与规划",
  search: "检索与证据",
  reasoning: "能力与工具",
  draft: "起草",
  evaluate: "评估",
  repair: "修复",
  verify: "验证",
  finalize: "终稿",
  other: "其他步骤",
};

export default {
  name: "StepDisplay",
  props: {
    runs: { type: Array, default: () => [] },
    renderTick: { type: Number, default: 0 },
  },
  data() {
    return {
      manualOpenId: "",
      collapsedRunIds: [],
      wallClock: 0,
      _clockId: null,
      stepsStickBottom: true,
    };
  },
  computed: {
    latestRun() {
      void this.renderTick;
      return this.runs.length ? this.runs[this.runs.length - 1] : null;
    },
    runningElapsedSec() {
      void this.wallClock;
      const run = this.latestRun;
      if (!run || run.status !== "running") return 0;
      const started = new Date(run.createdAt || 0).getTime();
      if (!started) return 0;
      return Math.max(0, Math.floor((Date.now() - started) / 1000));
    },
  },
  watch: {
    runs(nextRuns, oldRuns) {
      if ((nextRuns?.length || 0) !== (oldRuns?.length || 0)) {
        this.manualOpenId = "";
        const ids = new Set((nextRuns || []).map((run) => run.id));
        this.collapsedRunIds = this.collapsedRunIds.filter((id) => ids.has(id));
      }
      this.$nextTick(() => this.maybeScrollStepsToBottom());
    },
    renderTick() {
      this.$nextTick(() => this.maybeScrollStepsToBottom());
    },
  },
  mounted() {
    this._clockId = setInterval(() => {
      if (this.runs.some((run) => run.status === "running")) {
        this.wallClock += 1;
      }
    }, 1000);
    const list = this.$refs.stepsList;
    if (list) list.addEventListener("scroll", this.onStepsScroll, { passive: true });
  },
  beforeUnmount() {
    if (this._clockId) clearInterval(this._clockId);
    const list = this.$refs.stepsList;
    if (list) list.removeEventListener("scroll", this.onStepsScroll);
  },
  methods: {
    runtimeLabel(runtime) {
      const value = String(runtime || "").trim().toLowerCase();
      if (!value || value === "adaptive_dag_v3") return "Adaptive DAG Runtime";
      return runtime;
    },
    flatSteps(run) {
      return displayStepsForRun(run);
    },
    groupedPhases(run) {
      const buckets = Object.fromEntries(PHASE_ORDER.map((key) => [key, []]));
      this.flatSteps(run).forEach((step, globalIdx) => {
        const phase = inferPhaseGroup(step);
        const key = buckets[phase] ? phase : "other";
        buckets[key].push({ step, globalIdx });
      });
      return PHASE_ORDER.filter((key) => buckets[key].length).map((key) => ({
        key,
        title: PHASE_TITLES[key] || key,
        steps: buckets[key],
      }));
    },
    stepName(step) {
      return humanRuntimeStepName(step?.name, step?.meta || {});
    },
    onStepsScroll() {
      const list = this.$refs.stepsList;
      if (!list) return;
      this.stepsStickBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    },
    maybeScrollStepsToBottom() {
      if (!this.stepsStickBottom) return;
      this.$nextTick(() => {
        const list = this.$refs.stepsList;
        if (list) list.scrollTop = list.scrollHeight;
      });
    },
    truncateOut(text) {
      if (!text || typeof text !== "string") return "";
      const trimmed = text.trim();
      return trimmed.length <= 4000 ? trimmed : `${trimmed.slice(0, 4000)}\n…（以下省略）`;
    },
    shortTrace(traceId) {
      if (!traceId) return "trace …";
      return traceId.length > 18 ? `${traceId.slice(0, 14)}…` : traceId;
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
        const exists = this.collapsedRunIds.includes(run.id);
        this.collapsedRunIds = exists
          ? this.collapsedRunIds.filter((id) => id !== run.id)
          : [...this.collapsedRunIds, run.id];
        return;
      }
      this.manualOpenId = run.id;
    },
    statusText(status) {
      if (status === "ok") return "完成";
      if (status === "error") return "失败";
      if (status === "stopped") return "已停止";
      if (status === "skipped") return "已跳过";
      return "进行中";
    },
    statusShort(status) {
      if (status === "ok") return "完成";
      if (status === "error") return "失败";
      if (status === "running") return "进行中";
      if (status === "skipped") return "跳过";
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
      const sources = step?.meta?.sources;
      return Array.isArray(sources) ? sources.filter((src) => src && (src.url || src.title)) : [];
    },
    workflowLines(step) {
      const meta = step?.meta && typeof step.meta === "object" ? step.meta : {};
      const rows = [];
      const add = (label, value) => {
        if (value === undefined || value === null || value === "") return;
        rows.push({ label, text: this.formatMetaValue(value) });
      };

      if (step?.name === "complexity_analyze") {
        add("复杂度", meta.complexity);
        add("任务类型", meta.task_type);
        add("意图归类", meta.type);
        if (meta.search_required === true) add("需要联网", "是");
        if (meta.search_required === false) add("需要联网", "否");
        add("检索查询", meta.search_query);
        add("时效提示", meta.freshness_hint);
        add("置信度", meta.confidence);
        add("首选模型", meta.selected_model);
        add("回退模型", Array.isArray(meta.fallback_models) ? meta.fallback_models.join(" → ") : "");
        return rows;
      }

      if (step?.name === "dag_runtime_plan") {
        add("架构", meta.architecture);
        add("调度器", meta.scheduler);
        const plan = meta.plan && typeof meta.plan === "object" ? meta.plan : {};
        add("并行检索", plan.parallel_search_queries);
        add("并行评估", plan.parallel_critics);
        add("分层评估", plan.layered_critics);
        add("并行起草", plan.parallel_drafts);
        add("最大修复轮次", plan.max_repair_rounds);
        add("工具能力门", plan.tool_capability_gate);
        add("目标子图", plan.goal_subgraph);
        if (Array.isArray(plan.planned_nodes) && plan.planned_nodes.length) {
          add("计划节点", `${plan.planned_nodes.length} 个`);
        }
        return rows;
      }

      if (step?.name === "web_search" || step?.name === "dag_parallel_search") {
        add("检索查询", meta.query_effective || meta.query);
        add("提供方", meta.provider_used || meta.provider || step.provider);
        add("命中条数", meta.result_count != null ? meta.result_count : this.sourceList(step).length);
        add("降级执行", meta.degraded);
        add("失败码", meta.failure_code);
      } else if (step?.name === "dag_tool_capability_gate") {
        add("能力", meta.capability);
      } else if (step?.name === "goal_capability_gate") {
        add("目标已解决", meta.goals_resolved);
        add("证据充分", meta.evidence_sufficient);
        add("阻塞", meta.blocked);
      } else if (step?.name === "dag_parallel_critic") {
        add("轮次", meta.round);
        add("评估模式", meta.gather_mode);
      } else if (step?.name === "dag_repair") {
        add("轮次", meta.round);
        add("跳过修复", meta.skipped);
        add("已回滚保护", meta.guard_reverted);
      } else if (step?.name === "dag_verify") {
        add("推荐动作", meta.unified_critic?.recommended_action);
      }

      const genericRows = this.genericMetaRows(meta);
      genericRows.forEach((row) => {
        if (!rows.some((item) => item.label === row.label && item.text === row.text)) rows.push(row);
      });
      return rows;
    },
    genericMetaRows(meta) {
      const rows = [];
      const skip = new Set([
        "sources",
        "event_summary",
        "runtime_intent",
        "dynamic_plan",
        "plan",
        "unified_critic",
        "query_effective",
      ]);
      Object.entries(meta || {}).forEach(([key, value]) => {
        if (skip.has(key)) return;
        if (value === undefined || value === null || value === "") return;
        if (typeof value === "object" && !Array.isArray(value)) return;
        rows.push({ label: this.metaLabel(key), text: this.formatMetaValue(value) });
      });
      return rows;
    },
    metaLabel(key) {
      const map = {
        phase_group: "阶段",
        query: "查询",
        provider: "提供方",
        provider_used: "提供方",
        result_count: "命中条数",
        round: "轮次",
        architecture: "架构",
        scheduler: "调度器",
        capability: "能力",
        blocked: "阻塞",
        goals_resolved: "目标已解决",
        evidence_sufficient: "证据充分",
      };
      return map[key] || key;
    },
    formatMetaValue(value) {
      if (value === true) return "是";
      if (value === false) return "否";
      if (Array.isArray(value)) {
        return value.map((item) => this.formatMetaValue(item)).join("、");
      }
      if (typeof value === "object") {
        return JSON.stringify(value);
      }
      return String(value);
    },
    stepFoldOpen(run, step, globalIdx) {
      void this.renderTick;
      if (step.status === "running" || step.status === "error") return true;
      const runIdx = this.runs.findIndex((item) => item.id === run.id);
      const isLatest = runIdx >= 0 && runIdx === this.runs.length - 1;
      if (!isLatest) return false;
      const steps = this.flatSteps(run);
      return globalIdx === steps.length - 1;
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
.runtime-pill {
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.2);
  color: #c7d2fe;
  font-weight: 600;
}
.phase-hint {
  color: #e2e8f0;
  font-weight: 500;
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
  overflow: visible;
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
  overflow-wrap: anywhere;
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
  flex-wrap: wrap;
  justify-content: flex-end;
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
.badge.soft.runtime {
  color: #c7d2fe;
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
  padding: 0 10px 14px;
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
.phases-col {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.phase-card {
  border: 1px solid rgba(61, 77, 100, 0.85);
  border-radius: 12px;
  background: rgba(30, 36, 51, 0.55);
  padding: 10px 10px 12px;
}
.phase-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
  padding: 0 2px;
}
.phase-title {
  font-size: 12px;
  font-weight: 700;
  color: #e2e8f0;
}
.phase-count {
  font-size: 11px;
  color: #64748b;
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
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 8px 10px;
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
  flex: 1;
  min-width: 6.5rem;
  overflow-wrap: anywhere;
  line-height: 1.35;
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
.sum-st.running {
  background: rgba(99, 102, 241, 0.2);
  color: #c7d2fe;
}
.sum-meta {
  font-size: 11px;
  color: #94a3b8;
}
.step-inner {
  padding: 0 12px 12px;
  border-top: 1px solid #2f3a4d;
  max-height: min(52vh, 520px);
  overflow-x: hidden;
  overflow-y: auto;
}
.event-summary {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.5;
  color: #e2e8f0;
  overflow-wrap: anywhere;
}
.flow-block {
  margin-top: 10px;
}
.flow-h {
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  margin-bottom: 8px;
}
.flow-ul {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  line-height: 1.55;
}
.flow-ul li {
  display: grid;
  grid-template-columns: minmax(88px, 118px) minmax(0, 1fr);
  gap: 8px 12px;
  padding: 8px 0;
  border-bottom: 1px solid rgba(47, 58, 77, 0.6);
  align-items: start;
}
.flow-ul li:last-child {
  border-bottom: none;
}
.flow-k {
  color: #64748b;
}
.flow-v {
  color: #e2e8f0;
  min-width: 0;
  overflow-wrap: anywhere;
  line-height: 1.5;
  white-space: pre-wrap;
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
.sec-text {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #1a1f2b;
  font-size: 12px;
  line-height: 1.55;
  color: #cbd5e1;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: min(36vh, 320px);
  overflow-y: auto;
}
.sec-text.sm {
  max-height: 120px;
  font-size: 11px;
  color: #94a3b8;
}
.sec-text.out {
  max-height: 280px;
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
