/**
 * 从单轮 run.steps 生成「思考过程」时间轴（与 StepDisplay 同源折叠规则，叙事更细）。
 */

const STEP_LABELS = {
  complexity_analyze: "复杂度分析",
  track_select: "轨道选择",
  web_search: "联网搜索",
  fast_route: "快速轨",
  refine_layer1_draft: "初稿层",
  refine_layer2_review: "结构化审查",
  refine_quality_review: "结构化审查",
  refine_layer3_polish: "润色层",
  refine_disabled_fallback_fast: "精化关闭 · 快轨",
  review_web_search: "审查 · 联网核查",
  refine_entry_web_search: "精化轨 · 入口联网",
  agent_start: "Agent 启动",
  agent_iteration: "Agent 推理",
  agent_web_search: "Agent · 联网",
  agent_plain_coerce_refine: "Agent · 转精化",
  agent_self_check: "高复杂度自检",
  agent_refine_answer: "Refine 润色",
  fast_answer_cache: "快轨缓存",
  agent_refine_fallback: "Agent 兜底 → Refine",
  agent_stuck_abort: "Agent · 重复终止",
  agent_progress_abort: "遗留 · 低进度终止",
  agent_postprocess_bundle: "后处理（自检/审查/润色）",
  web_search_policy: "联网策略",
  fast_track: "快速轨",
  fast_quality_critic: "快轨 · 质量评估",
  fast_answer_eval: "快轨 · 质量评估",
  fast_unified_critic: "快轨 · 统一质量评估",
  fast_critic_reject: "快轨 · 评估保留草案",
  refine_runtime_generate: "精化 · 生成初稿",
  refine_runtime_critic: "精化 · 结构化批评",
  refine_runtime_repair: "精化 · 定点修复",
  refine_runtime_verify: "精化 · 答案验证",
  refine_runtime_finalize: "精化 · 终稿排版",
  fast_escalation: "快轨 · 升级精化",
  fast_escalation_skip: "快轨 · 升级跳过",
  fast_escalation_complete: "快轨 · 升级完成",
  dag_runtime_plan: "DAG · 规划",
  web_search_policy: "联网策略",
  dag_parallel_search: "DAG · 并行检索",
  dag_draft: "DAG · 起草",
  dag_parallel_critic: "DAG · 并行批评",
  dag_repair: "DAG · 定点修复",
  dag_verify: "DAG · 验证",
  dag_finalize: "DAG · 终稿排版",
};

const PHASE_LABELS = {
  intake: "一、分析与调度",
  search: "二、联网检索",
  fast: "三、快速生成",
  reasoning: "四、Agent 推理",
  refine: "五、精化流水线",
  polishing: "六、润色与输出",
  other: "其他",
};

function inferPhaseGroup(step) {
  const m = (step && step.meta) || {};
  if (m.phase_group) return String(m.phase_group);
  const n = (step && step.name) || "";
  const table = {
    complexity_analyze: "intake",
    track_select: "intake",
    web_search: "search",
    refine_entry_web_search: "search",
    web_search_policy: "search",
    dag_parallel_search: "search",
  fast_route: "fast",
  fast_quality_critic: "fast",
  fast_answer_eval: "fast",
  fast_unified_critic: "fast",
  fast_critic_reject: "fast",
  fast_escalation: "fast",
  fast_escalation_skip: "fast",
  fast_escalation_complete: "fast",
  fast_answer_cache: "fast",
    refine_disabled_fallback_fast: "fast",
    agent_start: "reasoning",
    agent_iteration: "reasoning",
    agent_web_search: "reasoning",
    agent_stuck_abort: "reasoning",
    agent_progress_abort: "reasoning",
    agent_refine_fallback: "polishing",
    agent_plain_coerce_refine: "polishing",
    agent_self_check: "polishing",
    agent_refine_answer: "polishing",
    agent_postprocess_bundle: "polishing",
    refine_layer1_draft: "refine",
    refine_layer2_review: "refine",
    refine_quality_review: "refine",
    refine_layer3_polish: "refine",
    review_web_search: "refine",
    refine_runtime_generate: "refine",
    refine_runtime_critic: "refine",
    refine_runtime_repair: "refine",
    refine_runtime_verify: "refine",
    refine_runtime_finalize: "refine",
    dag_runtime_plan: "intake",
    dag_draft: "refine",
    dag_parallel_critic: "refine",
    dag_repair: "polishing",
    dag_verify: "refine",
    dag_finalize: "polishing",
  };
  return table[n] || "other";
}

export function displayStepsForRun(run) {
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
          event_summary: "后处理：自检 → 审查 → 润色",
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
    i += 1;
  }
  return out;
}

function humanName(name) {
  return STEP_LABELS[name] || name || "步骤";
}

function clip(s, max = 420) {
  const t = String(s || "").trim();
  if (!t) return "";
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

/** 预判步骤：多行推理要点（参考「子弹式思考」） */
function bulletsComplexity(m) {
  const lines = [];
  if (m.reason) lines.push(String(m.reason));
  if (Array.isArray(m.reasons) && m.reasons.length) {
    m.reasons.forEach((r) => {
      const x = String(r || "").trim();
      if (x) lines.push(x);
    });
  }
  if (m.complexity) lines.push(`复杂度档位：${m.complexity}`);
  if (m.task_type) lines.push(`任务类型：${m.task_type}`);
  if (m.type) lines.push(`意图归类：${m.type}`);
  if (m.decision) lines.push(`调度倾向：${m.decision}`);
  if (m.suggested_track) lines.push(`建议轨道：${m.suggested_track}`);
  if (m.confidence != null && m.confidence !== "") lines.push(`综合置信度：${m.confidence}`);
  if (m.search_required === true) lines.push("检索策略：需要补充实时或外部信息");
  else if (m.search_required === false) lines.push("检索策略：本轮未强制联网");
  if (m.search_query) lines.push(`检索要点建议：${clip(m.search_query, 200)}`);
  if (m.freshness_hint) lines.push(`时效提示：${clip(m.freshness_hint, 160)}`);
  if (m.analyzer_timed_out) lines.push("预判调用超时，已按规则降级继续");
  const mm = Array.isArray(m.manual_hits) && m.manual_hits.length ? m.manual_hits.join("、") : "";
  if (mm) lines.push(`快捷触发：${mm}`);
  return lines.filter(Boolean);
}

/** 轨道选择：结构化说明 */
function bulletsTrackSelect(m) {
  const lines = [];
  lines.push(`请求模式：${m.mode || "—"}`);
  if (m.track) lines.push(`实际选用轨道：${m.track}`);
  if (m.intended_track && m.intended_track !== m.track) lines.push(`原计划轨道：${m.intended_track}（已调整）`);
  if (m.task_type) lines.push(`任务类型：${m.task_type}`);
  if (m.complexity) lines.push(`复杂度标签：${m.complexity}`);
  if (m.agent_disabled_fallback) lines.push("说明：Agent 已在配置中关闭，已自动改用精化轨");
  if (m.search_required === true) lines.push("预判认为：适合补充检索后再答");
  if (m.response_style && m.response_style !== "normal") lines.push(`响应样式：${m.response_style}（不绑定轨道能力）`);
  if (m.capability_plan && typeof m.capability_plan === "object") {
    const p = m.capability_plan;
    lines.push(
      `能力规划：level=${p.capability_level || "—"} style=${p.response_style || "—"} search=${p.search_policy || "—"} risk=${p.risk_level || "—"}`
    );
  }
  if (Array.isArray(m.limitations) && m.limitations.length) {
    lines.push(`系统限制：${m.limitations.join("；")}`);
  }
  return lines.filter(Boolean);
}

/** 快轨路由 */
function bulletsFastRoute(m) {
  const lines = [];
  if (m.rule) lines.push(`路由规则：${m.rule}`);
  if (Array.isArray(m.candidates) && m.candidates.length) lines.push(`候选模型顺序：${m.candidates.join(" → ")}`);
  if (m.selected) lines.push(`本次首选：${m.selected}`);
  return lines.filter(Boolean);
}

/** Agent 启动 */
function bulletsAgentStart(m) {
  const lines = [];
  if (m.phase) lines.push(`阶段：${m.phase}`);
  if (m.model) lines.push(`主模型：${m.model}`);
  if (m.max_iterations != null) lines.push(`迭代上限：${m.max_iterations} 轮`);
  if (m.thread_turns != null) lines.push(`带入对话轮次：${m.thread_turns}`);
  return lines.filter(Boolean);
}

/** Agent 单轮迭代 */
function bulletsAgentIteration(m) {
  const lines = [];
  if (m.phase) lines.push(`环节：${m.phase}`);
  if (m.i != null && m.max != null) {
    lines.push(`进度：第 ${m.i} / ${m.max} 轮（后者为本轮 Agent 可用轮次上限，非本次必然跑满的次数）`);
  }
  if (m.model) lines.push(`调用模型：${m.model}`);
  if (m.branch_next) lines.push(`下一步：${m.branch_next}`);
  if (m.agent_loop_exit_hint) lines.push(`循环说明：${clip(m.agent_loop_exit_hint, 520)}`);
  if (m.reply_preview) lines.push(`本轮输出摘录：${clip(m.reply_preview, 560)}`);
  else if (m.reply_chars != null) lines.push(`本轮输出约 ${m.reply_chars} 字符`);
  return lines.filter(Boolean);
}

/** 精化层 draft/review/polish */
function bulletsRefineLayer(step) {
  const m = step.meta || {};
  const lines = [];
  if (m.phase) lines.push(`环节：${m.phase}`);
  if (Array.isArray(m.candidates) && m.candidates.length) lines.push(`候选模型：${m.candidates.join("、")}`);
  if (m.review_search_loops != null) lines.push(`审查联网轮次：${m.review_search_loops}`);
  if (m.reason) lines.push(String(m.reason));
  if (step.input_preview) lines.push(`输入摘要：${clip(step.input_preview, 560)}`);
  if (step.output) lines.push(`阶段产出节选：${clip(step.output, 720)}`);
  return lines.filter(Boolean);
}

/** 后处理 bundle：展开子步骤一行摘要 */
function bulletsPostprocessBundle(m) {
  const inner = (m._bundle_steps || []).slice();
  const lines = [];
  inner.forEach((sub) => {
    const sm = sub.meta || {};
    const piece = sm.event_summary || humanName(sub.name);
    lines.push(`${humanName(sub.name)}：${sub.status === "ok" ? "完成" : sub.status === "running" ? "进行中" : sub.status}${sub.latency_ms != null ? `（${sub.latency_ms}ms）` : ""}${piece ? ` — ${clip(piece, 120)}` : ""}`);
  });
  if (m._bundle_latency_sum_ms != null) lines.push(`子步骤耗时合计：${m._bundle_latency_sum_ms}ms`);
  return lines.filter(Boolean);
}

/**
 * @returns {Array<{ kind: string, [key: string]: any }>}
 */
export function buildThinkingTimeline(run) {
  const flat = displayStepsForRun(run);
  const items = [];
  let lastPhase = "";

  for (let i = 0; i < flat.length; i += 1) {
    const step = flat[i];
    const m = step.meta && typeof step.meta === "object" ? step.meta : {};
    const n = step.name || "";
    const g = inferPhaseGroup(step);

    if (g !== lastPhase) {
      lastPhase = g;
      items.push({ kind: "phase", id: `ph-${g}-${i}`, label: PHASE_LABELS[g] || g });
    }

    const isSearch =
      n === "web_search" ||
      n === "agent_web_search" ||
      n === "review_web_search" ||
      n === "refine_entry_web_search" ||
      n === "web_search_policy";

    if (isSearch) {
      const src = Array.isArray(m.sources) ? m.sources : [];
      const count = m.result_count != null ? m.result_count : src.length;
      const q = m.query_effective || m.query || "";
      const skipped = step.status === "skipped";

      if (skipped || count === 0) {
        items.push({
          kind: "reason",
          id: `r-${i}-sk`,
          title: humanName(n),
          text: m.event_summary || (skipped ? "联网步骤已跳过（策略或用户关闭检索）" : "未返回检索条目"),
          status: step.status,
          latency_ms: step.latency_ms,
        });
        continue;
      }

      items.push({
        kind: "search",
        id: `s-${i}-${n}`,
        name: n,
        headline: `搜索到 ${count} 个网页`,
        subline: humanName(n),
        count,
        query: q,
        provider: m.provider_used || m.provider || "",
        status: step.status,
        sources: src,
        summary: m.event_summary || "",
        latency_ms: step.latency_ms,
        skipped: false,
      });

      if (src.length > 0 && n !== "web_search_policy") {
        items.push({
          kind: "browse",
          id: `b-${i}-${n}`,
          pageCount: src.length,
          sources: src,
          note: m.event_summary ? clip(m.event_summary, 240) : "",
        });
      }
      continue;
    }

    if (n === "complexity_analyze") {
      const bullets = bulletsComplexity(m);
      if (bullets.length) {
        items.push({
          kind: "bullets",
          id: `bl-${i}-cx`,
          title: humanName(n),
          lines: bullets,
          status: step.status,
          latency_ms: step.latency_ms,
        });
      }
      continue;
    }

    if (n === "track_select") {
      items.push({
        kind: "bullets",
        id: `bl-${i}-tr`,
        title: humanName(n),
        lines: bulletsTrackSelect(m),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      continue;
    }

    if (n === "fast_route") {
      const lines = bulletsFastRoute(m);
      if (lines.length) {
        items.push({
          kind: "bullets",
          id: `bl-${i}-fr`,
          title: humanName(n),
          lines,
          status: step.status,
          latency_ms: step.latency_ms,
        });
      }
      continue;
    }

    if (n === "agent_start") {
      items.push({
        kind: "bullets",
        id: `bl-${i}-as`,
        title: humanName(n),
        lines: bulletsAgentStart(m),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      continue;
    }

    if (n === "agent_iteration") {
      const lines = bulletsAgentIteration(m);
      if (lines.length) {
        items.push({
          kind: "bullets",
          id: `bl-${i}-ai`,
          title: humanName(n),
          lines,
          status: step.status,
          latency_ms: step.latency_ms,
        });
      }
      continue;
    }

    if (n === "agent_postprocess_bundle") {
      const lines = bulletsPostprocessBundle(m);
      items.push({
        kind: "bullets",
        id: `bl-${i}-pp`,
        title: humanName(n),
        lines: lines.length ? lines : ["自检 → 审查 → 润色流水线"],
        status: step.status,
        latency_ms: step.latency_ms,
      });
      continue;
    }

    if (n.indexOf("refine_layer") === 0) {
      const lines = bulletsRefineLayer(step);
      items.push({
        kind: "bullets",
        id: `bl-${i}-rf`,
        title: humanName(n),
        lines: lines.length ? lines : [step.status === "ok" ? "本层已完成" : "本层执行中"],
        status: step.status,
        latency_ms: step.latency_ms,
      });
      continue;
    }

    if (m.event_summary) {
      items.push({
        kind: "reason",
        id: `r-${i}`,
        title: humanName(n),
        text: m.event_summary,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      continue;
    }

    items.push({
      kind: "step",
      id: `st-${i}`,
      title: humanName(n),
      status: step.status,
      latency_ms: step.latency_ms,
      provider: step.provider,
      detail: clip(step.input_preview || step.output || "", 200),
    });
  }

  return items;
}

export function faviconUrlForLink(url) {
  try {
    const u = new URL(String(url).trim().startsWith("http") ? String(url).trim() : `https://${String(url).trim()}`);
    return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(u.hostname)}&sz=32`;
  } catch {
    return "";
  }
}

export function uniqueDomainsFromSources(sources, maxN = 6) {
  const out = [];
  const seen = new Set();
  for (const s of sources || []) {
    const url = s && s.url;
    if (!url) continue;
    try {
      const host = new URL(String(url).trim().startsWith("http") ? url : `https://${url}`).hostname;
      if (host && !seen.has(host)) {
        seen.add(host);
        out.push(host);
        if (out.length >= maxN) break;
      }
    } catch {
      /* ignore */
    }
  }
  return out;
}
