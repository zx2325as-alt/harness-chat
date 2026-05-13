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
      基础文件：<code>config.yaml</code>；可写叠加层：
      <code>{{ config.runtime_overlay_path || "config.runtime.yaml" }}</code>
      <span v-if="config.runtime_overlay_active" class="tag on">已存在叠加</span>
      <span v-else class="tag">尚未创建叠加文件</span>
    </p>

    <div v-if="saveMsg" class="save-banner" :class="saveOk ? 'ok' : 'err'">{{ saveMsg }}</div>

    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="editedHarness" class="config-root">
      <div class="pipeline-legend">
        <p class="legend-lead">
          下列卡片按<strong>单次请求在后端的大致执行顺序</strong>从左到右排列；请<strong>横向滑动</strong>查看整条链路。每列内部可独立上下滚动。
        </p>
        <ul class="legend-list">
          <li><strong>保存并生效</strong>会把当前表单合并写入 <code>config.runtime.yaml</code>，并触发聊天页重新拉取配置（默认模式、全局联网锁等与服务器对齐）。</li>
          <li><strong>密钥类字段</strong>（如检索 API Key）不会出现在此页，保存时也不会被前端空值覆盖。</li>
          <li>下列顺序为便于理解的<strong>逻辑流水线</strong>：文档与预判在后端可能并行，以实际代码为准。</li>
        </ul>
      </div>

      <div class="pipeline-scroller">
        <div class="pipeline-track" role="list">
          <!-- 1 入口策略 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">1</span>
              <div class="step-titles">
                <div class="step-title">入口策略</div>
                <div class="step-sub">default_mode · 全局联网锁</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                请求进入后最先生效的全局策略：<strong>默认走哪条轨</strong>，以及是否在<strong>服务器级禁止一切联网检索</strong>。此处改动会影响所有会话；单条消息若在输入区单独关了搜索，仍对本条优先，但挡不住全局部署锁。
              </p>
              <label class="field">
                <span class="field-label">harness.default_mode</span>
                <select v-model="editedHarness.default_mode" class="inp inp-fill">
                  <option value="auto">auto（预判选轨）</option>
                  <option value="fast">fast</option>
                  <option value="refine">refine</option>
                  <option value="agent">agent</option>
                </select>
                <span class="field-help">用户在聊天里未手动切换轨道时采用。<strong>auto</strong> 会在后续「预判」步骤由模型在 fast / refine / agent 间抉择。</span>
              </label>
              <div class="divider" />
              <label class="row-check">
                <input v-model="editedHarness.web_search.globally_disabled" type="checkbox" />
                <span>全局禁止联网 web_search.globally_disabled</span>
              </label>
              <p class="mini">
                <strong>部署锁：</strong>勾选后全轨道、Agent 工具与审查层检索均被短路，不调外部检索 API。聊天页「搜索」会与服务器同步为关闭并禁用切换。取消勾选并保存后，才允许按轨道与用户选择恢复检索。
              </p>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 2 预判 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">2</span>
              <div class="step-titles">
                <div class="step-title">预判</div>
                <div class="step-sub">complexity · features</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                在 <strong>auto</strong> 或需要意图分类时，由<strong>预判模型</strong>解析复杂度、是否检索、建议轨道等。超时过短容易退回规则预判，过长则首包变慢。<strong>analyzer_json_repair</strong> 在解析失败时多一次「只修格式」的补救。
              </p>
              <label class="row-check">
                <input v-model="complexity.use_llm_analyzer" type="checkbox" />
                <span>use_llm_analyzer</span>
              </label>
              <span class="field-help block">关闭后不再调用预判 LLM，轨道与检索意图改由规则/默认值推断，速度可能更快但灵活性下降。</span>
              <label class="field">
                <span class="field-label">analyzer_model</span>
                <select v-model="complexity.analyzer_model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'an-' + k" :value="k">{{ k }}</option>
                </select>
                <span class="field-help">必须已在服务端 <code>models</code> 注册；宜选延迟较低、能稳定输出 JSON 的模型。</span>
              </label>
              <label class="field">
                <span class="field-label">超时 / 重试 / 总预算（秒）</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="complexity.analyzer_request_timeout_s" type="number" min="5" max="120" class="inp inp-tiny" title="单次请求超时" />
                  <input v-model.number="complexity.analyzer_max_retries" type="number" min="0" max="5" class="inp inp-tiny" title="失败后的重试次数" />
                  <input v-model.number="complexity.analyzer_total_timeout_s" type="number" min="10" max="300" class="inp inp-tiny" title="整段预判硬超时" />
                </span>
                <span class="field-help">从左到右：单次 RPC 上限、额外重试次数、整段预判不得超过的秒数。<strong>总预算建议 ≥ 单次超时 × (1 + 重试次数)</strong>，否则可能被服务端警告或提前取消。</span>
              </label>
              <label class="field">
                <span class="field-label">analysis_cache_ttl_s</span>
                <input v-model.number="complexity.analysis_cache_ttl_s" type="number" min="0" max="86400" class="inp inp-fill" />
                <span class="field-help">预判结果在会话内可复用的缓存时间（秒）；适当增大可减少重复预判成本。</span>
              </label>
              <div class="divider" />
              <div class="cardTitle tight">features（预判配套）</div>
              <label class="row-check">
                <input v-model="features.analyzer_json_repair" type="checkbox" />
                <span>analyzer_json_repair</span>
              </label>
              <span class="field-help block">当预判输出不是合法 JSON 时，追加一次极短的「仅修复 JSON 结构」调用，有利于稳定性，会增加少量延迟与费用。</span>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 3 路由与置信度 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">3</span>
              <div class="step-titles">
                <div class="step-title">路由与回退</div>
                <div class="step-sub">routing · routing_tuning</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                预判之后，系统按<strong>模板与默认模型池</strong>选出实际调用的模型；失败则沿列表降级。当前<strong>执行主路径为 DAG Runtime</strong>，不再按置信度在 fast/refine/agent 之间<strong>互斥换轨</strong>；下方 <strong>routing_tuning</strong> 字段仅为<strong>兼容保留</strong>（后端暂不读取）。
              </p>
              <label class="field">
                <span class="field-label">routing.default_model</span>
                <select v-model="routing.default_model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="k" :value="k">{{ k }}</option>
                </select>
                <span class="field-help">无更具体模板命中时的首选模型；须已在 <code>models</code> 注册。</span>
              </label>
              <label class="field">
                <span class="field-label">default_models（每行一个）</span>
                <textarea v-model="defaultModelsText" class="ta ta-short" rows="4" placeholder="gpt-5.5&#10;claude-sonnet-4-6" />
                <span class="field-help">自上而下依次尝试，某一模型报错或不可用则换下一个，用于提高可用性与兜底。</span>
              </label>
              <div class="divider" />
              <div class="cardTitle tight">routing_tuning（兼容保留 · 当前 DAG 不生效）</div>
              <label class="field">
                <span class="field-label">guard_threshold（0～1）</span>
                <input v-model.number="routingTuning.confidence_track_guard_threshold" type="number" min="0" max="1" step="0.01" class="inp inp-fill" />
                <span class="field-help">历史字段：曾为低置信度时从快轨升到精化轨。DAG 主路径<strong>不使用</strong>该阈值。</span>
              </label>
              <label class="field">
                <span class="field-label">guard_min_prompt_chars</span>
                <input v-model.number="routingTuning.confidence_track_guard_min_prompt_chars" type="number" min="0" max="2000" class="inp inp-fill" />
                <span class="field-help">历史字段：短输入不触发升轨。DAG 主路径<strong>不使用</strong>。</span>
              </label>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 4 文档上下文 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">4</span>
              <div class="step-titles">
                <div class="step-title">文档上下文</div>
                <div class="step-sub">documents</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                用户<strong>上传文档或文件夹</strong>后，服务端用 BM25 与向量召回混合检索片段，并可<strong>重排序</strong>选出最相关段落写入上下文。<strong>compact.max_items</strong> 影响精化链里「指针式」摘要条数；权重和接近 1 时语义更直观。
              </p>
              <label class="field">
                <span class="field-label">bm25 / embedding 权重</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="documents.bm25_weight" type="number" min="0" max="1" step="0.05" class="inp inp-half" title="BM25" />
                  <input v-model.number="documents.embedding_weight" type="number" min="0" max="1" step="0.05" class="inp inp-half" title="向量" />
                </span>
                <span class="field-help">二者大致表示两类召回在融合时的相对重要性；二者之和不必严格为 1，但<strong>同时调高一侧、调低另一侧</strong>可分别偏向关键词字面匹配或语义相似。</span>
              </label>
              <label class="row-check">
                <input v-model="docEmbedding.enabled" type="checkbox" />
                <span>embedding.enabled</span>
              </label>
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
                <span class="field-help">仅对 BM25 前几名候选做向量化时的上限；增大通常提升召回质量但增加 embedding 调用成本。</span>
              </label>
              <label class="row-check">
                <input v-model="docRerank.enabled" type="checkbox" />
                <span>rerank.enabled</span>
              </label>
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
                  <input v-model.number="docRerank.max_items" type="number" min="1" max="48" class="inp inp-half" title="参与重排的候选数" />
                  <input v-model.number="docRerank.top_k" type="number" min="1" max="24" class="inp inp-half" title="重排后保留条数" />
                </span>
                <span class="field-help"><strong>max_items</strong>：送入重排模型的片段数量上限；<strong>top_k</strong>：重排后实际拼进上下文的条数。</span>
              </label>
              <label class="field">
                <span class="field-label">compact.max_items</span>
                <input v-model.number="docCompact.max_items" type="number" min="1" max="24" class="inp inp-fill" />
                <span class="field-help">精化链使用的「紧凑文档指针」列表长度；过大增加上下文长度与费用。</span>
              </label>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 5 联网检索 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">5</span>
              <div class="step-titles">
                <div class="step-title">联网检索</div>
                <div class="step-sub">search · relevance_filter</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                当预判或用户打开「搜索」且未被全局禁网时，后端调用检索提供商拉取网页摘要。<strong>relevance_filter</strong> 可用另一路模型丢掉明显不相关的条目；<strong>sync_*</strong> 决定是否在主链路里同步等待过滤（慢但顺序确定）。
              </p>
              <label class="row-check">
                <input v-model="searchCfg.query_enrich" type="checkbox" />
                <span>query_enrich</span>
              </label>
              <span class="field-help block">开启时可能对查询词做自动扩展；关闭则更依赖预判给出的 <code>search_query</code>，便于你要「搜什么就 strictly 搜什么」。</span>
              <label class="field">
                <span class="field-label">search_depth</span>
                <select v-model="searchCfg.search_depth" class="inp inp-fill">
                  <option value="basic">basic</option>
                  <option value="advanced">advanced</option>
                </select>
                <span class="field-help">一般由提供商解释「浅 / 深」抓取；advanced 往往更全但更慢、更贵。</span>
              </label>
              <label class="field">
                <span class="field-label">max_results</span>
                <input v-model.number="searchCfg.max_results" type="number" min="1" max="24" class="inp inp-fill" />
                <span class="field-help">单次检索最多保留的摘要条数；过大上下文膨胀，过小可能漏关键来源。</span>
              </label>
              <label class="field">
                <span class="field-label">timeout_s / max</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="searchCfg.timeout_s" type="number" min="5" max="120" class="inp inp-half" title="起始超时" />
                  <input v-model.number="searchCfg.timeout_s_max" type="number" min="5" max="180" class="inp inp-half" title="放宽上限" />
                </span>
                <span class="field-help">控制检索 RPC 的等待时间尺度；过短易超时失败，过长拖慢整条响应。</span>
              </label>
              <label class="row-check">
                <input v-model="searchCfg.include_answer" type="checkbox" />
                <span>include_answer</span>
              </label>
              <span class="field-help block">若提供商支持「摘要答案」字段，开启后可能多一段聚合文案（依提供商语义而定）。</span>
              <div class="divider" />
              <label class="row-check">
                <input v-model="searchRelFilter.enabled" type="checkbox" />
                <span>relevance_filter.enabled</span>
              </label>
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
                <span class="field-help"><strong>quality_tracks</strong>：仅下方列表中的轨道同步过滤；<strong>always</strong>：总是同步（延迟↑）；<strong>never</strong>：默认不同步，快轨更轻。</span>
              </label>
              <label class="field">
                <span class="field-label">sync_tracks（每行）</span>
                <textarea v-model="syncTracksText" class="ta ta-short" rows="2" placeholder="refine&#10;agent" />
                <span class="field-help">轨道名小写，如 <code>refine</code>、<code>agent</code>；在 quality_tracks 模式下只有这些轨会「边检索边等相关性过滤」。</span>
              </label>
              <p class="mini"><strong>说明：</strong>API Key、provider 等密钥不在此页展示；保存时服务端会剥离密钥字段，不会用浏览器里的空值覆盖磁盘上的密钥。</p>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 6 精化链 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">6</span>
              <div class="step-titles">
                <div class="step-title">精化链</div>
                <div class="step-sub">refine_chain · 审查联网轮数</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                <strong>精化轨</strong>走多层草稿 → 审查 → 润色。审查层可通过指令发起<strong>联网核查</strong>再迭代。<code>max_review_web_rounds</code> 限制这种「查资料 → 再审」最多循环几轮，防止费用与延迟失控。
              </p>
              <label class="row-check">
                <input v-model="refineChain.enabled" type="checkbox" />
                <span>refine_chain.enabled</span>
              </label>
              <span class="field-help block">关闭后精化多段流水线不可用或降级（以后端实现为准），一般仅调试或极简部署时关闭。</span>
              <label class="field">
                <span class="field-label">refine_chain_tuning.max_review_web_rounds</span>
                <input v-model.number="refineChainTuning.max_review_web_rounds" type="number" min="1" max="8" class="inp inp-fill" />
                <span class="field-help">审查层通过 JSON 工具协议（例如 <code v-pre>{"action":"web_search","query":"..."}</code>）触发的外查与再审最大轮数（1～8）。数字越大越「较真」，但响应越慢。</span>
              </label>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 7 Agent -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">7</span>
              <div class="step-titles">
                <div class="step-title">Agent 循环</div>
                <div class="step-sub">agent · agent_tuning</div>
              </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                <strong>Agent 轨</strong>以「思考—工具—观察」循环执行，可多次联网或调用封装能力。<strong>max_iterations</strong> 与按复杂度分解的上限控制最长循环；<strong>progress_eval</strong> 在进度停滞时中止并走 Refine 兜底，避免空转。
              </p>
              <label class="row-check">
                <input v-model="agent.enabled" type="checkbox" />
                <span>agent.enabled</span>
              </label>
              <span class="field-help block">关闭后需要 Agent 的场景往往退回精化链或其它兜底，复杂工具链能力会受限。</span>
              <label class="field">
                <span class="field-label">model</span>
                <select v-model="agent.model" class="inp inp-fill">
                  <option value="">（未设置）</option>
                  <option v-for="k in modelKeys" :key="'ag-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">model_by_task_type.code / reasoning</span>
                <select v-model="agent.model_by_task_type.code" class="inp inp-fill">
                  <option value="">code → 默认</option>
                  <option v-for="k in modelKeys" :key="'agc-' + k" :value="k">{{ k }}</option>
                </select>
                <select v-model="agent.model_by_task_type.reasoning" class="inp inp-fill mt-6">
                  <option value="">reasoning → 默认</option>
                  <option v-for="k in modelKeys" :key="'agr-' + k" :value="k">{{ k }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">max_iterations</span>
                <input v-model.number="agent.max_iterations" type="number" min="1" max="20" class="inp inp-fill" />
                <span class="field-help">未命中下方按复杂度覆盖时的默认最大步数。</span>
              </label>
              <label class="field">
                <span class="field-label">iterations：low / med / high</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="agent.max_iterations_by_complexity.low" type="number" min="1" max="20" class="inp inp-tiny" title="低复杂度" />
                  <input v-model.number="agent.max_iterations_by_complexity.medium" type="number" min="1" max="20" class="inp inp-tiny" title="中复杂度" />
                  <input v-model.number="agent.max_iterations_by_complexity.high" type="number" min="1" max="20" class="inp inp-tiny" title="高复杂度" />
                </span>
                <span class="field-help">与预判给出的 complexity 联动；高档任务允许更多轮工具调用，但也更容易耗时。</span>
              </label>
              <div class="divider" />
              <span class="field-help block">独立 Agent 循环已移除；下列 progress_eval 为<strong>兼容保留</strong>（DAG 主路径不调用进度评估模块）。</span>
              <label class="row-check">
                <input v-model="agentTuning.progress_eval.enabled" type="checkbox" />
                <span>progress_eval.enabled</span>
              </label>
              <label class="field">
                <span class="field-label">every_n_iterations</span>
                <input v-model.number="agentTuning.progress_eval.every_n_iterations" type="number" min="1" max="8" class="inp inp-fill" />
              </label>
              <label class="field">
                <span class="field-label">progress_score_delta_threshold / progress_delta_low_abort_after</span>
                <span class="inline-pair inp-fill-row">
                  <input v-model.number="agentTuning.progress_eval.progress_score_delta_threshold" type="number" min="0" max="1" step="0.01" class="inp inp-tiny" title="Δ 阈值" />
                  <input v-model.number="agentTuning.progress_eval.progress_delta_low_abort_after" type="number" min="1" max="12" class="inp inp-tiny" title="连续轮数" />
                </span>
                <span class="field-help">连续多轮进度评分变化低于阈值达到次数后终止 Agent。</span>
              </label>
            </div>
          </article>

          <span class="pipeline-arrow" aria-hidden="true">›</span>

          <!-- 8 输出流式 -->
          <article class="pipeline-step" role="listitem">
            <header class="pipeline-step-head">
              <span class="step-badge">8</span>
              <div class="step-titles">
                <div class="step-title">输出与流式</div>
                <div class="step-sub">stream_slice · stream_tuning</div>
      </div>
            </header>
            <div class="pipeline-step-body">
              <p class="step-blurb">
                模型生成文本后，服务端按块推送到前端。<strong>stream_slice_chars</strong> 主要作用于<strong>非流式接口模拟流式</strong>时的切块粒度；<strong>smart_chunk_boundary</strong> 尽量在标点处断开，中文阅读更顺；<strong>emit_content_reset</strong> 会在精化层切换时通知前端「清空展示缓冲区」，有利步骤区分也可能带来闪烁感。
              </p>
              <label class="field">
                <span class="field-label">stream_slice_chars（非流式切片）</span>
                <input v-model.number="editedHarness.stream_slice_chars" type="number" min="24" max="256" class="inp inp-fill" />
                <span class="field-help">每块大约多少字符触发一次向前端的增量输出；偏小事件更密、更「丝滑」，偏大则包数少、开销低。</span>
              </label>
              <label class="row-check">
                <input v-model="streamTuning.smart_chunk_boundary" type="checkbox" />
                <span>smart_chunk_boundary</span>
              </label>
              <span class="field-help block">优先在句号、逗号等断点切块，减少单词或汉字被拦腰截断的感觉。</span>
              <label class="row-check">
                <input v-model="streamTuning.emit_content_reset" type="checkbox" />
                <span>emit_content_reset</span>
              </label>
              <span class="field-help block">精化流水线阶段切换时是否发送 content_reset 事件。关闭后 UI 可能连续追加全文，但层次边界需靠步骤组件区分。</span>
      </div>
          </article>
      </div>
      </div>

      <section class="json-panel">
        <header class="json-panel-head">
          <span class="json-panel-title">完整 harness JSON（只读）</span>
          <span class="json-panel-hint">与上方表单为同一对象；便于核对未做表单化的字段。保存时整体提交；密钥类键仍不会由浏览器覆盖。</span>
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
      h.agent.model_by_task_type = h.agent.model_by_task_type || {};
      h.agent.max_iterations_by_complexity = h.agent.max_iterations_by_complexity || {};
      if (h.agent.max_iterations_by_complexity.low == null) h.agent.max_iterations_by_complexity.low = 3;
      if (h.agent.max_iterations_by_complexity.medium == null) h.agent.max_iterations_by_complexity.medium = 5;
      if (h.agent.max_iterations_by_complexity.high == null) h.agent.max_iterations_by_complexity.high = 8;

      h.agent_tuning = h.agent_tuning || {};
      h.agent_tuning.progress_eval = h.agent_tuning.progress_eval || {};
      const pe = h.agent_tuning.progress_eval;
      if (typeof pe.enabled !== "boolean") pe.enabled = true;
      if (pe.every_n_iterations == null) pe.every_n_iterations = 2;
      if (pe.max_calls_per_request == null) pe.max_calls_per_request = 4;
      if (pe.low_progress_abort_after == null) pe.low_progress_abort_after = 2;
      if (pe.min_progress_score == null) pe.min_progress_score = 0.22;
      if (pe.min_delta_vs_previous == null) pe.min_delta_vs_previous = 0.1;
      if (pe.progress_score_delta_threshold == null) pe.progress_score_delta_threshold = 0.05;
      if (pe.progress_delta_low_abort_after == null) pe.progress_delta_low_abort_after = 2;

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
      if (!Array.isArray(rf.sync_tracks)) rf.sync_tracks = ["dag", "refine", "agent"];

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
  padding: 12px 14px 12px 16px;
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.72);
  border: 1px solid rgba(71, 85, 105, 0.5);
}
.legend-lead {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: #cbd5e1;
}
.legend-list {
  margin: 0;
  padding-left: 1.15rem;
  font-size: 11px;
  line-height: 1.55;
  color: #94a3b8;
}
.legend-list li {
  margin-bottom: 6px;
}
.legend-list li:last-child {
  margin-bottom: 0;
}
.legend-list code {
  font-size: 10px;
  color: #a5b4fc;
  background: rgba(99, 102, 241, 0.12);
  padding: 1px 5px;
  border-radius: 4px;
}
.step-blurb {
  margin: 0 0 12px;
  padding: 8px 10px;
  font-size: 11px;
  line-height: 1.55;
  color: #94a3b8;
  background: rgba(15, 23, 42, 0.55);
  border-radius: 8px;
  border: 1px solid rgba(51, 65, 85, 0.45);
}
.step-blurb strong {
  color: #cbd5e1;
  font-weight: 600;
}
.pipeline-scroller {
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 12px;
  margin: 0 -6px;
  padding-left: 6px;
  padding-right: 6px;
  scrollbar-color: #475569 #1e293b;
}
.pipeline-track {
  display: flex;
  flex-direction: row;
  align-items: stretch;
  gap: 0;
  min-height: min(72vh, 680px);
}
.pipeline-step {
  flex: 0 0 min(300px, 85vw);
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  border: 1px solid rgba(71, 85, 105, 0.55);
  background: linear-gradient(165deg, rgba(79, 70, 229, 0.14) 0%, rgba(30, 41, 59, 0.92) 42%, #1a1f2b 100%);
  box-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}
.pipeline-step-head {
  display: flex;
  align-items: flex-start;
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
  box-shadow: 0 2px 10px rgba(99, 102, 241, 0.35);
}
.step-titles {
  min-width: 0;
}
.step-title {
  font-size: 14px;
  font-weight: 700;
  color: #f1f5f9;
  letter-spacing: 0.02em;
}
.step-sub {
  margin-top: 3px;
  font-size: 11px;
  color: #64748b;
  font-family: ui-monospace, monospace;
  line-height: 1.35;
  word-break: break-all;
}
.pipeline-step-body {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 12px 14px 16px;
  scrollbar-width: thin;
  scrollbar-color: #475569 transparent;
}
.pipeline-arrow {
  flex: 0 0 22px;
  align-self: center;
  text-align: center;
  font-size: 22px;
  font-weight: 300;
  color: #64748b;
  opacity: 0.85;
  user-select: none;
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
.json-panel .mono {
  margin: 0;
  padding: 12px 14px;
  max-height: 320px;
}
.divider {
  height: 1px;
  background: rgba(51, 65, 85, 0.65);
  margin: 12px 0;
}
.cardTitle {
  font-weight: 700;
  margin-bottom: 8px;
  color: #94a3b8;
  font-size: 12px;
}
.cardTitle.tight {
  margin-top: 4px;
}
.field-label {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  letter-spacing: 0.02em;
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
.inp-half {
  flex: 1;
  min-width: 0;
  max-width: none;
}
.inp-tiny {
  flex: 1;
  min-width: 0;
  max-width: none;
}
.mt-6 {
  margin-top: 6px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 12px;
  color: #64748b;
}
.field.inline-num {
  flex-direction: row;
  align-items: center;
  gap: 12px;
}
.field.inline-num span:first-child {
  min-width: 64px;
  color: #94a3b8;
}
.field-help {
  font-size: 11px;
  color: #64748b;
  line-height: 1.45;
}
.field-help.block {
  display: block;
  margin: -6px 0 10px;
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
.mini {
  font-size: 11px;
  color: #64748b;
  margin: -4px 0 10px;
  line-height: 1.45;
}
.inp {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #3d4d64;
  background: #1a1f2b;
  color: #e2e8f0;
  font-size: 13px;
}
.inp.sm {
  max-width: 120px;
}
.inp.wide-num {
  max-width: 100px;
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
  min-height: 68px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  line-height: 1.45;
  color: #94a3b8;
  max-height: 420px;
  overflow: auto;
}
</style>
