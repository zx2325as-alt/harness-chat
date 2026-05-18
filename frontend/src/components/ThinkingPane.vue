<template>
  <div v-if="run && (items.length > 0 || run.status === 'running')" class="think-wrap">
    <button type="button" class="think-toggle" :aria-expanded="open" @click="open = !open">
      <span class="think-ico" aria-hidden="true">🧠</span>
      <span class="think-title">
        已思考
        <span v-if="elapsedText" class="think-time">{{ elapsedText }}</span>
      </span>
      <span class="think-badges">
        <span v-if="run.traceId" class="tb" :title="'trace: ' + run.traceId">#{{ shortId(run.traceId) }}</span>
        <span v-if="run.runtime" class="tb runtime">{{ runtimeLabel(run.runtime) }}</span>
        <span v-if="run.status === 'running'" class="tb run">进行中</span>
        <span v-else class="tb ok">{{ items.length }} 条记录</span>
      </span>
      <span class="chev">{{ open ? "▼" : "▶" }}</span>
    </button>

    <div v-show="open" class="think-body">
      <p v-if="run.phaseMessage" class="phase-line"><strong>当前阶段：</strong>{{ run.phaseMessage }}</p>
      <ol class="think-tl" :key="tick">
        <li v-for="(it, idx) in items" :key="it.id || idx" class="tl-item" :class="'k-' + it.kind">
          <span class="tl-rail" aria-hidden="true" />
          <template v-if="it.kind === 'phase'">
            <div class="tl-phase">{{ it.label }}</div>
          </template>

          <template v-else-if="it.kind === 'search'">
            <div class="tl-search">
              <span class="tl-ic" aria-hidden="true">🔍</span>
              <div class="tl-main">
                <div class="tl-h">{{ it.headline || it.label || "联网检索" }}</div>
                <div v-if="it.subline" class="tl-subtle">{{ it.subline }}</div>
                <div v-if="it.query" class="tl-sub">检索查询：{{ it.query }}</div>
                <div v-if="it.provider" class="tl-sub">提供方：{{ it.provider }}</div>
                <div v-for="(line, ei) in (it.extra || [])" :key="'ex'+ei" class="tl-sub">{{ line }}</div>
                <div class="tl-sub row">
                  <span v-if="it.latency_ms != null">{{ it.latency_ms }}ms</span>
                </div>
                <div v-if="domainHosticons(it).length" class="fav-row">
                  <img
                    v-for="h in domainHosticons(it)"
                    :key="h"
                    class="fav"
                    :src="faviconUrl(h)"
                    :alt="''"
                    loading="lazy"
                  />
                </div>
                <p v-if="it.summary && !browseFollows(idx)" class="tl-sum">{{ it.summary }}</p>
              </div>
            </div>
          </template>

          <template v-else-if="it.kind === 'browse'">
            <div class="tl-browse">
              <span class="tl-ic" aria-hidden="true">📄</span>
              <div class="tl-main">
                <div class="tl-h">浏览 {{ it.pageCount }} 个页面</div>
                <p v-if="it.note" class="tl-note">{{ it.note }}</p>
                <ul class="link-ul">
                  <li v-for="(row, li) in browseLinkRows(it)" :key="li">
                    <a :href="row.url" target="_blank" rel="noopener noreferrer" class="link-a">
                      {{ row.title || row.url }}
                      <span class="ext" aria-hidden="true">↗</span>
                    </a>
                    <span v-if="row.authority_score != null" class="auth-tag" :title="'权威度 ' + row.authority_score">
                      权威 {{ fmtAuth(row.authority_score) }}
                    </span>
                  </li>
                </ul>
                <button
                  v-if="(it.sources || []).length > browsePreviewLimit"
                  type="button"
                  class="btn-more"
                  @click.stop="toggleBrowseExpand(it.id)"
                >
                  {{ browseExpanded[it.id] ? "收起" : `查看全部（${(it.sources || []).length}）` }}
                </button>
              </div>
            </div>
          </template>

          <template v-else-if="it.kind === 'bullets'">
            <div class="tl-bullets">
              <span class="tl-dot" aria-hidden="true">{{ it.icon || "•" }}</span>
              <div class="bullets-inner">
                <div class="tl-h" :class="it.colorClass">{{ it.title }}</div>
                <ul class="bullet-ul">
                  <li v-for="(line, bi) in it.lines" :key="bi" class="bullet-li">{{ line }}</li>
                </ul>
                <div v-if="it.latency_ms != null" class="tl-ms">{{ it.latency_ms }}ms</div>
              </div>
            </div>
          </template>

          <template v-else-if="it.kind === 'reason'">
            <div class="tl-reason">
              <span class="tl-dot" aria-hidden="true">•</span>
              <div>
                <div class="tl-h">{{ it.title }}</div>
                <p class="tl-p">{{ it.text }}</p>
                <div v-if="it.latency_ms != null" class="tl-ms">{{ it.latency_ms }}ms</div>
              </div>
            </div>
          </template>

          <template v-else>
            <div class="tl-step">
              <span class="tl-dot" aria-hidden="true">◇</span>
              <div>
                <span class="tl-h">{{ it.title }}</span>
                <span class="tl-st" :class="it.status">{{ statusText(it.status) }}</span>
                <span v-if="it.latency_ms != null" class="tl-ms">{{ it.latency_ms }}ms</span>
                <span v-if="it.provider" class="tl-prov">{{ it.provider }}</span>
                <p v-if="it.detail" class="tl-detail">{{ it.detail }}</p>
              </div>
            </div>
          </template>
        </li>
      </ol>
    </div>
  </div>
</template>

<script>
import { buildThinkingTimeline, uniqueDomainsFromSources } from "../thinkingFromRun.js";

const BROWSE_PREVIEW = 6;

export default {
  name: "ThinkingPane",
  props: {
    run: { type: Object, default: null },
    tick: { type: Number, default: 0 },
    streaming: { type: Boolean, default: false },
    latencyMs: { type: Number, default: null },
  },
  data() {
    return {
      open: false,
      wall: 0,
      _t: null,
      browseExpanded: {},
      browsePreviewLimit: BROWSE_PREVIEW,
    };
  },
  computed: {
    items() {
      void this.tick;
      void this.wall;
      if (!this.run) return [];
      return buildThinkingTimeline(this.run);
    },
    elapsedText() {
      void this.wall;
      const r = this.run;
      if (!r) return "";
      if (this.latencyMs != null && !this.streaming && r.status !== "running") {
        const s = this.latencyMs / 1000;
        const t = s < 10 ? s.toFixed(1) : String(Math.round(s));
        return `（用时 ${t} 秒）`;
      }
      if (r.status === "running") {
        const t0 = new Date(r.createdAt || 0).getTime();
        if (!t0) return "";
        const sec = Math.max(0, Math.floor((Date.now() - t0) / 1000));
        return `（已进行 ${sec} 秒）`;
      }
      return "";
    },
  },
  watch: {
    streaming: {
      immediate: true,
      handler(v) {
        if (v) this.open = true;
      },
    },
    run() {
      this.browseExpanded = {};
      if (this.run && this.run.status === "running") this.open = true;
    },
  },
  mounted() {
    this._t = setInterval(() => {
      if (this.run && this.run.status === "running") this.wall += 1;
    }, 500);
  },
  beforeUnmount() {
    if (this._t) clearInterval(this._t);
  },
  methods: {
    runtimeLabel(runtime) {
      const value = String(runtime || "").trim().toLowerCase();
      if (!value || value === "adaptive_dag_v3") return "Adaptive DAG Runtime";
      return runtime;
    },
    browseFollows(idx) {
      const next = this.items[idx + 1];
      return next && next.kind === "browse";
    },
    toggleBrowseExpand(id) {
      this.browseExpanded = { ...this.browseExpanded, [id]: !this.browseExpanded[id] };
    },
    browseLinkRows(it) {
      const src = it.sources || [];
      const lim = this.browseExpanded[it.id] ? src.length : Math.min(this.browsePreviewLimit, src.length);
      return src.slice(0, lim).map((s) => ({
        title: s.title || s.url,
        url: s.url || "#",
        authority_score: s.authority_score,
      }));
    },
    shortId(t) {
      const s = String(t || "");
      return s.length > 10 ? s.slice(-8) : s;
    },
    statusText(st) {
      if (st === "ok") return "完成";
      if (st === "error") return "失败";
      if (st === "running") return "进行中";
      if (st === "skipped") return "跳过";
      return "";
    },
    faviconUrl(host) {
      return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`;
    },
    domainHosticons(it) {
      return uniqueDomainsFromSources(it.sources || [], 10);
    },
    fmtAuth(v) {
      if (v == null || Number.isNaN(v)) return "";
      return Number(v).toFixed(2);
    },
  },
};
</script>

<style scoped>
.think-wrap {
  margin-bottom: 12px;
  margin-left: 4px;
  border-radius: 12px;
  border: 1px solid rgba(71, 85, 105, 0.55);
  background: rgba(15, 23, 42, 0.62);
  overflow: hidden;
}
.think-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  border: none;
  background: rgba(30, 41, 59, 0.45);
  color: #cbd5e1;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.think-toggle:hover {
  background: rgba(51, 65, 85, 0.38);
}
.think-ico {
  font-size: 16px;
  line-height: 1;
}
.think-title {
  font-weight: 700;
  color: #f1f5f9;
  flex: 1;
  min-width: 0;
}
.think-time {
  font-weight: 500;
  color: #94a3b8;
  font-size: 12px;
}
.think-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.tb {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 6px;
  background: #1e293b;
  color: #94a3b8;
}
.tb.runtime {
  color: #a5b4fc;
}
.tb.run {
  background: rgba(99, 102, 241, 0.2);
  color: #c7d2fe;
}
.tb.ok {
  color: #86efac;
}
.chev {
  color: #64748b;
  font-size: 11px;
}
.think-body {
  padding: 0 12px 14px 14px;
  max-height: min(70vh, 720px);
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.phase-line {
  margin: 8px 0 12px;
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.55;
}
.think-tl {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tl-item {
  position: relative;
  padding-left: 16px;
  margin-bottom: 12px;
}
.tl-rail {
  position: absolute;
  left: 4px;
  top: 0;
  bottom: -12px;
  width: 2px;
  background: rgba(100, 116, 139, 0.35);
  border-radius: 1px;
}
.tl-item:last-child .tl-rail {
  bottom: 0;
}
.tl-phase {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: #64748b;
  margin-bottom: 6px;
}
.tl-search,
.tl-browse {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.tl-ic {
  flex-shrink: 0;
  margin-top: 2px;
  opacity: 0.9;
}
.tl-main {
  min-width: 0;
  flex: 1;
}
.tl-h {
  font-size: 13px;
  font-weight: 600;
  color: #e2e8f0;
}
.tl-h.ok {
  color: #86efac;
}
.tl-h.warn {
  color: #fde68a;
}
.tl-h.err {
  color: #fca5a5;
}
.tl-subtle {
  font-size: 11px;
  color: #64748b;
  margin-top: 2px;
}
.tl-sub {
  font-size: 11px;
  color: #64748b;
  margin-top: 4px;
  line-height: 1.45;
}
.tl-sub.row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.tl-note {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.55;
  color: #94a3b8;
}
.fav-row {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}
.fav {
  width: 17px;
  height: 17px;
  border-radius: 4px;
  background: #fff;
}
.link-ul {
  margin: 8px 0 0;
  padding: 8px 10px;
  list-style: none;
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.65);
  border: 1px solid rgba(51, 65, 85, 0.5);
}
.link-ul li {
  margin-bottom: 6px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.link-ul li:last-child {
  margin-bottom: 0;
}
.link-a {
  font-size: 12px;
  color: #93c5fd;
  text-decoration: none;
  word-break: break-word;
}
.link-a:hover {
  text-decoration: underline;
}
.ext {
  opacity: 0.65;
  font-size: 11px;
}
.auth-tag {
  font-size: 10px;
  color: #86efac;
  background: rgba(34, 197, 94, 0.12);
  padding: 1px 6px;
  border-radius: 4px;
  flex-shrink: 0;
}
.btn-more {
  margin-top: 8px;
  padding: 4px 10px;
  font-size: 11px;
  border-radius: 8px;
  border: 1px solid #475569;
  background: rgba(51, 65, 85, 0.35);
  color: #cbd5e1;
  cursor: pointer;
}
.btn-more:hover {
  background: rgba(71, 85, 105, 0.45);
}
.tl-sum {
  margin: 8px 0 0;
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.55;
}
.tl-bullets {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.bullets-inner {
  flex: 1;
  min-width: 0;
}
.bullet-ul {
  margin: 6px 0 0;
  padding-left: 1.1rem;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.55;
}
.bullet-li {
  margin-bottom: 4px;
}
.bullet-li:last-child {
  margin-bottom: 0;
}
.tl-reason,
.tl-step {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.tl-dot {
  color: #64748b;
  flex-shrink: 0;
  margin-top: 2px;
}
.tl-p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.55;
  color: #94a3b8;
}
.tl-ms {
  font-size: 10px;
  color: #64748b;
  margin-top: 6px;
}
.tl-st {
  margin-left: 6px;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: #334155;
  color: #94a3b8;
}
.tl-st.ok {
  color: #86efac;
}
.tl-st.error {
  color: #fca5a5;
}
.tl-st.running {
  color: #93c5fd;
}
.tl-prov {
  margin-left: 6px;
  font-size: 10px;
  color: #64748b;
}
.tl-detail {
  margin: 6px 0 0;
  font-size: 11px;
  color: #64748b;
  line-height: 1.45;
}
</style>
