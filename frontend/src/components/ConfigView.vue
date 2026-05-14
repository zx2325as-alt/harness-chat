<template>
  <div class="wrap">
    <div class="row">
      <div class="title">运行时可写配置</div>
      <div class="spacer" />
      <button class="btn" :disabled="loading" @click="$emit('reload')">刷新</button>
      <button class="btn primary" :disabled="loading || !editedHarness || saving" @click="saveRuntime">
        {{ saving ? "保存中…" : "保存并生效" }}
      </button>
    </div>

    <div v-if="config?.routing_notes" class="note">
      <div v-if="config.routing_notes.runtime"><strong>运行时：</strong>{{ config.routing_notes.runtime }}</div>
      <div v-if="config.routing_notes.sync_stream_contract"><strong>同步 / 流式：</strong>{{ config.routing_notes.sync_stream_contract }}</div>
    </div>

    <p v-if="config" class="meta">
      基础文件：<code>config.yaml</code>；可写叠加层：
      <code>{{ config.runtime_overlay_path || "config.runtime.yaml" }}</code>
      <span v-if="config.runtime_overlay_active" class="tag on">已存在叠加</span>
      <span v-else class="tag">尚未创建叠加文件</span>
    </p>

    <div v-if="saveMsg" class="save-banner" :class="saveOk ? 'ok' : 'err'">{{ saveMsg }}</div>
    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <div v-else-if="editedHarness" class="config-root">
      <section class="card-grid">
        <article class="card">
          <h3>运行时基础</h3>
          <label class="field">
            <span class="field-label">runtime</span>
            <input v-model="editedHarness.runtime" class="inp" />
          </label>
          <label class="field inline">
            <span class="field-label">stream_slice_chars</span>
            <input v-model.number="editedHarness.stream_slice_chars" type="number" min="24" max="256" class="inp" />
          </label>
          <label class="row-check">
            <input v-model="webSearchCfg.globally_disabled" type="checkbox" />
            <span>全局禁止联网</span>
          </label>
        </article>

        <article class="card">
          <h3>执行规划</h3>
          <label class="field inline"><span class="field-label">parallel_search_queries</span><input v-model.number="dagRuntime.parallel_search_queries" type="number" min="1" max="8" class="inp" /></label>
          <label class="field inline"><span class="field-label">hedge_draft_delay_ms</span><input v-model.number="dagRuntime.hedge_draft_delay_ms" type="number" min="0" max="5000" class="inp" /></label>
          <label class="field inline"><span class="field-label">max_repair_rounds</span><input v-model.number="dagRuntime.max_repair_rounds" type="number" min="0" max="8" class="inp" /></label>
          <label class="row-check"><input v-model="dagRuntime.parallel_critics" type="checkbox" /><span>parallel_critics</span></label>
          <label class="row-check"><input v-model="dagRuntime.layered_critics" type="checkbox" /><span>layered_critics</span></label>
          <label class="row-check"><input v-model="dagRuntime.parallel_drafts" type="checkbox" /><span>parallel_drafts</span></label>
          <label class="row-check"><input v-model="dagRuntime.goal_subgraph_enabled" type="checkbox" /><span>goal_subgraph_enabled</span></label>
          <label class="row-check"><input v-model="dagRuntime.tool_capability_gate_enabled" type="checkbox" /><span>tool_capability_gate_enabled</span></label>
          <label class="row-check"><input v-model="dagRuntime.semantic_cache_short_circuit" type="checkbox" /><span>semantic_cache_short_circuit</span></label>
        </article>

        <article class="card">
          <h3>运行时编排</h3>
          <label class="row-check"><input v-model="runtimeOrchestrator.enabled" type="checkbox" /><span>runtime_orchestrator.enabled</span></label>
          <label class="field inline"><span class="field-label">search_budget_per_request</span><input v-model.number="runtimeOrchestrator.search_budget_per_request" type="number" min="1" max="200" class="inp" /></label>
          <label class="row-check"><input v-model="unifiedCritic.enabled" type="checkbox" /><span>unified_critic.enabled</span></label>
          <label class="field"><span class="field-label">unified_critic.model_key</span><select v-model="unifiedCritic.model_key" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`uc-${k}`" :value="k">{{ k }}</option></select></label>
          <label class="row-check"><input v-model="searchSufficiency.enabled" type="checkbox" /><span>search_sufficiency.enabled</span></label>
          <label class="field"><span class="field-label">search_sufficiency.model_key</span><select v-model="searchSufficiency.model_key" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`ss-${k}`" :value="k">{{ k }}</option></select></label>
          <label class="field"><span class="field-label">metrics_sqlite_path</span><input v-model="observability.metrics_sqlite_path" class="inp" /></label>
          <label class="field"><span class="field-label">metrics_jsonl_path</span><input v-model="observability.metrics_jsonl_path" class="inp" /></label>
        </article>

        <article class="card card-wide">
          <h3>检索策略</h3>
          <div class="grid two">
            <label class="field"><span class="field-label">provider</span><input v-model="searchCfg.provider" class="inp" /></label>
            <label class="field"><span class="field-label">fallback</span><input v-model="searchCfg.fallback" class="inp" /></label>
            <label class="field"><span class="field-label">search_depth</span><select v-model="searchCfg.search_depth" class="inp"><option value="basic">basic</option><option value="advanced">advanced</option></select></label>
            <label class="field"><span class="field-label">topic</span><input v-model="searchCfg.topic" class="inp" /></label>
            <label class="field inline"><span class="field-label">max_results</span><input v-model.number="searchCfg.max_results" type="number" min="1" max="24" class="inp" /></label>
            <label class="field inline"><span class="field-label">timeout_s</span><input v-model.number="searchCfg.timeout_s" type="number" min="1" max="120" class="inp" /></label>
            <label class="field inline"><span class="field-label">timeout_s_max</span><input v-model.number="searchCfg.timeout_s_max" type="number" min="1" max="180" class="inp" /></label>
            <label class="field inline"><span class="field-label">session_cache_ttl_s</span><input v-model.number="searchCfg.session_cache_ttl_s" type="number" min="0" max="86400" class="inp" /></label>
            <label class="field inline"><span class="field-label">session_cache_ttl_freshness_s</span><input v-model.number="searchCfg.session_cache_ttl_freshness_s" type="number" min="0" max="86400" class="inp" /></label>
            <label class="field inline"><span class="field-label">session_cache_ttl_required_s</span><input v-model.number="searchCfg.session_cache_ttl_required_s" type="number" min="0" max="86400" class="inp" /></label>
            <label class="field inline"><span class="field-label">session_cache_ttl_explicit_s</span><input v-model.number="searchCfg.session_cache_ttl_explicit_s" type="number" min="0" max="86400" class="inp" /></label>
          </div>
          <label class="row-check"><input v-model="searchCfg.query_enrich" type="checkbox" /><span>query_enrich</span></label>
          <label class="row-check"><input v-model="searchCfg.include_answer" type="checkbox" /><span>include_answer</span></label>
          <div class="subcard">
            <div class="subcard-title">by_intent</div>
            <div class="grid two">
              <label class="field inline"><span class="field-label">search.max_results</span><input v-model.number="searchByIntent.search.max_results" type="number" min="1" max="24" class="inp" /></label>
              <label class="field"><span class="field-label">search.search_depth</span><select v-model="searchByIntent.search.search_depth" class="inp"><option value="basic">basic</option><option value="advanced">advanced</option></select></label>
              <label class="field inline"><span class="field-label">dag.max_results</span><input v-model.number="searchByIntent.dag.max_results" type="number" min="1" max="24" class="inp" /></label>
              <label class="field"><span class="field-label">dag.search_depth</span><select v-model="searchByIntent.dag.search_depth" class="inp"><option value="basic">basic</option><option value="advanced">advanced</option></select></label>
            </div>
          </div>
          <div class="subcard">
            <div class="subcard-title">relevance_filter</div>
            <label class="row-check"><input v-model="searchRelevance.enabled" type="checkbox" /><span>enabled</span></label>
            <div class="grid two">
              <label class="field"><span class="field-label">model</span><select v-model="searchRelevance.model" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`rf-${k}`" :value="k">{{ k }}</option></select></label>
              <label class="field"><span class="field-label">sync_default_mode</span><select v-model="searchRelevance.sync_default_mode" class="inp"><option value="runtime_phases">runtime_phases</option><option value="always">always</option><option value="never">never</option></select></label>
            </div>
          </div>
          <label class="field"><span class="field-label">speculative_markers（每行一个）</span><textarea :value="joinLines(searchCfg.speculative_markers)" class="ta" rows="4" @input="searchCfg.speculative_markers = parseLines($event.target.value)" /></label>
        </article>

        <article class="card card-wide">
          <h3>文档检索</h3>
          <div class="grid two">
            <label class="field inline"><span class="field-label">bm25_weight</span><input v-model.number="documents.bm25_weight" type="number" min="0" max="1" step="0.05" class="inp" /></label>
            <label class="field inline"><span class="field-label">embedding_weight</span><input v-model.number="documents.embedding_weight" type="number" min="0" max="1" step="0.05" class="inp" /></label>
            <label class="field inline"><span class="field-label">max_text_chars</span><input v-model.number="documents.max_text_chars" type="number" min="1000" max="1000000" class="inp" /></label>
            <label class="field inline"><span class="field-label">chunk_size</span><input v-model.number="documents.chunk_size" type="number" min="100" max="20000" class="inp" /></label>
            <label class="field inline"><span class="field-label">chunk_overlap</span><input v-model.number="documents.chunk_overlap" type="number" min="0" max="5000" class="inp" /></label>
            <label class="field inline"><span class="field-label">max_pdf_pages</span><input v-model.number="documents.max_pdf_pages" type="number" min="1" max="500" class="inp" /></label>
            <label class="field inline"><span class="field-label">max_sheet_rows</span><input v-model.number="documents.max_sheet_rows" type="number" min="1" max="10000" class="inp" /></label>
          </div>
          <div class="subcard">
            <div class="subcard-title">embedding</div>
            <label class="row-check"><input v-model="docEmbedding.enabled" type="checkbox" /><span>enabled</span></label>
            <div class="grid two">
              <label class="field"><span class="field-label">model_key</span><select v-model="docEmbedding.model_key" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`emb-${k}`" :value="k">{{ k }}</option></select></label>
              <label class="field inline"><span class="field-label">max_items</span><input v-model.number="docEmbedding.max_items" type="number" min="1" max="200" class="inp" /></label>
              <label class="field inline"><span class="field-label">text_chars</span><input v-model.number="docEmbedding.text_chars" type="number" min="100" max="20000" class="inp" /></label>
              <label class="field inline"><span class="field-label">timeout_s</span><input v-model.number="docEmbedding.timeout_s" type="number" min="1" max="120" class="inp" /></label>
              <label class="field inline"><span class="field-label">rrf_k</span><input v-model.number="docEmbedding.rrf_k" type="number" min="1" max="500" class="inp" /></label>
            </div>
          </div>
          <div class="subcard">
            <div class="subcard-title">rerank / compact</div>
            <label class="row-check"><input v-model="docRerank.enabled" type="checkbox" /><span>rerank.enabled</span></label>
            <div class="grid two">
              <label class="field"><span class="field-label">rerank.model</span><select v-model="docRerank.model" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`rr-${k}`" :value="k">{{ k }}</option></select></label>
              <label class="field inline"><span class="field-label">rerank.max_items</span><input v-model.number="docRerank.max_items" type="number" min="1" max="200" class="inp" /></label>
              <label class="field inline"><span class="field-label">rerank.top_k</span><input v-model.number="docRerank.top_k" type="number" min="1" max="50" class="inp" /></label>
              <label class="field inline"><span class="field-label">compact.max_items</span><input v-model.number="docCompact.max_items" type="number" min="1" max="50" class="inp" /></label>
            </div>
          </div>
        </article>

        <article class="card">
          <h3>复杂度分析</h3>
          <label class="row-check"><input v-model="complexity.use_llm_analyzer" type="checkbox" /><span>use_llm_analyzer</span></label>
          <label class="field"><span class="field-label">analyzer_model</span><select v-model="complexity.analyzer_model" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`an-${k}`" :value="k">{{ k }}</option></select></label>
          <label class="field inline"><span class="field-label">analyzer_request_timeout_s</span><input v-model.number="complexity.analyzer_request_timeout_s" type="number" min="1" max="180" class="inp" /></label>
          <label class="field inline"><span class="field-label">analyzer_max_retries</span><input v-model.number="complexity.analyzer_max_retries" type="number" min="0" max="8" class="inp" /></label>
          <label class="field inline"><span class="field-label">analyzer_total_timeout_s</span><input v-model.number="complexity.analyzer_total_timeout_s" type="number" min="1" max="300" class="inp" /></label>
          <label class="field inline"><span class="field-label">analysis_cache_ttl_s</span><input v-model.number="complexity.analysis_cache_ttl_s" type="number" min="0" max="86400" class="inp" /></label>
          <label class="field"><span class="field-label">analyzer_prompt</span><textarea v-model="complexity.analyzer_prompt" class="ta" rows="8" /></label>
        </article>

        <article class="card card-wide">
          <h3>任务模型模板</h3>
          <div class="template-grid">
            <div v-for="(tpl, key) in taskTemplates" :key="key" class="subcard template-card">
              <div class="subcard-title">{{ key }}</div>
              <label class="field"><span class="field-label">selected_model</span><select v-model="tpl.selected_model" class="inp"><option value="">（未设置）</option><option v-for="k in modelKeys" :key="`${key}-sel-${k}`" :value="k">{{ k }}</option></select></label>
              <label class="field"><span class="field-label">fallback_models（每行一个）</span><textarea :value="joinLines(tpl.fallback_models)" class="ta" rows="3" @input="tpl.fallback_models = parseLines($event.target.value)" /></label>
              <template v-if="tpl.quality_models">
                <label class="field"><span class="field-label">quality_models.draft</span><textarea :value="joinLines(tpl.quality_models.draft)" class="ta" rows="2" @input="tpl.quality_models.draft = parseLines($event.target.value)" /></label>
                <label class="field"><span class="field-label">quality_models.review</span><textarea :value="joinLines(tpl.quality_models.review)" class="ta" rows="2" @input="tpl.quality_models.review = parseLines($event.target.value)" /></label>
                <label class="field"><span class="field-label">quality_models.polish</span><textarea :value="joinLines(tpl.quality_models.polish)" class="ta" rows="2" @input="tpl.quality_models.polish = parseLines($event.target.value)" /></label>
              </template>
            </div>
          </div>
        </article>

        <article class="card card-wide">
          <h3>质量流程</h3>
          <label class="row-check"><input v-model="qualityPipeline.enabled" type="checkbox" /><span>quality_pipeline.enabled</span></label>
          <div class="grid three">
            <label class="field inline"><span class="field-label">repair.temperature</span><input v-model.number="qualityRepair.temperature" type="number" min="0" max="2" step="0.05" class="inp" /></label>
            <label class="field inline"><span class="field-label">layer1.temperature</span><input v-model.number="qualityLayer1.temperature" type="number" min="0" max="2" step="0.05" class="inp" /></label>
            <label class="field inline"><span class="field-label">layer2.temperature</span><input v-model.number="qualityLayer2.temperature" type="number" min="0" max="2" step="0.05" class="inp" /></label>
            <label class="field inline"><span class="field-label">layer3.temperature</span><input v-model.number="qualityLayer3.temperature" type="number" min="0" max="2" step="0.05" class="inp" /></label>
          </div>
          <div class="grid three">
            <label class="field"><span class="field-label">layer1.name</span><input v-model="qualityLayer1.name" class="inp" /></label>
            <label class="field"><span class="field-label">layer2.name</span><input v-model="qualityLayer2.name" class="inp" /></label>
            <label class="field"><span class="field-label">layer3.name</span><input v-model="qualityLayer3.name" class="inp" /></label>
          </div>
          <label class="field"><span class="field-label">layer1.instruction</span><textarea v-model="qualityLayer1.instruction" class="ta" rows="6" /></label>
          <label class="field"><span class="field-label">layer2.instruction</span><textarea v-model="qualityLayer2.instruction" class="ta" rows="8" /></label>
          <label class="field"><span class="field-label">layer3.instruction</span><textarea v-model="qualityLayer3.instruction" class="ta" rows="5" /></label>
        </article>

        <article class="card">
          <h3>流式输出</h3>
          <label class="row-check"><input v-model="streamTuning.smart_chunk_boundary" type="checkbox" /><span>smart_chunk_boundary</span></label>
          <label class="row-check"><input v-model="streamTuning.emit_content_reset" type="checkbox" /><span>emit_content_reset</span></label>
        </article>
      </section>

      <section class="json-panel">
        <header class="json-panel-head">
          <span class="json-panel-title">完整 harness JSON（只读）</span>
          <span class="json-panel-hint">保存时整体提交；密钥类字段仍会在服务端剥离。</span>
        </header>
        <pre class="mono">{{ pretty(editedHarness) }}</pre>
      </section>
    </div>
  </div>
</template>

<script>
import { API_BASE } from "../apiBase.js";

export default {
  name: "ConfigView",
  props: {
    config: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    error: { type: String, default: "" },
  },
  emits: ["reload"],
  data() {
    return {
      editedHarness: null,
      saving: false,
      saveMsg: "",
      saveOk: false,
    };
  },
  computed: {
    modelKeys() {
      return Array.isArray(this.config?.models) ? this.config.models.slice() : [];
    },
    webSearchCfg() {
      return this.editedHarness?.web_search || {};
    },
    dagRuntime() {
      return this.editedHarness?.dag_runtime || {};
    },
    runtimeOrchestrator() {
      return this.editedHarness?.runtime_orchestrator || {};
    },
    unifiedCritic() {
      return this.runtimeOrchestrator.unified_critic || {};
    },
    searchSufficiency() {
      return this.runtimeOrchestrator.search_sufficiency || {};
    },
    observability() {
      return this.runtimeOrchestrator.observability || {};
    },
    streamTuning() {
      return this.editedHarness?.stream_tuning || {};
    },
    searchCfg() {
      return this.editedHarness?.search || {};
    },
    searchByIntent() {
      return this.searchCfg.by_intent || {};
    },
    searchRelevance() {
      return this.searchCfg.relevance_filter || {};
    },
    documents() {
      return this.editedHarness?.documents || {};
    },
    docEmbedding() {
      return this.documents.embedding || {};
    },
    docRerank() {
      return this.documents.rerank || {};
    },
    docCompact() {
      return this.documents.compact || {};
    },
    taskTemplates() {
      return this.editedHarness?.task_model_templates || {};
    },
    complexity() {
      return this.editedHarness?.complexity || {};
    },
    qualityPipeline() {
      return this.editedHarness?.quality_pipeline || {};
    },
    qualityRepair() {
      return this.qualityPipeline.repair || {};
    },
    qualityLayer1() {
      return this.qualityPipeline.layer1 || {};
    },
    qualityLayer2() {
      return this.qualityPipeline.layer2 || {};
    },
    qualityLayer3() {
      return this.qualityPipeline.layer3 || {};
    },
  },
  watch: {
    config: {
      deep: true,
      immediate: true,
      handler(config) {
        if (!config?.harness) {
          this.editedHarness = null;
          return;
        }
        this.editedHarness = JSON.parse(JSON.stringify(config.harness));
        this.ensureShapes();
      },
    },
  },
  methods: {
    pretty(obj) {
      try {
        return JSON.stringify(obj ?? null, null, 2);
      } catch {
        return String(obj);
      }
    },
    joinLines(value) {
      return Array.isArray(value) ? value.join("\n") : "";
    },
    parseLines(value) {
      return String(value || "")
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
    },
    ensureShapes() {
      const h = this.editedHarness;
      if (!h) return;

      if (!h.runtime) h.runtime = "adaptive_dag_v3";
      if (h.stream_slice_chars == null) h.stream_slice_chars = 72;

      h.web_search = h.web_search || {};
      if (typeof h.web_search.globally_disabled !== "boolean") h.web_search.globally_disabled = false;

      h.dag_runtime = h.dag_runtime || {};
      if (h.dag_runtime.parallel_search_queries == null) h.dag_runtime.parallel_search_queries = 2;
      if (typeof h.dag_runtime.parallel_critics !== "boolean") h.dag_runtime.parallel_critics = true;
      if (typeof h.dag_runtime.layered_critics !== "boolean") h.dag_runtime.layered_critics = true;
      if (typeof h.dag_runtime.parallel_drafts !== "boolean") h.dag_runtime.parallel_drafts = true;
      if (h.dag_runtime.hedge_draft_delay_ms == null) h.dag_runtime.hedge_draft_delay_ms = 0;
      if (h.dag_runtime.max_repair_rounds == null) h.dag_runtime.max_repair_rounds = 2;
      if (typeof h.dag_runtime.goal_subgraph_enabled !== "boolean") h.dag_runtime.goal_subgraph_enabled = false;
      if (typeof h.dag_runtime.tool_capability_gate_enabled !== "boolean") h.dag_runtime.tool_capability_gate_enabled = true;
      if (typeof h.dag_runtime.semantic_cache_short_circuit !== "boolean") h.dag_runtime.semantic_cache_short_circuit = false;

      h.runtime_orchestrator = h.runtime_orchestrator || {};
      if (typeof h.runtime_orchestrator.enabled !== "boolean") h.runtime_orchestrator.enabled = true;
      if (h.runtime_orchestrator.search_budget_per_request == null) h.runtime_orchestrator.search_budget_per_request = 24;
      h.runtime_orchestrator.unified_critic = h.runtime_orchestrator.unified_critic || {};
      if (typeof h.runtime_orchestrator.unified_critic.enabled !== "boolean") h.runtime_orchestrator.unified_critic.enabled = true;
      if (h.runtime_orchestrator.unified_critic.model_key == null) h.runtime_orchestrator.unified_critic.model_key = "";
      h.runtime_orchestrator.search_sufficiency = h.runtime_orchestrator.search_sufficiency || {};
      if (typeof h.runtime_orchestrator.search_sufficiency.enabled !== "boolean") h.runtime_orchestrator.search_sufficiency.enabled = true;
      if (h.runtime_orchestrator.search_sufficiency.model_key == null) h.runtime_orchestrator.search_sufficiency.model_key = "";
      h.runtime_orchestrator.observability = h.runtime_orchestrator.observability || {};
      if (h.runtime_orchestrator.observability.metrics_jsonl_path == null) h.runtime_orchestrator.observability.metrics_jsonl_path = "";
      if (h.runtime_orchestrator.observability.metrics_sqlite_path == null) h.runtime_orchestrator.observability.metrics_sqlite_path = "data/runtime_metrics.sqlite";

      h.stream_tuning = h.stream_tuning || {};
      if (typeof h.stream_tuning.smart_chunk_boundary !== "boolean") h.stream_tuning.smart_chunk_boundary = true;
      if (typeof h.stream_tuning.emit_content_reset !== "boolean") h.stream_tuning.emit_content_reset = true;

      h.search = h.search || {};
      if (!h.search.provider) h.search.provider = "tavily";
      if (!h.search.fallback) h.search.fallback = "duckduckgo";
      if (typeof h.search.query_enrich !== "boolean") h.search.query_enrich = false;
      if (!h.search.search_depth) h.search.search_depth = "basic";
      if (h.search.max_results == null) h.search.max_results = 8;
      h.search.by_intent = h.search.by_intent || {};
      h.search.by_intent.search = h.search.by_intent.search || {};
      if (h.search.by_intent.search.max_results == null) h.search.by_intent.search.max_results = 8;
      if (!h.search.by_intent.search.search_depth) h.search.by_intent.search.search_depth = "basic";
      h.search.by_intent.dag = h.search.by_intent.dag || {};
      if (h.search.by_intent.dag.max_results == null) h.search.by_intent.dag.max_results = 12;
      if (!h.search.by_intent.dag.search_depth) h.search.by_intent.dag.search_depth = "advanced";
      if (!h.search.topic) h.search.topic = "general";
      if (h.search.timeout_s == null) h.search.timeout_s = 15;
      if (h.search.timeout_s_max == null) h.search.timeout_s_max = 28;
      if (typeof h.search.include_answer !== "boolean") h.search.include_answer = false;
      h.search.relevance_filter = h.search.relevance_filter || {};
      if (typeof h.search.relevance_filter.enabled !== "boolean") h.search.relevance_filter.enabled = true;
      if (h.search.relevance_filter.model == null) h.search.relevance_filter.model = "claude-sonnet-4-6";
      if (!h.search.relevance_filter.sync_default_mode) h.search.relevance_filter.sync_default_mode = "runtime_phases";
      if (h.search.session_cache_ttl_s == null) h.search.session_cache_ttl_s = 1800;
      if (h.search.session_cache_ttl_freshness_s == null) h.search.session_cache_ttl_freshness_s = 180;
      if (h.search.session_cache_ttl_required_s == null) h.search.session_cache_ttl_required_s = 900;
      if (h.search.session_cache_ttl_explicit_s == null) h.search.session_cache_ttl_explicit_s = 1800;
      if (!Array.isArray(h.search.speculative_markers)) h.search.speculative_markers = [];

      h.documents = h.documents || {};
      if (h.documents.bm25_weight == null) h.documents.bm25_weight = 0.55;
      if (h.documents.embedding_weight == null) h.documents.embedding_weight = 0.45;
      h.documents.embedding = h.documents.embedding || {};
      if (typeof h.documents.embedding.enabled !== "boolean") h.documents.embedding.enabled = true;
      if (h.documents.embedding.model_key == null) h.documents.embedding.model_key = "n1n-embedding-3-large";
      if (h.documents.embedding.max_items == null) h.documents.embedding.max_items = 40;
      if (h.documents.embedding.text_chars == null) h.documents.embedding.text_chars = 2000;
      if (h.documents.embedding.timeout_s == null) h.documents.embedding.timeout_s = 30;
      if (h.documents.embedding.rrf_k == null) h.documents.embedding.rrf_k = 60;
      h.documents.compact = h.documents.compact || {};
      if (h.documents.compact.max_items == null) h.documents.compact.max_items = 8;
      if (h.documents.max_text_chars == null) h.documents.max_text_chars = 220000;
      if (h.documents.chunk_size == null) h.documents.chunk_size = 7000;
      if (h.documents.chunk_overlap == null) h.documents.chunk_overlap = 900;
      if (h.documents.max_pdf_pages == null) h.documents.max_pdf_pages = 100;
      if (h.documents.max_sheet_rows == null) h.documents.max_sheet_rows = 2500;
      h.documents.rerank = h.documents.rerank || {};
      if (typeof h.documents.rerank.enabled !== "boolean") h.documents.rerank.enabled = true;
      if (h.documents.rerank.model == null) h.documents.rerank.model = "bge-reranker-v2-m3";
      if (h.documents.rerank.max_items == null) h.documents.rerank.max_items = 12;
      if (h.documents.rerank.top_k == null) h.documents.rerank.top_k = 8;

      h.task_model_templates = h.task_model_templates || {};
      ["reasoning", "generation", "code", "conversation"].forEach((key) => {
        h.task_model_templates[key] = h.task_model_templates[key] || {};
        const block = h.task_model_templates[key];
        if (!Array.isArray(block.fallback_models)) block.fallback_models = [];
        if (key !== "conversation") {
          block.quality_models = block.quality_models || {};
          if (!Array.isArray(block.quality_models.draft)) block.quality_models.draft = [];
          if (!Array.isArray(block.quality_models.review)) block.quality_models.review = [];
          if (!Array.isArray(block.quality_models.polish)) block.quality_models.polish = [];
        }
      });

      h.complexity = h.complexity || {};
      if (typeof h.complexity.use_llm_analyzer !== "boolean") h.complexity.use_llm_analyzer = true;
      if (h.complexity.analyzer_model == null) h.complexity.analyzer_model = "claude-sonnet-4-6";
      if (h.complexity.analyzer_request_timeout_s == null) h.complexity.analyzer_request_timeout_s = 18;
      if (h.complexity.analyzer_max_retries == null) h.complexity.analyzer_max_retries = 2;
      if (h.complexity.analyzer_total_timeout_s == null) h.complexity.analyzer_total_timeout_s = 60;
      if (h.complexity.analysis_cache_ttl_s == null) h.complexity.analysis_cache_ttl_s = 300;
      if (h.complexity.analyzer_prompt == null) h.complexity.analyzer_prompt = "";

      h.quality_pipeline = h.quality_pipeline || {};
      if (typeof h.quality_pipeline.enabled !== "boolean") h.quality_pipeline.enabled = true;
      h.quality_pipeline.repair = h.quality_pipeline.repair || {};
      if (h.quality_pipeline.repair.temperature == null) h.quality_pipeline.repair.temperature = 0.15;
      h.quality_pipeline.layer1 = h.quality_pipeline.layer1 || {};
      if (!h.quality_pipeline.layer1.name) h.quality_pipeline.layer1.name = "draft";
      if (h.quality_pipeline.layer1.temperature == null) h.quality_pipeline.layer1.temperature = 0.2;
      if (h.quality_pipeline.layer1.instruction == null) h.quality_pipeline.layer1.instruction = "";
      h.quality_pipeline.layer2 = h.quality_pipeline.layer2 || {};
      if (!h.quality_pipeline.layer2.name) h.quality_pipeline.layer2.name = "review";
      if (h.quality_pipeline.layer2.temperature == null) h.quality_pipeline.layer2.temperature = 0.1;
      if (h.quality_pipeline.layer2.instruction == null) h.quality_pipeline.layer2.instruction = "";
      h.quality_pipeline.layer3 = h.quality_pipeline.layer3 || {};
      if (!h.quality_pipeline.layer3.name) h.quality_pipeline.layer3.name = "polish";
      if (h.quality_pipeline.layer3.temperature == null) h.quality_pipeline.layer3.temperature = 0.3;
      if (h.quality_pipeline.layer3.instruction == null) h.quality_pipeline.layer3.instruction = "";
    },
    async saveRuntime() {
      if (!this.editedHarness) return;
      this.ensureShapes();
      this.saving = true;
      this.saveMsg = "";
      try {
        const response = await fetch(`${API_BASE}/api/config/runtime`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ harness: this.editedHarness }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          let detail = data.detail;
          if (Array.isArray(detail)) {
            detail = detail.map((item) => (item?.msg ? item.msg : JSON.stringify(item))).join("; ");
          } else if (detail && typeof detail === "object") {
            detail = JSON.stringify(detail);
          }
          throw new Error(detail || data.message || `HTTP ${response.status}`);
        }
        this.saveOk = true;
        this.saveMsg = data.message || "已保存";
        this.$emit("reload");
      } catch (error) {
        this.saveOk = false;
        this.saveMsg = String(error?.message || error);
      } finally {
        this.saving = false;
      }
    },
  },
};
</script>

<style scoped>
.wrap {
  height: 100%;
  overflow: auto;
  padding-bottom: 24px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.title {
  font-weight: 800;
  color: #f1f5f9;
}
.spacer {
  flex: 1;
}
.btn {
  padding: 8px 14px;
  border-radius: 10px;
  border: 1px solid #3d4d64;
  background: #232a38;
  color: #cbd5e1;
  cursor: pointer;
  font-weight: 500;
}
.btn.primary {
  background: linear-gradient(135deg, #4f46e5, #6366f1);
  border-color: rgba(129, 140, 248, 0.45);
  color: #fff;
}
.btn:hover:not(:disabled) {
  filter: brightness(1.05);
}
.btn:disabled {
  opacity: 0.5;
}
.note {
  font-size: 13px;
  line-height: 1.6;
  color: #cbd5e1;
  margin: 0 0 10px;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(59, 130, 246, 0.08);
  border: 1px solid rgba(59, 130, 246, 0.22);
}
.meta {
  font-size: 12px;
  color: #64748b;
  margin: 0 0 14px;
}
.meta code {
  color: #a5b4fc;
}
.tag {
  margin-left: 8px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: #2f3a4d;
  color: #94a3b8;
}
.tag.on {
  background: rgba(34, 197, 94, 0.15);
  color: #86efac;
}
.save-banner {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13px;
}
.save-banner.ok {
  background: rgba(34, 197, 94, 0.12);
  border: 1px solid rgba(34, 197, 94, 0.35);
  color: #86efac;
}
.save-banner.err {
  background: rgba(248, 113, 113, 0.12);
  border: 1px solid rgba(248, 113, 113, 0.35);
  color: #fca5a5;
}
.hint {
  color: #94a3b8;
  font-size: 13px;
}
.error {
  padding: 12px;
  border-radius: 12px;
  border: 1px solid rgba(248, 113, 113, 0.35);
  background: rgba(248, 113, 113, 0.1);
  color: #fca5a5;
  line-height: 1.5;
}
.config-root {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 14px;
}
.card {
  border: 1px solid rgba(71, 85, 105, 0.5);
  border-radius: 14px;
  background: rgba(15, 23, 42, 0.72);
  padding: 14px;
}
.card-wide {
  grid-column: 1 / -1;
}
.card h3 {
  margin: 0 0 12px;
  color: #f1f5f9;
  font-size: 14px;
}
.subcard {
  margin-top: 12px;
  padding: 12px;
  border-radius: 12px;
  background: rgba(30, 41, 59, 0.55);
  border: 1px solid rgba(51, 65, 85, 0.55);
}
.subcard-title {
  margin-bottom: 10px;
  font-size: 12px;
  font-weight: 700;
  color: #cbd5e1;
}
.template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.template-card {
  margin-top: 0;
}
.grid {
  display: grid;
  gap: 10px;
}
.grid.two {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.grid.three {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 12px;
  color: #64748b;
}
.field.inline {
  flex-direction: row;
  align-items: center;
  gap: 12px;
}
.field.inline .field-label {
  min-width: 148px;
}
.field-label {
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  letter-spacing: 0.02em;
}
.row-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-size: 13px;
  color: #cbd5e1;
  cursor: pointer;
}
.inp {
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #1a1f2b;
  color: #e2e8f0;
  font-size: 13px;
}
.ta {
  width: 100%;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #1a1f2b;
  color: #e2e8f0;
  font-family: ui-monospace, monospace;
  font-size: 12px;
  resize: vertical;
}
.json-panel {
  border: 1px solid #334155;
  border-radius: 12px;
  background: #151a24;
  overflow: hidden;
}
.json-panel-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px 16px;
  padding: 10px 14px;
  border-bottom: 1px solid #2f3a4d;
  background: rgba(30, 41, 59, 0.45);
}
.json-panel-title {
  font-size: 13px;
  font-weight: 700;
  color: #94a3b8;
}
.json-panel-hint {
  font-size: 11px;
  color: #64748b;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  margin: 0;
  padding: 12px 14px;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  line-height: 1.45;
  color: #94a3b8;
  max-height: 360px;
  overflow: auto;
}
</style>
