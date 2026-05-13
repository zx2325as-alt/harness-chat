<template>
  <div class="wrap">
    <div v-if="runs.length" class="summary-bar">
      <span>{{ runs.length }} 轮对话</span>
      <span class="sep">·</span>
      <span class="track-pill">{{ trackLabel(latestRun?.track) }}</span>
      <span class="sep">·</span>
        <span>共 {{ latestRun ? displayStepsForRun(latestRun).length : 0 }} 步</span>
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
            <span class="badge soft" :class="run.status">{{ statusText(run.status) }}</span>
            <span class="badge outline">{{ displayStepsForRun(run).length || 0 }} 步</span>
            <span class="chev">{{ isOpen(run, idx) ? "收起" : "展开" }}</span>
          </div>
        </button>

        <div v-if="isOpen(run, idx)" class="run-body">
          <div v-if="run.documents && run.documents.length" class="doc-line">
            文档：
            <span v-for="d in run.documents" :key="d.name" class="doc-pill">{{ d.name }}</span>
          </div>

          <div class="phases-col">
            <section
              v-for="ph in groupedPhases(run)"
              :key="ph.key"
              class="phase-card"
            >
              <div class="phase-head">
                <span class="phase-title">{{ ph.title }}</span>
                <span class="phase-count">{{ ph.steps.length }} 步</span>
              </div>

              <div v-if="ph.key === 'refine'" class="refine-pipeline" aria-hidden="true">
                <span class="pipe-node" :class="refinePipeClass(ph.steps, 'draft')">初稿</span>
                <span class="pipe-arrow">→</span>
                <span class="pipe-node" :class="refinePipeClass(ph.steps, 'review')">审查</span>
                <span class="pipe-arrow">→</span>
                <span class="pipe-node" :class="refinePipeClass(ph.steps, 'polish')">润色</span>
              </div>

              <div v-if="ph.key === 'polishing'" class="polish-pipeline-hint" aria-hidden="true">
                <span class="pipe-node" :class="polishPipeClass(ph.steps, 'self')">自检</span>
                <span class="pipe-arrow">→</span>
                <span class="pipe-node" :class="polishPipeClass(ph.steps, 'review')">审查</span>
                <span class="pipe-arrow">→</span>
                <span class="pipe-node" :class="polishPipeClass(ph.steps, 'polish')">润色</span>
              </div>

              <div class="steps-col">
                <template v-for="item in ph.steps" :key="`${ph.key}-${item.globalIdx}-${item.step.name}-${item.step.status}-${renderTick}-${(item.step.output || '').length}`">
                  <div
                    v-if="ph.key === 'reasoning' && narrativeReasoningLine(item.step)"
                    class="narrative-row"
                    :class="{
                      think: item.step.name === 'agent_iteration',
                      act: item.step.name === 'agent_web_search',
                      start: item.step.name === 'agent_start',
                    }"
                  >
                    <span class="nar-icon">{{ narrativeReasoningLine(item.step).icon }}</span>
                    <div class="nar-body">
                      <div class="nar-title">{{ narrativeReasoningLine(item.step).title }}</div>
                      <div v-if="(item.step.meta || {}).event_summary" class="nar-sum">{{ (item.step.meta || {}).event_summary }}</div>
                    </div>
                  </div>

                  <details
                    class="step-one"
                    :class="{ 'nar-attached': ph.key === 'reasoning' && narrativeReasoningLine(item.step) }"
                    :open="stepFoldOpen(run, item.step, item.globalIdx)"
                  >
                    <summary class="step-sum">
                      <span class="sum-chev" aria-hidden="true">◇</span>
                      <span class="sum-name">{{ humanStepName(item.step.name, item.step.meta) }}</span>
                      <span class="sum-st" :class="item.step.status">{{ statusShort(item.step.status) }}</span>
                      <span v-if="item.step.provider" class="sum-meta">{{ item.step.provider }}</span>
                      <span v-if="item.step.latency_ms != null" class="sum-meta">{{ item.step.latency_ms }}ms</span>
                      <span
                        v-if="item.step.name === 'agent_postprocess_bundle' && (item.step.meta || {})._bundle_latency_sum_ms"
                        class="sum-meta"
                      >
                        Σ {{ (item.step.meta || {})._bundle_latency_sum_ms }}ms
                      </span>
                    </summary>

                    <div class="step-inner">
                      <details
                        v-if="item.step.name === 'agent_postprocess_bundle' && ((item.step.meta || {})._bundle_steps || []).length"
                        class="bundle-substeps"
                      >
                        <summary>展开子步骤（耗时与状态）</summary>
                        <ul class="bundle-ul">
                          <li v-for="(bs, bi) in (item.step.meta || {})._bundle_steps || []" :key="bi" class="bundle-li">
                            <span class="bundle-name">{{ humanStepName(bs.name, bs.meta) }}</span>
                            <span class="bundle-st" :class="bs.status">{{ statusShort(bs.status) }}</span>
                            <span v-if="bs.latency_ms != null" class="bundle-meta">{{ bs.latency_ms }}ms</span>
                            <span v-if="bs.error" class="bundle-err">{{ bs.error }}</span>
                          </li>
                        </ul>
                      </details>
                      <p
                        v-if="
                          (item.step.meta || {}).event_summary &&
                          !(ph.key === 'reasoning' && narrativeReasoningLine(item.step))
                        "
                        class="event-summary"
                      >
                        {{ (item.step.meta || {}).event_summary }}
                      </p>

                      <details
                        v-if="item.step.name === 'agent_iteration' && (item.step.meta || {}).reply_preview"
                        class="think-preview"
                      >
                        <summary>本轮模型输出摘录</summary>
                        <div class="sec-text sm">{{ (item.step.meta || {}).reply_preview }}</div>
                      </details>

                      <button
                        v-if="wantsTechToggle(item.step)"
                        type="button"
                        class="meta-toggle meta-toggle-first"
                        @click.stop="toggleFullMeta(run.id, item.globalIdx)"
                      >
                        {{ fullMetaOpen(run.id, item.globalIdx) ? "收起技术细节" : "查看技术细节" }}
                      </button>

                      <div
                        v-if="showWorkflowBlock(run, item.globalIdx, item.step)"
                        class="flow-block"
                      >
                        <div class="flow-h">技术细节</div>
                        <ul class="flow-ul">
                          <li v-for="(row, wi) in workflowLines(item.step, run, item.globalIdx)" :key="wi">
                            <span class="flow-k">{{ row.label }}</span>
                            <span class="flow-v">{{ row.text }}</span>
                          </li>
                        </ul>
                      </div>

                      <div v-if="sourceList(item.step).length" class="src-block">
                        <div class="src-h">引用链接</div>
                        <ul class="src-ul">
                          <li v-for="(src, si) in sourceList(item.step)" :key="si">
                            <span class="src-n">[{{ src.index }}]</span>
                            <a :href="src.url" target="_blank" rel="noopener noreferrer" class="src-a">{{ src.title || "打开" }}</a>
                          </li>
                        </ul>
                      </div>

                      <template v-if="item.step.input_preview">
                        <div class="sec-h">输入摘要</div>
                        <div class="sec-text sm">{{ item.step.input_preview }}</div>
                      </template>

                      <template v-if="item.step.output">
                        <div class="sec-h">阶段产出（节选）</div>
                        <div class="sec-text out">{{ truncateOut(item.step.output) }}</div>
                      </template>

                      <div v-if="item.step.error" class="err-line">{{ item.step.error }}</div>
                    </div>
                  </details>
                </template>
              </div>
            </section>
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
  web_search_policy: "联网策略",
  fast_route: "快速轨路由",
  refine_layer1_draft: "初稿层",
  refine_draft: "初稿层",
  refine_layer2_review: "审查层",
  refine_quality_review: "结构化审查",
  refine_layer3_polish: "润色层",
  refine_finalize: "终稿排版",
  refine_disabled_fallback_fast: "精化关闭 · 降级快速轨",
  review_web_search: "审查 · 联网核查",
  refine_entry_web_search: "精化轨 · 入口轻量联网",
  agent_start: "Agent 轨",
  agent_iteration: "Agent 迭代",
  agent_web_search: "Agent · 联网",
  agent_plain_coerce_refine: "Agent · 纯文本强制精化",
  agent_self_check: "Agent · 高复杂度自检",
  agent_refine_answer: "Agent · Refine 润色",
  fast_answer_cache: "快轨 · 缓存命中",
  agent_refine_fallback: "Agent 迭代用尽 · 全链 Refine 兜底",
  agent_postprocess_bundle: "Agent 后处理（自检+润色）",
  dag_runtime_plan: "DAG 规划",
  dag_parallel_search: "DAG · 并行检索",
  dag_draft: "DAG · 起草",
  dag_parallel_critic: "DAG · 并行批评",
  dag_repair: "DAG · 定点修复",
  dag_verify: "DAG · 验证",
  dag_finalize: "DAG · 终稿排版",
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
      wallClock: 0,
      _clockId: null,
      stepsStickBottom: true,
      showFullMetaSteps: {},
    };
  },
  computed: {
    latestRun() {
      void this.renderTick;
      return this.runs && this.runs.length ? this.runs[this.runs.length - 1] : null;
    },
    runningElapsedSec() {
      void this.wallClock;
      void this.renderTick;
      const r = this.latestRun;
      if (!r || r.status !== "running") return 0;
      const t0 = new Date(r.createdAt || 0).getTime();
      if (!t0) return 0;
      return Math.max(0, Math.floor((Date.now() - t0) / 1000));
    },
  },
  watch: {
    runs(newRuns, oldRuns) {
      if ((newRuns?.length || 0) !== (oldRuns?.length || 0)) {
        this.manualOpenId = "";
        const ids = new Set((newRuns || []).map((r) => r.id));
        this.collapsedRunIds = (this.collapsedRunIds || []).filter((id) => ids.has(id));
      }
      this.$nextTick(() => this.maybeScrollStepsToBottom());
    },
    renderTick() {
      this.$nextTick(() => this.maybeScrollStepsToBottom());
    },
  },
  mounted() {
    this._clockId = setInterval(() => {
      if (this.runs && this.runs.some((r) => r.status === "running")) this.wallClock++;
    }, 1000);
    const el = this.$refs.stepsList;
    if (el) el.addEventListener("scroll", this.onStepsScroll, { passive: true });
  },
  beforeUnmount() {
    if (this._clockId) clearInterval(this._clockId);
    const el = this.$refs.stepsList;
    if (el) el.removeEventListener("scroll", this.onStepsScroll);
  },
  methods: {
    fullMetaKey(runId, stepIdx) {
      return `${runId || "x"}:${stepIdx}`;
    },
    fullMetaOpen(runId, stepIdx) {
      return !!this.showFullMetaSteps[this.fullMetaKey(runId, stepIdx)];
    },
    toggleFullMeta(runId, stepIdx) {
      const k = this.fullMetaKey(runId, stepIdx);
      this.showFullMetaSteps = { ...this.showFullMetaSteps, [k]: !this.showFullMetaSteps[k] };
    },
    displayStepsForRun(run) {
      const steps = run?.steps || [];
      if (!steps.length) return [];
      const bundle = new Set(["agent_plain_coerce_refine", "agent_self_check", "agent_refine_answer"]);
      const out = [];
      let i = 0;
      while (i < steps.length) {
        const s = steps[i];
        if (bundle.has(s.name)) {
          let j = i;
          while (j < steps.length && bundle.has(steps[j].name)) j++;
          const chunk = steps.slice(i, j);
          const running = chunk.some((x) => x.status === "running");
          const err = chunk.some((x) => x.status === "error");
          const st = running ? "running" : err ? "error" : "ok";
          const last = chunk[chunk.length - 1];
          const latSum = chunk.reduce((acc, x) => acc + (x.latency_ms || 0), 0);
          out.push({
            name: "agent_postprocess_bundle",
            status: st,
            meta: {
              phase_group: "polishing",
              event_summary: "后处理：自检 → 审查 → 润色（合并为一步展示）",
              _bundle_members: chunk.map((c) => c.name).join(" → "),
              _bundle_steps: chunk.map((c) => ({ ...c })),
              _bundle_latency_sum_ms: latSum || null,
            },
            model: last?.model,
            provider: last?.provider,
            latency_ms: last?.latency_ms,
            output: null,
            error: err ? (chunk.find((x) => x.status === "error") || {}).error : null,
          });
          i = j;
          continue;
        }
        out.push(s);
        i++;
      }
      return out;
    },
    inferPhaseGroup(step) {
      const m = (step && step.meta) || {};
      if (m.phase_group) return String(m.phase_group);
      const n = (step && step.name) || "";
      const table = {
        complexity_analyze: "intake",
        track_select: "intake",
        web_search: "search",
        refine_entry_web_search: "search",
        web_search_policy: "search",
        fast_route: "fast",
        fast_answer_cache: "fast",
        refine_disabled_fallback_fast: "fast",
        agent_start: "reasoning",
        agent_iteration: "reasoning",
        agent_web_search: "reasoning",
        agent_refine_fallback: "polishing",
        agent_plain_coerce_refine: "polishing",
        agent_self_check: "polishing",
        agent_refine_answer: "polishing",
        agent_postprocess_bundle: "polishing",
        refine_layer1_draft: "refine",
        refine_draft: "refine",
        refine_layer2_review: "refine",
        refine_quality_review: "refine",
        refine_layer3_polish: "refine",
        refine_finalize: "refine",
        refine_runtime_generate: "refine",
        refine_runtime_critic: "refine",
        refine_runtime_repair: "refine",
        refine_runtime_verify: "refine",
        refine_runtime_finalize: "refine",
        review_web_search: "refine",
        dag_runtime_plan: "intake",
        dag_parallel_search: "search",
        dag_draft: "refine",
        dag_parallel_critic: "refine",
        dag_repair: "polishing",
        dag_verify: "refine",
        dag_finalize: "polishing",
      };
      return table[n] || "other";
    },
    groupedPhases(run) {
      const order = ["intake", "search", "fast", "reasoning", "refine", "polishing", "other"];
      const titles = {
        intake: "分析与调度",
        search: "联网检索",
        fast: "快速生成",
        reasoning: "Agent 推理循环",
        refine: "精化流水线",
        polishing: "精化与输出",
        other: "其他步骤",
      };
      const flat = this.displayStepsForRun(run);
      const buckets = {};
      order.forEach((k) => {
        buckets[k] = [];
      });
      flat.forEach((step, globalIdx) => {
        const g = this.inferPhaseGroup(step);
        const k = buckets[g] !== undefined ? g : "other";
        buckets[k].push({ step, globalIdx });
      });
      return order.filter((k) => buckets[k].length).map((k) => ({ key: k, title: titles[k], steps: buckets[k] }));
    },
    _flattenStepsForPipe(stepsWrap) {
      const flat = [];
      (stepsWrap || []).forEach((w) => {
        const s = w.step;
        if (!s) return;
        flat.push(s);
        const inner = (s.meta && s.meta._bundle_steps) || [];
        inner.forEach((sub) => flat.push(sub));
      });
      return flat;
    },
    polishPipeClass(stepsWrap, role) {
      const steps = this._flattenStepsForPipe(stepsWrap);
      const pick = (name) => steps.find((s) => s.name === name);
      if (role === "self") {
        const sc = pick("agent_self_check");
        if (sc) {
          if (sc.status === "running") return "run";
          if (sc.status === "error") return "err";
          return "ok";
        }
        const b = pick("agent_postprocess_bundle");
        const bm = (b && b.meta && b.meta._bundle_members) || "";
        if (bm.includes("agent_self_check")) return "ok";
        return "idle";
      }
      if (role === "review") {
        const s = pick("refine_runtime_critic") || pick("refine_quality_review") || pick("refine_layer2_review");
        if (!s) return "idle";
        if (s.status === "running") return "run";
        if (s.status === "error") return "err";
        return "ok";
      }
      const s = pick("refine_runtime_finalize") || pick("refine_finalize") || pick("refine_layer3_polish");
      if (!s) return "idle";
      if (s.status === "running") return "run";
      if (s.status === "error") return "err";
      return "ok";
    },
    refinePipeClass(stepsWrap, layer) {
      const steps = this._flattenStepsForPipe(stepsWrap);
      const pick = (name) => steps.find((s) => s.name === name);
      if (layer === "draft") {
        const s = pick("refine_runtime_generate") || pick("refine_draft") || pick("refine_layer1_draft");
        if (!s) return "idle";
        if (s.status === "running") return "run";
        if (s.status === "error") return "err";
        return "ok";
      }
      if (layer === "review") {
        const s = pick("refine_runtime_critic") || pick("refine_quality_review") || pick("refine_layer2_review");
        if (!s) return "idle";
        if (s.status === "running") return "run";
        if (s.status === "error") return "err";
        return "ok";
      }
      const s = pick("refine_runtime_finalize") || pick("refine_finalize") || pick("refine_layer3_polish");
      if (!s) return "idle";
      if (s.status === "running") return "run";
      if (s.status === "error") return "err";
      return "ok";
    },
    narrativeReasoningLine(step) {
      if (!step) return null;
      if (step.name === "agent_start") return { icon: "▶", title: "Agent 启动" };
      if (step.name === "agent_iteration") return { icon: "🤔", title: "思考" };
      if (step.name === "agent_web_search") return { icon: "🔍", title: "行动 · 联网检索" };
      return null;
    },
    wantsTechToggle(step) {
      return !!(step && step.meta && step.meta.event_summary);
    },
    showWorkflowBlock(run, globalIdx, step) {
      const rows = this.workflowLines(step, run, globalIdx);
      if (!rows.length) return false;
      if (this.wantsTechToggle(step) && !this.fullMetaOpen(run.id, globalIdx)) return false;
      return true;
    },
    onStepsScroll() {
      const el = this.$refs.stepsList;
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      this.stepsStickBottom = nearBottom;
    },
    maybeScrollStepsToBottom() {
      if (!this.stepsStickBottom) return;
      this.$nextTick(() => {
        const el = this.$refs.stepsList;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    trackLabel(t) {
      const z = String(t || "").toLowerCase();
      const map = { fast: "快速轨", refine: "精化轨", agent: "Agent 轨", dag: "DAG Runtime", auto: "自动" };
      return map[z] || t || "—";
    },
    truncateOut(text) {
      if (!text || typeof text !== "string") return "";
      const max = 4000;
      const t = text.trim();
      return t.length <= max ? t : `${t.slice(0, max)}\n…（以下省略）`;
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
    workflowLines(step, run = null, stepIdx = -1) {
      const rows = [];
      const m = step.meta && typeof step.meta === "object" ? step.meta : {};
      const name = step.name || "";
      const fk = run && stepIdx >= 0 ? this.fullMetaKey(run.id, stepIdx) : "";
      const showAllComplexity = fk && this.showFullMetaSteps[fk];

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
          if (showAllComplexity) {
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
          }
          break;
        }
        case "agent_self_check": {
          if (m.chars != null) this._line(rows, "自检输出", `约 ${m.chars} 字`);
          if (m.phase) this._line(rows, "环节", m.phase);
          break;
        }
        case "fast_answer_cache": {
          if (m.chars != null) this._line(rows, "缓存答案", `约 ${m.chars} 字`);
          if (m.note) this._line(rows, "说明", m.note);
          break;
        }
        case "agent_postprocess_bundle": {
          if (m._bundle_members) this._line(rows, "合并子步骤", m._bundle_members);
          if (m._bundle_latency_sum_ms != null) this._line(rows, "子步骤耗时合计", `${m._bundle_latency_sum_ms}ms`);
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
          if (m.from === "agent") this._line(rows, "触发方式", "Agent JSON：{\"action\":\"web_search\",...}");
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
        case "refine_draft":
        case "refine_layer2_review":
        case "refine_quality_review":
        case "refine_layer3_polish":
        case "refine_finalize":
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
    stepFoldOpen(run, step, globalIdx) {
      void this.renderTick;
      if (step.status === "running" || step.status === "error") return true;
      const runIdx = this.runs.findIndex((r) => r.id === run.id);
      const isLatestCard = runIdx >= 0 && runIdx === this.runs.length - 1;
      if (!isLatestCard) return false;
      const disp = this.displayStepsForRun(run);
      return globalIdx === (disp.length || 0) - 1;
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
  letter-spacing: 0.02em;
}
.phase-count {
  font-size: 11px;
  color: #64748b;
}
.refine-pipeline,
.polish-pipeline-hint {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 4px;
  margin: 0 0 10px 4px;
  font-size: 11px;
}
.pipe-arrow {
  color: #475569;
}
.pipe-node {
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid #3d4d64;
  color: #94a3b8;
  background: #1a1f2b;
}
.pipe-node.soft {
  border-style: dashed;
  opacity: 0.92;
}
.pipe-node.ok {
  border-color: rgba(52, 211, 153, 0.45);
  color: #6ee7b7;
}
.pipe-node.run {
  border-color: rgba(129, 140, 248, 0.55);
  color: #c7d2fe;
}
.pipe-node.err {
  border-color: rgba(248, 113, 113, 0.45);
  color: #fca5a5;
}
.pipe-node.idle {
  opacity: 0.55;
}
.narrative-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 8px 10px;
  margin-bottom: 4px;
  border-radius: 8px;
  background: rgba(99, 102, 241, 0.06);
  border: 1px solid rgba(99, 102, 241, 0.12);
}
.narrative-row.start {
  background: rgba(14, 165, 233, 0.06);
  border-color: rgba(14, 165, 233, 0.15);
}
.narrative-row.act {
  background: rgba(245, 158, 11, 0.06);
  border-color: rgba(245, 158, 11, 0.14);
}
.nar-icon {
  font-size: 16px;
  line-height: 1.2;
  flex-shrink: 0;
}
.nar-body {
  min-width: 0;
  flex: 1;
}
.nar-title {
  font-size: 12px;
  font-weight: 600;
  color: #cbd5e1;
}
.nar-sum {
  margin-top: 4px;
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.step-one.nar-attached {
  margin-top: -2px;
}
.event-summary {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.5;
  color: #e2e8f0;
  overflow-wrap: anywhere;
}
.think-preview {
  margin-bottom: 10px;
  border-radius: 8px;
  border: 1px solid #2f3a4d;
  background: #1a1f2b;
  padding: 6px 10px;
  font-size: 12px;
  color: #94a3b8;
}
.think-preview > summary {
  cursor: pointer;
  font-weight: 600;
  color: #a5b4fc;
}
.bundle-substeps {
  margin-bottom: 10px;
  border-radius: 8px;
  border: 1px solid #2f3a4d;
  background: #1a1f2b;
  padding: 6px 10px;
  font-size: 12px;
  color: #94a3b8;
}
.bundle-substeps > summary {
  cursor: pointer;
  font-weight: 600;
  color: #a5b4fc;
}
.bundle-ul {
  margin: 8px 0 0;
  padding-left: 1.1em;
  list-style: disc;
}
.bundle-li {
  margin: 4px 0;
  line-height: 1.45;
}
.bundle-name {
  margin-right: 6px;
}
.bundle-st {
  font-size: 11px;
  margin-right: 6px;
  opacity: 0.9;
}
.bundle-st.ok {
  color: #6ee7b7;
}
.bundle-st.running {
  color: #a5b4fc;
}
.bundle-st.error {
  color: #fca5a5;
}
.bundle-meta {
  font-size: 11px;
  color: #64748b;
}
.bundle-err {
  display: block;
  font-size: 11px;
  color: #fca5a5;
  margin-top: 2px;
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
  writing-mode: horizontal-tb;
  overflow-wrap: break-word;
  word-break: normal;
  line-break: auto;
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
.meta-toggle {
  margin-top: 10px;
  padding: 6px 10px;
  font-size: 12px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #1e2433;
  color: #a5b4fc;
  cursor: pointer;
}
.meta-toggle:hover {
  border-color: rgba(129, 140, 248, 0.45);
  color: #e0e7ff;
}
.meta-toggle-first {
  margin-top: 4px;
  margin-bottom: 2px;
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
