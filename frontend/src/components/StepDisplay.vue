<template>
  <div class="wrap">
    <div v-if="runs.length" class="summary-bar">
      <span>{{ runs.length }} 轮对话</span>
      <span class="sep">·</span>
      <span class="track-pill">{{ trackLabel(latestRun?.track) }}</span>
      <span class="sep">·</span>
      <span>共 {{ latestRun?.steps?.length || 0 }} 步</span>
      <template v-if="latestRun?.phaseMessage">
        <span class="sep">·</span>
        <span class="phase-hint">{{ latestRun.phaseMessage }}</span>
      </template>
    </div>

    <div class="list">
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
                <span class="sum-name">{{ humanStepName(s.name, s.meta) }}</span>
                <span class="sum-st" :class="s.status">{{ statusShort(s.status) }}</span>
                <span v-if="s.provider" class="sum-meta">{{ s.provider }}</span>
                <span v-if="s.latency_ms != null" class="sum-meta">{{ s.latency_ms }}ms</span>
              </summary>

              <div class="step-inner">
                <div v-if="workflowLines(s).length" class="flow-block">
                  <div class="flow-h">流程要点</div>
                  <ul class="flow-ul">
                    <li v-for="(row, wi) in workflowLines(s)" :key="wi">
                      <span class="flow-k">{{ row.label }}</span>
                      <span class="flow-v">{{ row.text }}</span>
                    </li>
                  </ul>
                </div>

                <div v-if="sourceList(s).length" class="src-block">
                  <div class="src-h">引用链接</div>
                  <ul class="src-ul">
                    <li v-for="(src, si) in sourceList(s)" :key="si">
                      <span class="src-n">[{{ src.index }}]</span>
                      <a :href="src.url" target="_blank" rel="noopener noreferrer" class="src-a">{{ src.title || "打开" }}</a>
                    </li>
                  </ul>
                </div>

                <template v-if="s.input_preview">
                  <div class="sec-h">输入摘要</div>
                  <div class="sec-text sm">{{ s.input_preview }}</div>
                </template>

                <template v-if="s.output">
                  <div class="sec-h">阶段产出（节选）</div>
                  <div class="sec-text out">{{ truncateOut(s.output) }}</div>
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
  review_web_search: "审查 · 联网核查",
  refine_entry_web_search: "精化轨 · 入口轻量联网",
  agent_start: "Agent 轨",
  agent_iteration: "Agent 迭代",
  agent_web_search: "Agent · 联网",
  agent_plain_coerce_refine: "Agent · 纯文本强制精化",
  agent_refine_answer: "Agent · Refine 润色",
  agent_refine_fallback: "Agent 迭代用尽 · 全链 Refine 兜底",
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
    trackLabel(t) {
      const M = { fast: "快速轨", refine: "精化轨", agent: "Agent 轨", auto: "自动" };
      return (t && M[t]) || t || "—";
    },
    truncateOut(text) {
      if (!text || typeof text !== "string") return "";
      const m = 6000;
      return text.length > m ? text.slice(0, m) + "\n…（以下省略）" : text;
    },
    humanStepName(name, meta) {
      const m = meta || {};
      const base = STEP_LABELS[name] || name || "步骤";
      if (name === "review_web_search" && m.review_round != null) {
        return `${base} · 第${m.review_round}轮`;
      }
      if (name === "agent_iteration" && m.i != null && m.max != null) {
        return `${base} · ${m.i}/${m.max}`;
      }
      return base;
    },
    shortTrace(t) {
      if (!t) return "trace …";
      return t.length > 18 ? `${t.slice(0, 14)}…` : t;
    },
    trackLabel(t) {
      const z = String(t || "").toLowerCase();
      const map = { fast: "快速轨", refine: "精化轨", agent: "Agent", auto: "自动" };
      return map[z] || t || "—";
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
      if (status === "skipped") return "已跳过";
      return "进行中";
    },
    statusShort(st) {
      if (st === "ok") return "完成";
      if (st === "error") return "失败";
      if (st === "running") return "进行中";
      if (st === "skipped") return "跳过";
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
    truncateOut(text) {
      if (!text || typeof text !== "string") return "";
      const max = 4000;
      const t = text.trim();
      return t.length <= max ? t : `${t.slice(0, max)}\n…（以下省略）`;
    },
    _fmtDecision(d) {
      if (d === undefined || d === null || d === "") return "";
      const x = String(d).toLowerCase();
      if (x === "fast") return "倾向快速单段答复";
      if (x === "refine") return "倾向精化（草稿→审查→润色）";
      return String(d);
    },
    _fmtMode(mode) {
      const z = String(mode || "").toLowerCase();
      const map = { auto: "自动", fast: "快速轨", refine: "精化轨", agent: "Agent 轨" };
      return map[z] || mode || "";
    },
    _fmtTaskType(tt) {
      const z = String(tt || "").toLowerCase();
      const map = {
        reasoning: "推理类（可走 Agent 轨）",
        generation: "生成类（倾向精化轨）",
        conversation: "对话类（倾向快速轨）",
      };
      return map[z] || tt || "";
    },
    _line(rows, label, text) {
      if (text === undefined || text === null) return;
      let s = text;
      if (typeof text === "boolean") s = text ? "是" : "否";
      else if (typeof text === "number") s = String(text);
      else if (typeof text === "string") {
        s = text.trim();
        if (!s) return;
      } else return;
      rows.push({ label, text: s });
    },
    _briefModels(obj) {
      if (!obj || typeof obj !== "object") return "";
      try {
        return Object.entries(obj)
          .map(([k, v]) => {
            const arr = Array.isArray(v) ? v : [v];
            return `${k}：${arr.filter(Boolean).slice(0, 3).join("、")}`;
          })
          .join("；");
      } catch {
        return "";
      }
    },
    workflowLines(step) {
      const rows = [];
      const m = step.meta && typeof step.meta === "object" ? step.meta : {};
      const name = step.name || "";

      switch (name) {
        case "complexity_analyze": {
          this._line(rows, "调度结论", this._fmtDecision(m.decision));
          const tt = [this._fmtTaskType(m.task_type) || m.task_type, m.type].filter(Boolean).join(" · ");
          if (tt) this._line(rows, "任务 / 归类", tt);
          this._line(rows, "复杂度", m.complexity);
          const rs =
            m.reason ||
            (Array.isArray(m.reasons) && m.reasons.length ? m.reasons.join("；") : "") ||
            (m.analyzer_timed_out ? "预判超时，已按规则降级" : "");
          if (rs) this._line(rows, "判定摘要", rs);
          if (m.search_required === true) this._line(rows, "检索策略", "倾向补充实时信息");
          else if (m.search_required === false) this._line(rows, "检索策略", "未强制检索");
          if (m.search_query) this._line(rows, "检索要点建议", m.search_query);
          if (m.selected_model || (Array.isArray(m.fallback_models) && m.fallback_models.length)) {
            const pool = [m.selected_model, ...(Array.isArray(m.fallback_models) ? m.fallback_models : [])]
              .filter(Boolean)
              .join(" → ");
            if (pool) this._line(rows, "模型池", pool);
          }
          if (Array.isArray(m.manual_hits) && m.manual_hits.length) {
            this._line(rows, "快捷触发", m.manual_hits.join("、"));
          }
          if (m.refine_models && typeof m.refine_models === "object") {
            const b = this._briefModels(m.refine_models);
            if (b) this._line(rows, "精化各层模型", b);
          }
          break;
        }
        case "track_select":
          this._line(rows, "请求模式", this._fmtMode(m.mode));
          this._line(rows, "选用轨道", this.trackLabel(m.track));
          this._line(rows, "任务类型", this._fmtTaskType(m.task_type) || m.task_type || "—");
          if (m.complexity) this._line(rows, "复杂度标签", m.complexity);
          if (m.decision) this._line(rows, "预判调度", this._fmtDecision(m.decision));
          if (m.search_required === true) this._line(rows, "实时信息", "预判：需要检索补充");
          else if (m.search_required === false) this._line(rows, "实时信息", "预判：未强制检索");
          if (m.intended_track && m.intended_track !== m.track) {
            this._line(rows, "原计划轨道", this.trackLabel(m.intended_track));
          }
          if (m.agent_disabled_fallback) this._line(rows, "说明", "Agent 未启用，已改用精化轨");
          break;
        case "agent_start":
          if (m.phase) this._line(rows, "初始化", m.phase);
          if (m.model) this._line(rows, "主模型", m.model);
          if (m.max_iterations != null) this._line(rows, "迭代上限", `${m.max_iterations} 轮`);
          if (m.thread_turns != null) this._line(rows, "上下文带入轮次", String(m.thread_turns));
          break;
        case "agent_iteration":
          if (m.phase) this._line(rows, "阶段", m.phase);
          if (m.i != null && m.max != null) this._line(rows, "进度", `第 ${m.i} / ${m.max} 轮`);
          if (m.model) this._line(rows, "模型", m.model);
          if (m.branch_next) this._line(rows, "下一步", m.branch_next);
          if (m.reply_preview) this._line(rows, "本轮模型输出摘录", m.reply_preview);
          if (m.reply_chars != null) this._line(rows, "输出长度", `${m.reply_chars} 字符`);
          break;
        case "agent_web_search":
          this._line(rows, "查询词", m.query);
          if (m.from === "agent") this._line(rows, "触发方式", "Agent <<ACTION: web_search>>");
          if (Array.isArray(m.sources)) this._line(rows, "返回条目", String(m.sources.length));
          break;
        case "fast_route":
          this._line(rows, "路由策略", m.rule || "按分析结果选择模型");
          if (Array.isArray(m.candidates) && m.candidates.length) {
            this._line(rows, "候选顺序", m.candidates.join(" → "));
          }
          this._line(rows, "首选模型", m.selected);
          break;
        case "web_search":
          this._line(rows, "检索查询", m.query_effective || m.query);
          this._line(rows, "触发原因", m.reason);
          if (m.result_count != null) this._line(rows, "命中条数", m.result_count);
          if (m.degraded) this._line(rows, "检索状态", "部分降级");
          if (m.failure_code) this._line(rows, "失败码", m.failure_code);
          break;
        case "review_web_search":
          if (m.phase) this._line(rows, "环节", m.phase);
          if (m.review_round != null) this._line(rows, "轮次", `第 ${m.review_round} 轮`);
          this._line(rows, "查询", m.query);
          if (m.result_count != null) this._line(rows, "命中条数", m.result_count);
          if (m.agent_tool === "refine_answer") this._line(rows, "来源", "Agent 工具 refine_answer");
          if (m.agent_fallback) this._line(rows, "来源", "Agent 迭代兜底");
          break;
        case "refine_layer1_draft":
        case "refine_layer2_review":
        case "refine_layer3_polish":
          if (m.phase) this._line(rows, "环节", m.phase);
          if (Array.isArray(m.candidates)) this._line(rows, "候选模型", m.candidates.join("、"));
          if (Array.isArray(m.attempts)) this._line(rows, "尝试次数", m.attempts.length);
          if (m.from_agent) this._line(rows, "链路", "Agent Refine 流水线");
          if (m.review_search_loops != null) this._line(rows, "审查联网轮次", m.review_search_loops);
          if (m.reason) this._line(rows, "说明", m.reason);
          break;
        default:
          if (name.startsWith("agent_")) {
            if (m.query) this._line(rows, "查询词", m.query);
            if (m.reason === "max_iterations_exhausted") this._line(rows, "触发原因", "已达最大迭代次数");
            if (m.same_pipeline_as) this._line(rows, "对齐流水线", String(m.same_pipeline_as));
          }
          this._appendGenericMetaLines(m, rows);
          break;
      }

      if (!rows.some((r) => r.label === "模型") && step.model) this._line(rows, "模型", step.model);
      if (!rows.some((r) => r.label === "接入") && step.provider) this._line(rows, "接入", step.provider);

      return rows;
    },
    _appendGenericMetaLines(m, rows) {
      const skip = new Set([
        "sources",
        "raw_llm_response",
        "hits",
        "results_preview",
        "attempts",
        "refine_models",
        "fallback_models",
        "next_move",
      ]);
      const labels = {
        query: "查询",
        query_effective: "生效查询",
        reason: "原因",
        track: "轨道",
        mode: "模式",
        task_type: "任务类型",
        meta: "附加",
        model: "模型",
        max_iterations: "最大迭代轮次",
        result_count: "命中条数",
        branch_next: "下一步",
        reply_preview: "输出摘录",
        next_move: "分支",
        phase: "阶段",
        thread_turns: "对话轮次",
      };
      const have = new Set(rows.map((r) => r.label));
      for (const [k, v] of Object.entries(m)) {
        if (skip.has(k)) continue;
        if (v === null || v === undefined || v === "") continue;
        const lab = labels[k] || k;
        if (have.has(lab)) continue;
        if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
          this._line(rows, lab, v);
          have.add(lab);
        }
      }
    },
    stepFoldOpen(run, stepIdx, step) {
      void this.renderTick;
      const runIdx = this.runs.findIndex((r) => r.id === run.id);
      const isLatestCard = runIdx >= 0 && runIdx === this.runs.length - 1;
      if (isLatestCard) return true;
      if (step.status === "running") return true;
      return stepIdx === (run.steps?.length || 0) - 1;
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
.phase-hint {
  color: #e2e8f0;
  font-weight: 500;
  max-width: 100%;
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
  word-break: break-word;
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
  padding: 0 10px 14px;
  overflow: visible;
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
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
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
  overscroll-behavior: contain;
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
  flex-shrink: 0;
}
.flow-v {
  color: #e2e8f0;
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
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
  word-break: break-word;
  max-height: min(36vh, 320px);
  overflow-y: auto;
  overscroll-behavior: contain;
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
