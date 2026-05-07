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

    <p v-if="config?.routing_notes" class="note">
      <strong>关于 auto 与「降级」：</strong>{{ config.routing_notes.auto }}
      {{ config.routing_notes.sync_api_agent }}
    </p>
    <p v-if="config" class="meta">
      基础：<code>config.yaml</code>；叠加层：
      <code>{{ config.runtime_overlay_path || "config.runtime.yaml" }}</code>
      <span v-if="config.runtime_overlay_active" class="tag on">已有叠加文件</span>
      <span v-else class="tag">尚无叠加文件</span>
    </p>

    <div v-if="saveMsg" class="save-banner" :class="saveOk ? 'ok' : 'err'">{{ saveMsg }}</div>
    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <div v-else-if="editedHarness" class="config-root">
      <div class="pipeline-legend">
        <p class="legend-lead">
          按<strong>请求处理的大致顺序</strong>从左到右排列；请<strong>横向滑动</strong>。每列可上下滚动。保存后聊天页会重新拉取配置（默认模式、全局禁网等）。
        </p>
        <ul class="legend-list">
          <li>密钥字段不会出现在 API 中；保存时也不会用浏览器覆盖磁盘上的密钥。</li>
          <li>文档与预判在后端可能并行，此顺序仅为便于理解。</li>
        </ul>
      </div>

      <div class="pipeline-scroller">
        <div class="pipeline-track" role="list">
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">1</span>
              <div class="step-titles">
                <div class="step-title">入口策略</div>
                <div class="step-sub">default_mode · 全局联网锁</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">默认轨道与服务器级禁网。全局勾选后所有检索 API 被短路，聊天页「搜索」与服务器同步锁定。</p>
              <label class="field">
                <span class="field-label">harness.default_mode</span>
                <select v-model="editedHarness.default_mode" class="inp inp-fill">
                  <option value="auto">auto（预判选轨）</option>
                  <option value="fast">fast</option>
                  <option value="refine">refine</option>
                  <option value="agent">agent</option>
                </select>
              </label>
              <div class="divider" />
              <label class="row-check">
                <input v-model="editedHarness.web_search.globally_disabled" type="checkbox" />
                <span>全局禁止联网 web_search.globally_disabled</span>
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">2</span>
              <div class="step-titles">
                <div class="step-title">预判</div>
                <div class="step-sub">complexity · features</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">auto 下的复杂度与检索意图分析；超时过短易降级为规则预判。</p>
              <label class="row-check"><input v-model="complexity.use_llm_analyzer" type="checkbox" /><span>use_llm_analyzer</span></label>
              <label class="field">
                <span class="field-label">analyzer_model</span>
                <select v-model="complexity.analyzer_model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'an-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">单次超时 / 重试 / 总超时（秒）</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="complexity.analyzer_request_timeout_s" type="number" min="5" max="120" class="inp inp-tiny" />
                  <input v-model.number="complexity.analyzer_max_retries" type="number" min="0" max="5" class="inp inp-tiny" />
                  <input v-model.number="complexity.analyzer_total_timeout_s" type="number" min="10" max="300" class="inp inp-tiny" />
                </span>
              </label>
              <label class="field">
                <span class="field-label">analysis_cache_ttl_s</span>
                <input v-model.number="complexity.analysis_cache_ttl_s" type="number" min="0" max="86400" class="inp inp-fill" />
              </label>
              <div class="divider" />
              <label class="row-check"><input v-model="features.analyzer_json_repair" type="checkbox" /><span>analyzer_json_repair</span></label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">3</span>
              <div class="step-titles">
                <div class="step-title">路由</div>
                <div class="step-sub">routing · routing_tuning</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">模型回退链与 auto 下低置信度时 fast→refine 守卫。</p>
              <label class="field">
                <span class="field-label">default_model</span>
                <select v-model="routing.default_model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">default_models（每行）</span>
                <textarea v-model="defaultModelsText" class="ta ta-short" rows="4" />
              </label>
              <div class="divider" />
              <label class="field">
                <span class="field-label">guard_threshold</span>
                <input v-model.number="routingTuning.confidence_track_guard_threshold" type="number" min="0" max="1" step="0.01" class="inp inp-fill" />
              </label>
              <label class="field">
                <span class="field-label">guard_min_prompt_chars</span>
                <input v-model.number="routingTuning.confidence_track_guard_min_prompt_chars" type="number" min="0" max="2000" class="inp inp-fill" />
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">4</span>
              <div class="step-titles">
                <div class="step-title">文档</div>
                <div class="step-sub">documents</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">上传文档时的 BM25/向量融合与重排序。</p>
              <label class="field">
                <span class="field-label">bm25 / embedding 权重</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="documents.bm25_weight" type="number" min="0" max="1" step="0.05" class="inp inp-half" />
                  <input v-model.number="documents.embedding_weight" type="number" min="0" max="1" step="0.05" class="inp inp-half" />
                </span>
              </label>
              <label class="row-check"><input v-model="docEmbedding.enabled" type="checkbox" /><span>embedding.enabled</span></label>
              <label class="field">
                <span class="field-label">embedding.model_key</span>
                <select v-model="docEmbedding.model_key" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'emb-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">embedding.max_items</span>
                <input v-model.number="docEmbedding.max_items" type="number" min="5" max="200" class="inp inp-fill" />
              </label>
              <label class="row-check"><input v-model="docRerank.enabled" type="checkbox" /><span>rerank.enabled</span></label>
              <label class="field">
                <span class="field-label">rerank.model</span>
                <select v-model="docRerank.model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'rr-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">rerank max_items / top_k</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="docRerank.max_items" type="number" min="1" max="48" class="inp inp-half" />
                  <input v-model.number="docRerank.top_k" type="number" min="1" max="24" class="inp inp-half" />
                </span>
              </label>
              <label class="field">
                <span class="field-label">compact.max_items</span>
                <input v-model.number="docCompact.max_items" type="number" min="1" max="24" class="inp inp-fill" />
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">5</span>
              <div class="step-titles">
                <div class="step-title">联网检索</div>
                <div class="step-sub">search · relevance_filter</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">检索参数与结果相关性过滤（不含密钥）。</p>
              <label class="row-check"><input v-model="searchCfg.query_enrich" type="checkbox" /><span>query_enrich</span></label>
              <label class="field">
                <span class="field-label">search_depth</span>
                <select v-model="searchCfg.search_depth" class="inp inp-fill">
                  <option value="basic">basic</option>
                  <option value="advanced">advanced</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">max_results</span>
                <input v-model.number="searchCfg.max_results" type="number" min="1" max="24" class="inp inp-fill" />
              </label>
              <label class="field">
                <span class="field-label">timeout_s / max</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="searchCfg.timeout_s" type="number" min="5" max="120" class="inp inp-half" />
                  <input v-model.number="searchCfg.timeout_s_max" type="number" min="5" max="180" class="inp inp-half" />
                </span>
              </label>
              <label class="row-check"><input v-model="searchCfg.include_answer" type="checkbox" /><span>include_answer</span></label>
              <div class="divider" />
              <label class="row-check"><input v-model="searchRelFilter.enabled" type="checkbox" /><span>relevance_filter.enabled</span></label>
              <label class="field">
                <span class="field-label">filter model</span>
                <select v-model="searchRelFilter.model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'rf-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">sync_default_mode</span>
                <select v-model="searchRelFilter.sync_default_mode" class="inp inp-fill">
                  <option value="quality_tracks">quality_tracks</option>
                  <option value="always">always</option>
                  <option value="never">never</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">sync_tracks（每行）</span>
                <textarea v-model="syncTracksText" class="ta ta-short" rows="2" placeholder="refine&#10;agent" />
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">6</span>
              <div class="step-titles">
                <div class="step-title">精化链</div>
                <div class="step-sub">refine_chain</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">多层精化与审查层联网轮数上限。</p>
              <label class="row-check"><input v-model="refineChain.enabled" type="checkbox" /><span>refine_chain.enabled</span></label>
              <label class="field">
                <span class="field-label">max_review_web_rounds</span>
                <input v-model.number="refineChainTuning.max_review_web_rounds" type="number" min="1" max="8" class="inp inp-fill" />
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">7</span>
              <div class="step-titles">
                <div class="step-title">Agent</div>
                <div class="step-sub">agent · agent_tuning</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">推理循环与卡死保护。</p>
              <label class="row-check"><input v-model="agent.enabled" type="checkbox" /><span>agent.enabled</span></label>
              <label class="row-check"><input v-model="agent.sync_non_stream_api" type="checkbox" /><span>sync_non_stream_api</span></label>
              <label class="field">
                <span class="field-label">model</span>
                <select v-model="agent.model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'ag-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">model_by_task_type code</span>
                <select v-model="agent.model_by_task_type.code" class="inp inp-fill">
                  <option value="">默认</option>
                  <option v-for="k in modelKeys" :key="'agc-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">model_by_task_type reasoning</span>
                <select v-model="agent.model_by_task_type.reasoning" class="inp inp-fill">
                  <option value="">默认</option>
                  <option v-for="k in modelKeys" :key="'agr-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">max_iterations</span>
                <input v-model.number="agent.max_iterations" type="number" min="1" max="20" class="inp inp-fill" />
              </label>
              <label class="field">
                <span class="field-label">iter low / med / high</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="agent.max_iterations_by_complexity.low" type="number" min="1" max="20" class="inp inp-tiny" />
                  <input v-model.number="agent.max_iterations_by_complexity.medium" type="number" min="1" max="20" class="inp inp-tiny" />
                  <input v-model.number="agent.max_iterations_by_complexity.high" type="number" min="1" max="20" class="inp inp-tiny" />
                </span>
              </label>
              <div class="divider" />
              <label class="row-check"><input v-model="agentTuning.stuck_loop_guard" type="checkbox" /><span>stuck_loop_guard</span></label>
              <label class="field">
                <span class="field-label">stuck_reply_similarity</span>
                <input v-model.number="agentTuning.stuck_reply_similarity" type="number" min="0" max="1" step="0.01" class="inp inp-fill" />
              </label>
              <label class="field">
                <span class="field-label">stuck_abort_after</span>
                <input v-model.number="agentTuning.stuck_abort_after" type="number" min="1" max="12" class="inp inp-fill" />
              </label>
            </div>
          </article>
          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">8</span>
              <div class="step-titles">
                <div class="step-title">输出流式</div>
                <div class="step-sub">stream_slice · stream_tuning</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">非流式切片与 SSE 断句、content_reset。</p>
              <label class="field">
                <span class="field-label">stream_slice_chars</span>
                <input v-model.number="editedHarness.stream_slice_chars" type="number" min="24" max="256" class="inp inp-fill" />
              </label>
              <label class="row-check"><input v-model="streamTuning.smart_chunk_boundary" type="checkbox" /><span>smart_chunk_boundary</span></label>
              <label class="row-check"><input v-model="streamTuning.emit_content_reset" type="checkbox" /><span>emit_content_reset</span></label>
            </div>
          </article>
        </div>
      </div>

      <section class="json-panel">
        <header class="json-panel-head">
          <span class="json-panel-title">harness JSON（只读）</span>
          <span class="json-panel-hint">与表单同一对象；保存时整体提交。</span>
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
      const m = this.config && this.config.models;
      return Array.isArray(m) ? m.slice() : [];
    },
    routing() {
      return this.editedHarness?.routing || {};
    },
    routingTuning() {
      return this.editedHarness?.routing_tuning || {};
    },
    agent() {
      return this.editedHarness?.agent || {};
    },
    agentTuning() {
      return this.editedHarness?.agent_tuning || {};
    },
    complexity() {
      return this.editedHarness?.complexity || {};
    },
    features() {
      return this.editedHarness?.features || {};
    },
    refineChain() {
      return this.editedHarness?.refine_chain || {};
    },
    refineChainTuning() {
      return this.editedHarness?.refine_chain_tuning || {};
    },
    streamTuning() {
      return this.editedHarness?.stream_tuning || {};
    },
    searchCfg() {
      return this.editedHarness?.search || {};
    },
    searchRelFilter() {
      const s = this.editedHarness?.search || {};
      return s.relevance_filter || {};
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
    defaultModelsText: {
      get() {
        const m = this.routing.default_models;
        return Array.isArray(m) ? m.join("\n") : "";
      },
      set(v) {
        const lines = String(v || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        this.routing.default_models = lines;
      },
    },
    syncTracksText: {
      get() {
        const raw = this.searchRelFilter.sync_tracks;
        return Array.isArray(raw) ? raw.join("\n") : "";
      },
      set(v) {
        const lines = String(v || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => s.toLowerCase());
        this.searchRelFilter.sync_tracks = lines;
      },
    },
  },
  watch: {
    config: {
      deep: true,
      handler(c) {
        if (c && c.harness) {
          this.editedHarness = JSON.parse(JSON.stringify(c.harness));
          this.ensureShapes();
        } else {
          this.editedHarness = null;
        }
      },
      immediate: true,
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
    ensureShapes() {
      const h = this.editedHarness;
      if (!h) return;
      h.web_search = h.web_search || {};
      if (typeof h.web_search.globally_disabled !== "boolean") h.web_search.globally_disabled = false;
      h.routing = h.routing || {};
      if (!Array.isArray(h.routing.default_models)) h.routing.default_models = [];
      h.routing_tuning = h.routing_tuning || {};
      if (h.routing_tuning.confidence_track_guard_threshold == null) h.routing_tuning.confidence_track_guard_threshold = 0.52;
      if (h.routing_tuning.confidence_track_guard_min_prompt_chars == null) h.routing_tuning.confidence_track_guard_min_prompt_chars = 150;
      h.agent = h.agent || {};
      if (typeof h.agent.enabled !== "boolean") h.agent.enabled = true;
      if (h.agent.sync_non_stream_api === undefined) h.agent.sync_non_stream_api = true;
      h.agent.model_by_task_type = h.agent.model_by_task_type || {};
      h.agent.max_iterations_by_complexity = h.agent.max_iterations_by_complexity || {};
      if (h.agent.max_iterations_by_complexity.low == null) h.agent.max_iterations_by_complexity.low = 3;
      if (h.agent.max_iterations_by_complexity.medium == null) h.agent.max_iterations_by_complexity.medium = 5;
      if (h.agent.max_iterations_by_complexity.high == null) h.agent.max_iterations_by_complexity.high = 8;
      h.agent_tuning = h.agent_tuning || {};
      if (typeof h.agent_tuning.stuck_loop_guard !== "boolean") h.agent_tuning.stuck_loop_guard = true;
      if (h.agent_tuning.stuck_reply_similarity == null) h.agent_tuning.stuck_reply_similarity = 0.88;
      if (h.agent_tuning.stuck_abort_after == null) h.agent_tuning.stuck_abort_after = 3;
      h.complexity = h.complexity || {};
      if (typeof h.complexity.use_llm_analyzer !== "boolean") h.complexity.use_llm_analyzer = true;
      h.features = h.features || {};
      if (typeof h.features.analyzer_json_repair !== "boolean") h.features.analyzer_json_repair = true;
      h.refine_chain = h.refine_chain || {};
      if (typeof h.refine_chain.enabled !== "boolean") h.refine_chain.enabled = true;
      h.refine_chain_tuning = h.refine_chain_tuning || {};
      if (h.refine_chain_tuning.max_review_web_rounds == null) h.refine_chain_tuning.max_review_web_rounds = 3;
      h.stream_tuning = h.stream_tuning || {};
      if (typeof h.stream_tuning.smart_chunk_boundary !== "boolean") h.stream_tuning.smart_chunk_boundary = true;
      if (typeof h.stream_tuning.emit_content_reset !== "boolean") h.stream_tuning.emit_content_reset = true;
      h.search = h.search || {};
      if (typeof h.search.query_enrich !== "boolean") h.search.query_enrich = false;
      if (!h.search.search_depth) h.search.search_depth = "basic";
      if (h.search.max_results == null) h.search.max_results = 8;
      if (h.search.timeout_s == null) h.search.timeout_s = 15;
      if (h.search.timeout_s_max == null) h.search.timeout_s_max = 28;
      if (typeof h.search.include_answer !== "boolean") h.search.include_answer = false;
      h.search.relevance_filter = h.search.relevance_filter || {};
      const rf = h.search.relevance_filter;
      if (typeof rf.enabled !== "boolean") rf.enabled = true;
      if (!rf.sync_default_mode) rf.sync_default_mode = "quality_tracks";
      if (!Array.isArray(rf.sync_tracks)) rf.sync_tracks = ["refine", "agent"];
      h.documents = h.documents || {};
      if (h.documents.bm25_weight == null) h.documents.bm25_weight = 0.55;
      if (h.documents.embedding_weight == null) h.documents.embedding_weight = 0.45;
      h.documents.embedding = h.documents.embedding || {};
      if (typeof h.documents.embedding.enabled !== "boolean") h.documents.embedding.enabled = true;
      if (h.documents.embedding.model_key == null) h.documents.embedding.model_key = "n1n-embedding-3-large";
      if (h.documents.embedding.max_items == null) h.documents.embedding.max_items = 40;
      h.documents.rerank = h.documents.rerank || {};
      if (typeof h.documents.rerank.enabled !== "boolean") h.documents.rerank.enabled = true;
      if (h.documents.rerank.max_items == null) h.documents.rerank.max_items = 12;
      if (h.documents.rerank.top_k == null) h.documents.rerank.top_k = 8;
      h.documents.compact = h.documents.compact || {};
      if (h.documents.compact.max_items == null) h.documents.compact.max_items = 8;
    },
    async saveRuntime() {
      if (!this.editedHarness) return;
      this.ensureShapes();
      this.saving = true;
      this.saveMsg = "";
      try {
        const res = await fetch(`${API_BASE}/api/config/runtime`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ harness: this.editedHarness }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          let detail = data.detail;
          if (Array.isArray(detail)) {
            detail = detail.map((x) => (x && x.msg ? x.msg : JSON.stringify(x))).join("; ");
          } else if (detail && typeof detail === "object") {
            detail = JSON.stringify(detail);
          }
          throw new Error(detail || data.message || `HTTP ${res.status}`);
        }
        this.saveOk = true;
        this.saveMsg = data.message || "已保存";
        this.$emit("reload");
      } catch (e) {
        this.saveOk = false;
        this.saveMsg = String(e?.message || e);
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
  line-height: 1.55;
  color: #94a3b8;
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
.pipeline-legend {
  margin: 0;
  padding: 12px 14px;
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.72);
  border: 1px solid rgba(71, 85, 105, 0.5);
}
.legend-lead {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.55;
  color: #cbd5e1;
}
.legend-list {
  margin: 0;
  padding-left: 1.1rem;
  font-size: 11px;
  color: #94a3b8;
  line-height: 1.5;
}
.pipeline-scroller {
  overflow-x: auto;
  padding-bottom: 12px;
  scrollbar-color: #475569 #1e293b;
}
.pipeline-track {
  display: flex;
  flex-direction: row;
  align-items: stretch;
  min-height: min(68vh, 620px);
}
.pipeline-step {
  flex: 0 0 min(292px, 86vw);
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  border: 1px solid rgba(71, 85, 105, 0.55);
  background: linear-gradient(165deg, rgba(79, 70, 229, 0.12) 0%, #1a1f2b 100%);
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.25);
  overflow: hidden;
}
.pipeline-step-head {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.65);
  background: rgba(15, 23, 42, 0.35);
}
.step-badge {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
  color: #e0e7ff;
  background: linear-gradient(135deg, #6366f1, #4f46e5);
}
.step-title {
  font-size: 14px;
  font-weight: 700;
  color: #f1f5f9;
}
.step-sub {
  margin-top: 3px;
  font-size: 11px;
  color: #64748b;
  font-family: ui-monospace, monospace;
}
.pipeline-step-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 14px 16px;
}
.pipeline-arrow {
  flex: 0 0 22px;
  align-self: center;
  text-align: center;
  font-size: 20px;
  color: #64748b;
  user-select: none;
}
.step-blurb {
  margin: 0 0 10px;
  padding: 8px 10px;
  font-size: 11px;
  line-height: 1.5;
  color: #94a3b8;
  background: rgba(15, 23, 42, 0.55);
  border-radius: 8px;
  border: 1px solid rgba(51, 65, 85, 0.45);
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
  gap: 8px 14px;
  padding: 10px 14px;
  border-bottom: 1px solid #2f3a4d;
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
.json-panel .mono {
  margin: 0;
  padding: 12px 14px;
  max-height: 280px;
  overflow: auto;
}
.divider {
  height: 1px;
  background: rgba(51, 65, 85, 0.65);
  margin: 10px 0;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
  font-size: 12px;
  color: #64748b;
}
.field-label {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
}
.row-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  color: #cbd5e1;
  cursor: pointer;
}
.inp {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #1a1f2b;
  color: #e2e8f0;
  font-size: 13px;
}
.inp-fill {
  width: 100%;
  max-width: none;
}
.inp-fill-row {
  width: 100%;
}
.inp-fill-row .inp {
  flex: 1;
  min-width: 0;
  max-width: none;
}
.inp-half,
.inp-tiny {
  flex: 1;
  min-width: 0;
  max-width: none;
}
.inline-pair {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
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
.ta-short {
  min-height: 64px;
}
.mono {
  font-family: ui-monospace, monospace;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  line-height: 1.45;
  color: #94a3b8;
}
</style>
