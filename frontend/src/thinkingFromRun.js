const STEP_LABELS = {
  complexity_analyze: "复杂度分析",
  dag_runtime_plan: "运行时规划",
  web_search_policy: "联网策略",
  web_search: "联网检索",
  dag_parallel_search: "并行检索",
  dag_tool_capability_gate: "工具能力门",
  goal_capability_gate: "目标能力门",
  dag_draft: "起草",
  dag_parallel_critic: "评估",
  dag_repair: "修复",
  dag_verify: "验证",
  dag_finalize: "终稿",
  refine_layer1_draft: "起草",
  refine_draft: "起草",
  refine_runtime_generate: "起草",
  refine_layer2_review: "评估",
  refine_quality_review: "评估",
  refine_runtime_critic: "评估",
  refine_runtime_repair: "修复",
  refine_runtime_verify: "验证",
  refine_layer3_polish: "终稿润色",
  refine_finalize: "终稿",
  refine_runtime_finalize: "终稿",
  agent_start: "推理初始化",
  agent_iteration: "推理",
  agent_web_search: "联网检索",
};

const PHASE_LABELS = {
  intake: "一、分析与规划",
  search: "二、检索与证据",
  reasoning: "三、能力与工具",
  draft: "四、起草",
  evaluate: "五、评估",
  repair: "六、修复",
  verify: "七、验证",
  finalize: "八、终稿",
  other: "其他",
};

const PHASE_BY_STEP = {
  complexity_analyze: "intake",
  dag_runtime_plan: "intake",
  web_search_policy: "intake",
  web_search: "search",
  dag_parallel_search: "search",
  review_web_search: "search",
  refine_entry_web_search: "search",
  dag_tool_capability_gate: "reasoning",
  goal_capability_gate: "reasoning",
  agent_start: "reasoning",
  agent_iteration: "reasoning",
  agent_web_search: "search",
  dag_draft: "draft",
  refine_layer1_draft: "draft",
  refine_draft: "draft",
  refine_runtime_generate: "draft",
  dag_parallel_critic: "evaluate",
  refine_layer2_review: "evaluate",
  refine_quality_review: "evaluate",
  refine_runtime_critic: "evaluate",
  dag_repair: "repair",
  refine_runtime_repair: "repair",
  dag_verify: "verify",
  refine_runtime_verify: "verify",
  dag_finalize: "finalize",
  refine_layer3_polish: "finalize",
  refine_finalize: "finalize",
  refine_runtime_finalize: "finalize",
};

function clip(text, max = 240) {
  const value = String(text || "").trim();
  if (!value) return "";
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function boolText(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  return "";
}

function maybePush(lines, label, value) {
  if (value === undefined || value === null || value === "") return;
  lines.push(`${label}：${value}`);
}

export function humanStepName(name, meta = {}) {
  const base = STEP_LABELS[name] || name || "步骤";
  if (name === "dag_parallel_critic" && meta.round != null) {
    return `${base} · 第 ${meta.round} 轮`;
  }
  if (name === "dag_repair" && meta.round != null) {
    return `${base} · 第 ${meta.round} 轮`;
  }
  if (name === "dag_verify" && meta.round != null) {
    return `${base} · 第 ${meta.round} 轮`;
  }
  return base;
}

export function inferPhaseGroup(step) {
  const meta = step?.meta || {};
  if (meta.phase_group) return String(meta.phase_group);
  return PHASE_BY_STEP[step?.name || ""] || "other";
}

export function displayStepsForRun(run) {
  return Array.isArray(run?.steps) ? run.steps.slice() : [];
}

function complexityLines(meta) {
  const lines = [];
  if (meta.reason) maybePush(lines, "判定摘要", clip(meta.reason, 360));
  if (Array.isArray(meta.reasons)) {
    meta.reasons
      .map((item) => clip(item, 180))
      .filter(Boolean)
      .forEach((item) => lines.push(item));
  }
  maybePush(lines, "复杂度", meta.complexity);
  maybePush(lines, "任务类型", meta.task_type);
  maybePush(lines, "意图归类", meta.type);
  if (meta.search_required === true) maybePush(lines, "需要联网", "是");
  if (meta.search_required === false) maybePush(lines, "需要联网", "否");
  maybePush(lines, "检索查询", clip(meta.search_query, 180));
  maybePush(lines, "时效提示", clip(meta.freshness_hint, 180));
  if (meta.confidence != null && meta.confidence !== "") maybePush(lines, "置信度", meta.confidence);
  maybePush(lines, "首选模型", meta.selected_model);
  if (Array.isArray(meta.fallback_models) && meta.fallback_models.length) {
    maybePush(lines, "回退模型", meta.fallback_models.join(" → "));
  }
  return lines;
}

function planLines(meta) {
  const lines = [];
  maybePush(lines, "架构", meta.architecture);
  maybePush(lines, "调度器", meta.scheduler);
  const plan = meta.plan && typeof meta.plan === "object" ? meta.plan : {};
  if (Object.keys(plan).length) {
    maybePush(lines, "并行检索", plan.parallel_search_queries);
    maybePush(lines, "并行评估", boolText(plan.parallel_critics));
    maybePush(lines, "分层评估", boolText(plan.layered_critics));
    maybePush(lines, "并行起草", boolText(plan.parallel_drafts));
    maybePush(lines, "最大修复轮次", plan.max_repair_rounds);
    maybePush(lines, "工具能力门", boolText(plan.tool_capability_gate));
    maybePush(lines, "目标子图", boolText(plan.goal_subgraph));
    if (Array.isArray(plan.planned_nodes) && plan.planned_nodes.length) {
      maybePush(lines, "计划节点", `${plan.planned_nodes.length} 个`);
    }
  }
  const dynamicPlan = meta.dynamic_plan;
  if (dynamicPlan && typeof dynamicPlan === "object") {
    maybePush(lines, "执行摘要", clip(JSON.stringify(dynamicPlan), 360));
  }
  return lines;
}

export function buildThinkingTimeline(run) {
  const steps = displayStepsForRun(run);
  if (!steps.length) return [];

  const items = [];
  let lastPhase = "";

  steps.forEach((step, idx) => {
    const meta = step?.meta && typeof step.meta === "object" ? step.meta : {};
    const phase = inferPhaseGroup(step);
    if (phase !== lastPhase) {
      lastPhase = phase;
      items.push({ kind: "phase", id: `phase-${phase}-${idx}`, label: PHASE_LABELS[phase] || phase });
    }

    if (step.name === "complexity_analyze") {
      items.push({
        kind: "bullets",
        id: `complexity-${idx}`,
        title: humanStepName(step.name, meta),
        lines: complexityLines(meta),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    if (step.name === "dag_runtime_plan") {
      items.push({
        kind: "bullets",
        id: `plan-${idx}`,
        title: humanStepName(step.name, meta),
        lines: planLines(meta),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    if (step.name === "web_search" || step.name === "dag_parallel_search") {
      const sources = Array.isArray(meta.sources) ? meta.sources : [];
      const count = meta.result_count != null ? meta.result_count : sources.length;
      items.push({
        kind: "search",
        id: `search-${idx}`,
        headline: count > 0 ? `检索到 ${count} 条结果` : "已执行检索",
        subline: humanStepName(step.name, meta),
        query: meta.query_effective || meta.query || "",
        provider: meta.provider_used || meta.provider || step.provider || "",
        status: step.status,
        sources,
        summary: meta.event_summary || "",
        latency_ms: step.latency_ms,
      });
      if (sources.length) {
        items.push({
          kind: "browse",
          id: `browse-${idx}`,
          pageCount: sources.length,
          sources,
          note: meta.event_summary ? clip(meta.event_summary, 240) : "",
        });
      }
      return;
    }

    if (meta.event_summary) {
      items.push({
        kind: "reason",
        id: `reason-${idx}`,
        title: humanStepName(step.name, meta),
        text: meta.event_summary,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    items.push({
      kind: "step",
      id: `step-${idx}`,
      title: humanStepName(step.name, meta),
      status: step.status,
      latency_ms: step.latency_ms,
      provider: step.provider,
      detail: clip(step.input_preview || step.output || "", 200),
    });
  });

  return items;
}

export function faviconUrlForLink(url) {
  try {
    const parsed = new URL(String(url).trim().startsWith("http") ? String(url).trim() : `https://${String(url).trim()}`);
    return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(parsed.hostname)}&sz=32`;
  } catch {
    return "";
  }
}

export function uniqueDomainsFromSources(sources, maxN = 6) {
  const out = [];
  const seen = new Set();
  for (const src of sources || []) {
    const url = src?.url;
    if (!url) continue;
    try {
      const parsed = new URL(String(url).trim().startsWith("http") ? String(url).trim() : `https://${String(url).trim()}`);
      if (!seen.has(parsed.hostname)) {
        seen.add(parsed.hostname);
        out.push(parsed.hostname);
      }
      if (out.length >= maxN) break;
    } catch {
      /* ignore */
    }
  }
  return out;
}
