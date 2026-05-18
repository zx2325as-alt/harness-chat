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

// ── per-step line extractors ────────────────────────────────────────────────

function runtimeIntentLines(meta) {
  const lines = [];
  const ri = meta.runtime_intent;
  if (!ri || typeof ri !== "object") return lines;
  if (ri.reasoning_score != null) maybePush(lines, "推理强度", `${(ri.reasoning_score * 100).toFixed(0)}%`);
  if (ri.search_score != null) maybePush(lines, "检索需求", `${(ri.search_score * 100).toFixed(0)}%`);
  if (ri.risk_score != null) maybePush(lines, "风险评分", `${(ri.risk_score * 100).toFixed(0)}%`);
  if (ri.ambiguity_score != null) maybePush(lines, "歧义度", `${(ri.ambiguity_score * 100).toFixed(0)}%`);
  if (ri.latency_budget) maybePush(lines, "延迟预算", ri.latency_budget);
  if (ri.quality_requirement) maybePush(lines, "质量要求", ri.quality_requirement);
  return lines;
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
      lines.push(`计划节点（${plan.planned_nodes.length}）：${plan.planned_nodes.join(" → ")}`);
    }
  }
  const dynamicPlan = meta.dynamic_plan;
  if (dynamicPlan && typeof dynamicPlan === "object" && dynamicPlan.summary) {
    maybePush(lines, "执行摘要", clip(dynamicPlan.summary, 240));
  }
  return lines;
}

function draftLines(meta) {
  const lines = [];
  maybePush(lines, "起草模型", meta.model || meta.provider);
  if (meta.tokens_in != null || meta.tokens_out != null) {
    lines.push(`Token：输入 ${meta.tokens_in ?? "?"} / 输出 ${meta.tokens_out ?? "?"}`);
  }
  if (meta.draft_quality_score != null) {
    maybePush(lines, "草稿质量分", Number(meta.draft_quality_score).toFixed(3));
  }
  if (meta.parallel_mode === true) maybePush(lines, "并行起草", "是");
  if (meta.synthesized === true) maybePush(lines, "草稿合并", "是（synthesis）");
  if (meta.draft_len != null) maybePush(lines, "草稿长度", `${meta.draft_len} 字`);
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function criticLines(meta) {
  const lines = [];
  if (meta.round != null) maybePush(lines, "评估轮次", `第 ${meta.round} 轮`);
  if (meta.issue_total != null) maybePush(lines, "发现问题", `${meta.issue_total} 条`);
  maybePush(lines, "触发修复", boolText(meta.needs_repair));
  const facets = meta.facets && typeof meta.facets === "object" ? meta.facets : {};
  const faceMap = {
    coverage_ok: "覆盖",
    logic_ok: "逻辑",
    evidence_ok: "证据",
    hallucination_ok: "幻觉",
    policy_ok: "合规",
    style_ok: "风格",
  };
  const faceResults = [];
  for (const [k, label] of Object.entries(faceMap)) {
    if (facets[k] != null) {
      faceResults.push(`${label}${facets[k] ? "✓" : "✗"}`);
    }
  }
  if (faceResults.length) lines.push(`各维度：${faceResults.join("  ")}`);
  if (Array.isArray(meta.missing_points) && meta.missing_points.length) {
    lines.push(`缺失要点：${meta.missing_points.slice(0, 3).map((x) => clip(x, 80)).join("；")}`);
  }
  if (Array.isArray(meta.logic_issues) && meta.logic_issues.length) {
    lines.push(`逻辑问题：${meta.logic_issues.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (Array.isArray(meta.fact_risks) && meta.fact_risks.length) {
    lines.push(`事实风险：${meta.fact_risks.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (Array.isArray(meta.unsupported_claims) && meta.unsupported_claims.length) {
    lines.push(`待支撑断言：${meta.unsupported_claims.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function repairLines(meta) {
  const lines = [];
  if (meta.round != null) maybePush(lines, "修复轮次", `第 ${meta.round} 轮`);
  if (meta.guard_reverted === true) lines.push("守卫回退：修复幅度过大已还原原稿");
  maybePush(lines, "修复模型", meta.model || meta.provider);
  if (Array.isArray(meta.fix_claims) && meta.fix_claims.length) {
    lines.push(`修复断言（${meta.fix_claims.length}）：${meta.fix_claims.slice(0, 3).map((x) => clip(x, 80)).join("；")}`);
  }
  if (Array.isArray(meta.add_evidence) && meta.add_evidence.length) {
    lines.push(`补充证据（${meta.add_evidence.length}）：${meta.add_evidence.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (Array.isArray(meta.remove_hallucinations) && meta.remove_hallucinations.length) {
    lines.push(`删除臆测（${meta.remove_hallucinations.length}）：${meta.remove_hallucinations.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (meta.tokens_in != null || meta.tokens_out != null) {
    lines.push(`Token：输入 ${meta.tokens_in ?? "?"} / 输出 ${meta.tokens_out ?? "?"}`);
  }
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function verifyLines(meta) {
  const lines = [];
  if (meta.score != null) {
    const pct = (Number(meta.score) * 100).toFixed(0);
    maybePush(lines, "验证评分", `${pct}%`);
  }
  if (meta.claims_count != null) maybePush(lines, "提取断言", `${meta.claims_count} 条`);
  if (meta.mapping_method) {
    const m = meta.mapping_method;
    maybePush(lines, "映射方式", m === "dense" ? "稠密向量" : m === "ngram" ? "N-gram" : m === "mixed" ? "混合" : m);
  }
  const contra = meta.contradiction_heuristic;
  if (contra?.hit === true) {
    const sigs = Array.isArray(contra.signals) ? contra.signals.slice(0, 2).join("；") : "";
    lines.push(`矛盾检测：发现矛盾${sigs ? `（${sigs}）` : ""}`);
  }
  if (meta.unsupported_count != null) maybePush(lines, "不支持断言", `${meta.unsupported_count} 条`);
  if (Array.isArray(meta.unsupported_claims_heuristic) && meta.unsupported_claims_heuristic.length) {
    lines.push(`无据断言：${meta.unsupported_claims_heuristic.slice(0, 2).map((x) => clip(x, 80)).join("；")}`);
  }
  if (meta.recommended_action) maybePush(lines, "建议动作", meta.recommended_action);
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function gateLines(meta) {
  const lines = [];
  if (meta.completion_score != null) {
    maybePush(lines, "完成度", `${(Number(meta.completion_score) * 100).toFixed(0)}%`);
  }
  if (Array.isArray(meta.subgoals) && meta.subgoals.length) {
    lines.push(`子目标（${meta.subgoals.length}）：${meta.subgoals.slice(0, 4).map((x) => clip(x, 60)).join("；")}`);
  }
  if (Array.isArray(meta.resolved_goals) && meta.resolved_goals.length) {
    lines.push(`已解决：${meta.resolved_goals.slice(0, 3).map((x) => clip(x, 60)).join("；")}`);
  }
  if (Array.isArray(meta.queries_executed) && meta.queries_executed.length) {
    maybePush(lines, "执行查询", `${meta.queries_executed.length} 条`);
  }
  if (meta.confident === true) maybePush(lines, "置信", "是");
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function finalizeLines(meta) {
  const lines = [];
  maybePush(lines, "终稿模型", meta.model || meta.provider);
  if (meta.tokens_in != null || meta.tokens_out != null) {
    lines.push(`Token：输入 ${meta.tokens_in ?? "?"} / 输出 ${meta.tokens_out ?? "?"}`);
  }
  if (meta.final_len != null) maybePush(lines, "终稿长度", `${meta.final_len} 字`);
  if (meta.total_repair_rounds != null) maybePush(lines, "总修复轮次", `${meta.total_repair_rounds} 轮`);
  if (meta.event_summary) maybePush(lines, "摘要", clip(meta.event_summary, 280));
  return lines;
}

function searchExtraLines(meta) {
  const lines = [];
  if (Array.isArray(meta.queries) && meta.queries.length > 1) {
    lines.push(`并行查询（${meta.queries.length}）：${meta.queries.slice(0, 4).map((q) => clip(q, 80)).join("；")}`);
  }
  if (meta.evidence_count != null) maybePush(lines, "证据节点", `${meta.evidence_count} 条`);
  if (meta.cached === true) maybePush(lines, "缓存命中", "是");
  if (meta.authority_enabled === true) maybePush(lines, "权威度排序", "已启用");
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

    // ── intake ────────────────────────────────────────────────────────────
    if (step.name === "complexity_analyze") {
      const lines = [...complexityLines(meta), ...runtimeIntentLines(meta)];
      items.push({
        kind: "bullets",
        id: `complexity-${idx}`,
        icon: "🧩",
        title: humanStepName(step.name, meta),
        lines,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    if (step.name === "dag_runtime_plan") {
      const lines = [...planLines(meta), ...runtimeIntentLines(meta)];
      items.push({
        kind: "bullets",
        id: `plan-${idx}`,
        icon: "🗺",
        title: humanStepName(step.name, meta),
        lines,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── search ────────────────────────────────────────────────────────────
    if (step.name === "web_search" || step.name === "dag_parallel_search") {
      const sources = Array.isArray(meta.sources) ? meta.sources : [];
      const count = meta.result_count != null ? meta.result_count : sources.length;
      const extra = searchExtraLines(meta);
      items.push({
        kind: "search",
        id: `search-${idx}`,
        headline: count > 0 ? `检索到 ${count} 条结果` : "已执行检索",
        subline: humanStepName(step.name, meta),
        query: meta.query_effective || meta.query || "",
        provider: meta.provider_used || meta.provider || step.provider || "",
        status: step.status,
        sources,
        extra,
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

    // ── reasoning / gate ─────────────────────────────────────────────────
    if (step.name === "dag_tool_capability_gate" || step.name === "goal_capability_gate") {
      const gateMeta = { model: step.model, provider: step.provider, ...meta };
      items.push({
        kind: "bullets",
        id: `gate-${idx}`,
        icon: "🎯",
        title: humanStepName(step.name, meta),
        lines: gateLines(gateMeta),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── draft ─────────────────────────────────────────────────────────────
    if (step.name === "dag_draft" || step.name === "refine_layer1_draft" ||
        step.name === "refine_draft" || step.name === "refine_runtime_generate") {
      // backend may emit model/provider as top-level step fields; merge as fallback
      const draftMeta = { model: step.model, provider: step.provider, ...meta };
      items.push({
        kind: "bullets",
        id: `draft-${idx}`,
        icon: "✍️",
        title: humanStepName(step.name, meta),
        lines: draftLines(draftMeta),
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── evaluate ──────────────────────────────────────────────────────────
    if (step.name === "dag_parallel_critic" || step.name === "refine_layer2_review" ||
        step.name === "refine_quality_review" || step.name === "refine_runtime_critic") {
      const lines = criticLines(meta);
      const colorClass = meta.needs_repair === true ? "warn" : meta.needs_repair === false ? "ok" : "";
      items.push({
        kind: "bullets",
        id: `critic-${idx}`,
        icon: "🔎",
        title: humanStepName(step.name, meta),
        lines,
        colorClass,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── repair ────────────────────────────────────────────────────────────
    if (step.name === "dag_repair" || step.name === "refine_runtime_repair") {
      const repairMeta = { model: step.model, provider: step.provider, ...meta };
      items.push({
        kind: "bullets",
        id: `repair-${idx}`,
        icon: "🔧",
        title: humanStepName(step.name, meta),
        lines: repairLines(repairMeta),
        colorClass: meta.guard_reverted ? "warn" : "",
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── verify ────────────────────────────────────────────────────────────
    if (step.name === "dag_verify" || step.name === "refine_runtime_verify") {
      const score = meta.score != null ? Number(meta.score) : null;
      const colorClass = score != null ? (score >= 0.7 ? "ok" : score >= 0.4 ? "warn" : "err") : "";
      items.push({
        kind: "bullets",
        id: `verify-${idx}`,
        icon: "✅",
        title: humanStepName(step.name, meta),
        lines: verifyLines(meta),
        colorClass,
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── finalize ──────────────────────────────────────────────────────────
    if (step.name === "dag_finalize" || step.name === "refine_layer3_polish" ||
        step.name === "refine_finalize" || step.name === "refine_runtime_finalize") {
      const finMeta = { model: step.model, provider: step.provider, ...meta };
      items.push({
        kind: "bullets",
        id: `finalize-${idx}`,
        icon: "🏁",
        title: humanStepName(step.name, meta),
        lines: finalizeLines(finMeta),
        colorClass: "ok",
        status: step.status,
        latency_ms: step.latency_ms,
      });
      return;
    }

    // ── fallback: reason or generic step ─────────────────────────────────
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
