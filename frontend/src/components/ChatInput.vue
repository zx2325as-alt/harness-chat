<template>
  <div class="wrap">
    <div class="composer">
      <div v-if="attachments.length > 0" class="att-row">
        <div class="att-item" v-for="(att, idx) in attachments" :key="idx">
          <span class="att-name">{{ att.name }}</span>
          <span class="att-status" :class="att.status">{{ statusLabel(att.status) }}</span>
          <button type="button" class="att-del" @click="removeAttachment(idx)" aria-label="移除">×</button>
        </div>
      </div>

      <textarea
        class="textarea"
        :disabled="busy"
        v-model="draft"
        placeholder="输入问题…（Enter 发送，Shift+Enter 换行）"
        rows="2"
        @keydown.enter.exact.prevent="send"
      />

      <div class="composer-bar" ref="dropRoot">
        <div class="bar-left">
          <div class="dd" :class="{ open: openMenu === 'mode' }">
            <button
              type="button"
              class="dd-trigger"
              :disabled="busy"
              aria-haspopup="listbox"
              :aria-expanded="openMenu === 'mode'"
              @click.stop="toggleMenu('mode')"
            >
              <span class="dd-prefix">轨道</span>
              <span class="dd-value">{{ modeLabel }}</span>
              <svg class="dd-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            <transition name="dd-pop">
              <ul v-show="openMenu === 'mode'" class="dd-list" role="listbox">
                <li
                  v-for="o in modeOptions"
                  :key="o.value"
                  class="dd-item"
                  :class="{ active: mode === o.value }"
                  role="option"
                  @click.stop="pickMode(o.value)"
                >
                  {{ o.label }}
                </li>
              </ul>
            </transition>
          </div>

          <div class="dd dd-search" :class="{ open: openMenu === 'search' }">
            <button
              type="button"
              class="dd-trigger"
              :disabled="busy"
              aria-haspopup="listbox"
              :aria-expanded="openMenu === 'search'"
              @click.stop="toggleMenu('search')"
            >
              <span class="dd-prefix">搜索</span>
              <span class="dd-value">{{ searchLabel }}</span>
              <svg class="dd-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>
            <transition name="dd-pop">
              <ul v-show="openMenu === 'search'" class="dd-list" role="listbox">
                <li
                  v-for="o in searchOptions"
                  :key="o.value"
                  class="dd-item"
                  :class="{ active: searchMode === o.value }"
                  role="option"
                  @click.stop="pickSearch(o.value)"
                >
                  {{ o.label }}
                </li>
              </ul>
            </transition>
          </div>
        </div>
        <div class="bar-fill" aria-hidden="true" />
        <div class="bar-right">
          <input ref="fileInput" type="file" multiple class="hidden-file" @change="onFileChange" />
          <button type="button" class="ico" :disabled="busy" title="附件" @click="$refs.fileInput.click()">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path
                d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"
              />
            </svg>
          </button>
          <button v-if="busy" type="button" class="ico danger" title="停止" @click="$emit('stop')">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <rect x="7" y="7" width="10" height="10" rx="1" />
            </svg>
          </button>
          <button
            type="button"
            class="send"
            :disabled="busy || (!draft.trim() && attachments.length === 0)"
            title="发送"
            @click="send"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>
      </div>
      <div class="foot-hint">
        <span v-if="busy">处理中…</span>
        <span v-else>文档将上传解析后加入本轮上下文</span>
      </div>
    </div>
  </div>
</template>

<script>
import { API_BASE } from "../apiBase.js";

const MODE_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "refine", label: "精化轨" },
  { value: "fast", label: "快速轨" },
];

const SEARCH_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "on", label: "开启" },
  { value: "off", label: "关闭" },
];

export default {
  name: "ChatInput",
  props: {
    busy: { type: Boolean, default: false },
    mode: { type: String, default: "auto" },
  },
  emits: ["send", "stop", "update:mode"],
  data() {
    return {
      draft: "",
      attachments: [],
      searchMode: "auto",
      openMenu: null,
      modeOptions: MODE_OPTIONS,
      searchOptions: SEARCH_OPTIONS,
    };
  },
  computed: {
    modeLabel() {
      return MODE_OPTIONS.find((o) => o.value === this.mode)?.label ?? this.mode;
    },
    searchLabel() {
      return SEARCH_OPTIONS.find((o) => o.value === this.searchMode)?.label ?? this.searchMode;
    },
  },
  watch: {
    busy(v) {
      if (v) this.openMenu = null;
    },
  },
  mounted() {
    this._onDocDown = (e) => {
      const root = this.$refs.dropRoot;
      if (!root || root.contains(e.target)) return;
      this.openMenu = null;
    };
    document.addEventListener("pointerdown", this._onDocDown, true);
  },
  beforeUnmount() {
    document.removeEventListener("pointerdown", this._onDocDown, true);
  },
  methods: {
    toggleMenu(which) {
      if (this.busy) return;
      this.openMenu = this.openMenu === which ? null : which;
    },
    pickMode(value) {
      this.$emit("update:mode", value);
      this.openMenu = null;
    },
    pickSearch(value) {
      this.searchMode = value;
      this.openMenu = null;
    },
    statusLabel(s) {
      if (s === "ok") return "已解析";
      if (s === "error") return "失败";
      if (s === "parsing") return "解析中…";
      return "";
    },
    removeAttachment(idx) {
      this.attachments.splice(idx, 1);
    },
    async onFileChange(e) {
      const files = e.target.files;
      if (!files || !files.length) return;
      const list = Array.from(files);
      this.$refs.fileInput.value = "";

      const docFiles = list.filter((f) => !f.type.startsWith("image/"));
      const imageFiles = list.filter((f) => f.type.startsWith("image/"));

      for (const file of imageFiles) {
        await new Promise((resolve) => {
          const reader = new FileReader();
          reader.onload = (ev) => {
            this.attachments.push({
              name: file.name,
              kind: "image",
              type: file.type,
              data: ev.target.result,
              status: "ok",
            });
            resolve();
          };
          reader.readAsDataURL(file);
        });
      }

      if (docFiles.length === 0) return;

      const form = new FormData();
      docFiles.forEach((f) => form.append("files", f));
      docFiles.forEach((f) =>
        this.attachments.push({ name: f.name, kind: "document", status: "parsing", doc: null })
      );
      const parseSlotsStart = this.attachments.length - docFiles.length;

      try {
        const controller = new AbortController();
        const res = await fetch(`${API_BASE}/api/documents/parse`, {
          method: "POST",
          body: form,
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const docs = data.documents || [];
        docs.forEach((doc, i) => {
          const slot = parseSlotsStart + i;
          if (this.attachments[slot]) {
            this.attachments[slot].doc = doc;
            this.attachments[slot].status = doc.status === "ok" ? "ok" : "error";
          }
        });
      } catch (err) {
        for (let i = 0; i < docFiles.length; i++) {
          const slot = parseSlotsStart + i;
          if (this.attachments[slot]) {
            this.attachments[slot].status = "error";
            this.attachments[slot].error = String(err?.message || err);
          }
        }
      }
    },
    send() {
      const t = this.draft.trim();
      if (!t && this.attachments.length === 0) return;
      if (this.busy) return;

      const documents = this.attachments
        .filter((a) => a.kind === "document" && a.status === "ok" && a.doc)
        .map((a) => a.doc);

      let content = t;
      const imgs = this.attachments.filter((a) => a.kind === "image" && a.status === "ok");
      if (imgs.length > 0 || documents.length > 0) {
        content = [];
        if (t) content.push({ type: "text", text: t });
        imgs.forEach((a) => content.push({ type: "image_url", image_url: { url: a.data } }));
      }

      this.$emit("send", {
        content,
        documents,
        searchMode: this.searchMode,
      });
      this.draft = "";
      this.attachments = [];
    },
  },
};
</script>

<style scoped>
.hidden-file {
  display: none;
}
.wrap {
  padding: 6px 16px 14px;
  background: #1a1f2b;
  border-top: 1px solid #2f3a4d;
  flex-shrink: 0;
}
.composer {
  width: 100%;
  max-width: min(100%, 980px);
  margin: 0 auto;
  border: 1px solid #3d4d64;
  border-radius: 14px;
  background: #252d3d;
  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.22);
  padding: 8px 12px 7px;
}
.att-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.att-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: #1e2433;
  border: 1px solid #3d4d64;
  padding: 4px 8px 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  color: #cbd5e1;
}
.att-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.att-status {
  font-size: 11px;
  color: #64748b;
}
.att-status.ok {
  color: #6ee7b7;
}
.att-status.error {
  color: #fca5a5;
}
.att-del {
  background: none;
  border: none;
  color: #64748b;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  padding: 0 2px;
}
.att-del:hover {
  color: #ef4444;
}
.textarea {
  width: 100%;
  min-height: 52px;
  max-height: 220px;
  resize: vertical;
  padding: 4px 2px;
  border: none;
  background: transparent;
  color: #f1f5f9;
  outline: none;
  line-height: 1.5;
  font-size: 15px;
  font-family: inherit;
}
.textarea::placeholder {
  color: #64748b;
}
.textarea:disabled {
  opacity: 0.6;
}
.composer-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: nowrap;
  padding-top: 8px;
  border-top: 1px solid #2f3545;
  margin-top: 2px;
}
.bar-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
}
.bar-fill {
  flex: 1;
  min-width: 24px;
  min-height: 34px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.12);
  border: 1px solid #2f3545;
}

/* 自定义下拉（替代原生 select，避免系统白底菜单） */
.dd {
  position: relative;
  z-index: 1;
}
.dd.open {
  z-index: 40;
}
.dd-trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px 5px 11px;
  border-radius: 999px;
  border: 1px solid #353f52;
  background: #1e2433;
  color: #e2e8f0;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
  line-height: 1.25;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.dd-trigger:hover:not(:disabled) {
  border-color: #4b5c78;
  background: #252d3d;
}
.dd-trigger:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.dd-prefix {
  color: #94a3b8;
  font-weight: 500;
}
.dd-value {
  font-weight: 600;
  min-width: 3em;
  text-align: left;
}
.dd-chev {
  flex-shrink: 0;
  color: #64748b;
  transition: transform 0.18s ease;
}
.dd.open .dd-chev {
  transform: rotate(180deg);
  color: #94a3b8;
}
.dd-list {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  margin: 0;
  padding: 6px;
  min-width: 128px;
  list-style: none;
  border-radius: 10px;
  border: 1px solid #3d4d64;
  background: #1e2433;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
}
.dd-search .dd-list {
  min-width: 100px;
}
.dd-item {
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: #cbd5e1;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease;
}
.dd-item:hover {
  background: #2f3a4d;
  color: #f1f5f9;
}
.dd-item.active {
  background: rgba(99, 102, 241, 0.2);
  color: #c7d2fe;
}

.dd-pop-enter-active,
.dd-pop-leave-active {
  transition: opacity 0.14s ease, transform 0.14s ease;
}
.dd-pop-enter-from,
.dd-pop-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

.bar-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.ico {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  border: 1px solid #353f52;
  background: #1e2433;
  color: #94a3b8;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.ico:hover:not(:disabled) {
  background: #2f3a4d;
  color: #e2e8f0;
}
.ico:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ico.danger {
  color: #fca5a5;
  border-color: rgba(248, 113, 113, 0.35);
  background: rgba(248, 113, 113, 0.1);
}
.send {
  width: 36px;
  height: 36px;
  border-radius: 999px;
  border: none;
  background: linear-gradient(135deg, #4f46e5, #6366f1);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.28);
}
.send:hover:not(:disabled) {
  filter: brightness(1.05);
}
.send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  box-shadow: none;
}
.foot-hint {
  margin-top: 5px;
  padding-left: 2px;
  font-size: 11px;
  color: #5c6d85;
}
@media (max-width: 560px) {
  .composer-bar {
    flex-wrap: wrap;
  }
  .bar-fill {
    display: none;
  }
}
</style>
