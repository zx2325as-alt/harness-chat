<template>
  <div class="wrap">
    <div class="composer">
      <div v-if="attachments.length > 0" class="att-row">
        <div class="att-item" v-for="(att, idx) in attachments" :key="att.id || idx" :title="attachmentTitle(att)">
          <span class="att-name">{{ att.name }}</span>
          <span class="att-status" :class="att.status">{{ statusLabel(att.status) }}</span>
          <span v-if="att.error" class="att-error">{{ att.error }}</span>
          <button
            v-if="att.status === 'error' && att.kind === 'document' && att.file"
            type="button"
            class="att-retry"
            @click="retryParse(idx)"
          >
            重试
          </button>
          <button type="button" class="att-del" @click="removeAttachment(idx)" aria-label="移除">×</button>
        </div>
      </div>

      <textarea
        class="textarea"
        :disabled="uiBusy"
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
              :disabled="uiBusy"
              :title="modeHint"
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

          <div class="dd dd-search" :class="{ open: openMenu === 'search', locked: globalSearchDisabled }">
            <button
              type="button"
              class="dd-trigger dd-trigger-pill"
              :disabled="uiBusy || globalSearchDisabled"
              :title="searchModeHint"
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
          <input
            ref="fileInput"
            type="file"
            multiple
            class="hidden-file"
            :accept="fileAccept"
            @change="onFileChange"
          />
          <button type="button" class="ico" :disabled="uiBusy" title="上传文件" @click="$refs.fileInput.click()">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path
                d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"
              />
            </svg>
          </button>
          <button type="button" class="ico folder-btn" :disabled="uiBusy" title="读本地文件夹" @click="readLocalFolder">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              <path d="M8 13h8" />
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
            :disabled="!canSend"
            :title="sendTitle"
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
        <span v-else-if="folderLoading">正在读取本地文件夹…</span>
        <span v-else-if="hasParsingAttachment">文档仍在解析中，完成后才能发送</span>
        <span v-else-if="hasImageAttachment">图片目前不会进入模型，请先移除图片或改传文档/文本</span>
        <span v-else-if="globalSearchDisabled">服务器已全局禁止联网；搜索选项已锁定为关闭并与配置同步。</span>
        <span v-else>支持上传文件，也支持直接读取服务端本地文件夹中的文档与代码文件</span>
      </div>
    </div>
  </div>
</template>

<script>
import { API_BASE } from "../apiBase.js";
import { isSendableComposerState } from "../chatShared.js";

const MODE_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "agent", label: "Agent 轨（仅流式真 Agent）" },
  { value: "refine", label: "精化轨" },
  { value: "fast", label: "快速轨" },
];

const SEARCH_OPTIONS = [
  { value: "auto", label: "自动（快轨）" },
  { value: "on", label: "开启（快轨）" },
  { value: "off", label: "关闭（快轨）" },
];
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const UPLOAD_CONCURRENCY = 3;
const SUPPORTED_DOCUMENT_EXTS = [
  "txt",
  "md",
  "markdown",
  "json",
  "csv",
  "pdf",
  "doc",
  "docx",
  "xls",
  "xlsx",
  "yaml",
  "yml",
  "toml",
  "ini",
  "cfg",
  "conf",
  "env",
  "log",
  "xml",
  "html",
  "htm",
  "css",
  "scss",
  "less",
  "js",
  "mjs",
  "cjs",
  "jsx",
  "ts",
  "tsx",
  "py",
  "java",
  "kt",
  "kts",
  "scala",
  "go",
  "rs",
  "c",
  "cc",
  "cpp",
  "cxx",
  "h",
  "hh",
  "hpp",
  "cs",
  "php",
  "rb",
  "swift",
  "sh",
  "bash",
  "zsh",
  "ps1",
  "sql",
  "r",
  "lua",
  "pl",
  "pm",
  "proto",
  "properties",
  "gradle",
  "vue",
  "svelte",
  "dart",
];
const SPECIAL_DOCUMENT_NAMES = [
  "dockerfile",
  "makefile",
  "jenkinsfile",
  ".env",
  ".gitignore",
  ".npmrc",
  ".yarnrc",
  ".editorconfig",
  ".prettierrc",
  ".eslintrc",
  "cmakelists.txt",
];
const SUPPORTED_DOCUMENT_EXT_SET = new Set([...SUPPORTED_DOCUMENT_EXTS, ...SPECIAL_DOCUMENT_NAMES]);
const FILE_ACCEPT = SUPPORTED_DOCUMENT_EXTS.map((ext) => `.${ext}`).join(",");

export default {
  name: "ChatInput",
  props: {
    busy: { type: Boolean, default: false },
    mode: { type: String, default: "auto" },
    /** 服务端 harness.web_search.globally_disabled：强制关闭搜索并不允许切换 */
    globalSearchDisabled: { type: Boolean, default: false },
  },
  emits: ["send", "stop", "update:mode"],
  data() {
    return {
      draft: "",
      attachments: [],
      folderLoading: false,
      searchMode: "auto",
      openMenu: null,
      modeOptions: MODE_OPTIONS,
      _parseControllers: new Set(),
      _parseControllerByAttachmentId: new Map(),
    };
  },
  computed: {
    hasParsingAttachment() {
      return this.folderLoading || this.attachments.some((a) => a.status === "parsing");
    },
    hasImageAttachment() {
      return this.attachments.some((a) => a.kind === "image");
    },
    canSend() {
      return isSendableComposerState({
        draft: this.draft,
        attachments: this.attachments,
        busy: this.uiBusy,
        hasParsingAttachment: this.hasParsingAttachment,
        hasImageAttachment: this.hasImageAttachment,
      });
    },
    uiBusy() {
      return this.busy || this.folderLoading;
    },
    sendTitle() {
      if (this.folderLoading) return "正在读取本地文件夹";
      if (this.hasParsingAttachment) return "文档仍在解析中";
      if (this.hasImageAttachment) return "图片暂未接入模型，请移除后发送";
      if (!this.draft.trim() && !this.attachments.some((a) => a.kind === "document" && a.status === "ok" && a.doc)) {
        return "无可发送内容";
      }
      return "发送";
    },
    modeLabel() {
      return MODE_OPTIONS.find((o) => o.value === this.mode)?.label ?? this.mode;
    },
    modeHint() {
      return "Agent 真循环仅在流式接口可用；同步 /api/chat 即使选择 Agent 也会降级为 Refine。";
    },
    searchLabel() {
      if (this.globalSearchDisabled) return "关闭（全局锁定）";
      return SEARCH_OPTIONS.find((o) => o.value === this.searchMode)?.label ?? this.searchMode;
    },
    searchOptions() {
      if (this.globalSearchDisabled) {
        return [{ value: "off", label: "关闭（全局锁定）" }];
      }
      return SEARCH_OPTIONS;
    },
    searchModeHint() {
      if (this.globalSearchDisabled) {
        return "服务器配置已全局禁止联网；无法在此开启搜索。";
      }
      return "仅影响「快速轨」入口是否做前置联网检索；精化轨 / Agent 仍按内部逻辑按需搜索。";
    },
    fileAccept() {
      return FILE_ACCEPT;
    },
  },
  watch: {
    busy(v) {
      if (v) this.openMenu = null;
    },
    globalSearchDisabled: {
      immediate: true,
      handler(on) {
        if (on) {
          this.searchMode = "off";
          if (this.openMenu === "search") this.openMenu = null;
        }
      },
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
    this._parseControllers.forEach((controller) => controller.abort());
    this._parseControllers.clear();
    this._parseControllerByAttachmentId.clear();
  },
  methods: {
    newAttachmentId() {
      return `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    },
    toggleMenu(which) {
      if (this.uiBusy) return;
      if (which === "search" && this.globalSearchDisabled) return;
      this.openMenu = this.openMenu === which ? null : which;
    },
    pickMode(value) {
      this.$emit("update:mode", value);
      this.openMenu = null;
    },
    pickSearch(value) {
      if (this.globalSearchDisabled && value !== "off") return;
      this.searchMode = value;
      this.openMenu = null;
    },
    syncFromServerConfig(config) {
      const g = Boolean(config?.harness?.web_search?.globally_disabled);
      if (g) {
        this.searchMode = "off";
        if (this.openMenu === "search") this.openMenu = null;
      }
    },
    statusLabel(s) {
      if (s === "ok") return "已解析";
      if (s === "error") return "失败";
      if (s === "parsing") return "解析中…";
      return "";
    },
    attachmentTitle(att) {
      if (!att) return "";
      return att.error ? `${att.name}\n${att.error}` : att.name;
    },
    detectDocumentExt(name) {
      const lower = String(name || "").trim().toLowerCase();
      if (!lower) return "";
      if (SPECIAL_DOCUMENT_NAMES.includes(lower)) return lower;
      const idx = lower.lastIndexOf(".");
      return idx >= 0 ? lower.slice(idx + 1) : "";
    },
    isImageFile(file) {
      const name = String(file?.name || "").toLowerCase();
      return Boolean(file?.type?.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(name));
    },
    isSupportedDocumentFile(file) {
      return SUPPORTED_DOCUMENT_EXT_SET.has(this.detectDocumentExt(file?.name));
    },
    formatFileSize(size) {
      if (!Number.isFinite(size) || size <= 0) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let value = size;
      let unitIdx = 0;
      while (value >= 1024 && unitIdx < units.length - 1) {
        value /= 1024;
        unitIdx += 1;
      }
      return `${value >= 10 || unitIdx === 0 ? Math.round(value) : value.toFixed(1)} ${units[unitIdx]}`;
    },
    createLocalErrorAttachment(file, error, kind = "document") {
      this.attachments.push({
        id: this.newAttachmentId(),
        name: file?.name || "未命名文件",
        kind,
        type: file?.type || "",
        status: "error",
        error,
        file: kind === "document" ? file : null,
      });
    },
    currentDocumentCount() {
      return this.attachments.filter((att) => att.kind === "document").length;
    },
    attachmentSignature(file) {
      return [file?.name || "", file?.size || 0, file?.lastModified || 0].join("|");
    },
    existingAttachmentSignatures() {
      return new Set(
        this.attachments
          .filter((att) => att?.file)
          .map((att) => this.attachmentSignature(att.file))
      );
    },
    documentPayloadSignature(doc) {
      if (!doc || typeof doc !== "object") return "";
      const chunks = Array.isArray(doc.chunks)
        ? doc.chunks.slice(0, 24).map((chunk) => ({
            index: chunk?.index ?? "",
            content: String(chunk?.content || "").slice(0, 120),
          }))
        : [];
      return JSON.stringify({
        name: String(doc.name || ""),
        ext: String(doc.ext || ""),
        status: String(doc.status || ""),
        content: String(doc.content || "").slice(0, 500),
        chunks,
      });
    },
    existingDocumentPayloadSignatures() {
      return new Set(
        this.attachments
          .filter((att) => att?.kind === "document" && att?.doc)
          .map((att) => this.documentPayloadSignature(att.doc))
          .filter(Boolean)
      );
    },
    async extractApiError(res) {
      try {
        const data = await res.json();
        return data?.detail || data?.error || `HTTP ${res.status}`;
      } catch (_) {
        const text = await res.text().catch(() => "");
        return text || `HTTP ${res.status}`;
      }
    },
    applyParsedDocumentResult(att, doc) {
      if (!att) return;
      if (doc) {
        att.doc = doc;
        att.status = doc.status === "ok" ? "ok" : "error";
        att.error = doc.status === "ok" ? "" : doc.error || "解析失败";
      } else {
        att.status = "error";
        att.error = "解析结果为空";
      }
    },
    async uploadDocumentAttachment(att, file) {
      const form = new FormData();
      form.append("files", file);
      form.append("client_file_ids", att.id);
      const controller = new AbortController();
      this._parseControllers.add(controller);
      this._parseControllerByAttachmentId.set(att.id, controller);
      try {
        const res = await fetch(`${API_BASE}/api/documents/parse`, {
          method: "POST",
          body: form,
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(await this.extractApiError(res));
        const data = await res.json();
        const docs = data.documents || [];
        const doc = docs.find((item) => item && item.client_file_id === att.id) || docs[0];
        const current = this.attachments.find((item) => item.id === att.id);
        this.applyParsedDocumentResult(current, doc);
      } catch (err) {
        if (err?.name === "AbortError") return;
        const current = this.attachments.find((item) => item.id === att.id);
        if (current) {
          current.status = "error";
          current.error = String(err?.message || err);
        }
      } finally {
        this._parseControllers.delete(controller);
        this._parseControllerByAttachmentId.delete(att.id);
      }
    },
    async runUploadQueue(items) {
      const queue = items.slice();
      const workers = Array.from({ length: Math.min(UPLOAD_CONCURRENCY, queue.length) }, async () => {
        while (queue.length > 0) {
          const item = queue.shift();
          if (!item) break;
          await this.uploadDocumentAttachment(item.att, item.file);
        }
      });
      await Promise.all(workers);
    },
    appendFolderDocuments(documents) {
      const existingDocSigs = this.existingDocumentPayloadSignatures();
      const selectedDocSigs = new Set();
      (documents || []).forEach((doc) => {
        if (!doc || !doc.name) return;
        const docSig = this.documentPayloadSignature(doc);
        if (docSig && (existingDocSigs.has(docSig) || selectedDocSigs.has(docSig))) {
          this.createLocalErrorAttachment(
            { name: doc.name, type: "" },
            "该文件已在附件列表中，无需重复导入。"
          );
          return;
        }
        if (docSig) selectedDocSigs.add(docSig);
        this.attachments.push({
          id: this.newAttachmentId(),
          name: doc.name,
          kind: "document",
          status: doc.status === "ok" ? "ok" : "error",
          doc: doc.status === "ok" ? doc : null,
          error: doc.status === "ok" ? "" : doc.error || "解析失败",
          file: null,
          source: "local_folder",
        });
      });
    },
    async readLocalFolder() {
      if (this.uiBusy) return;
      const folderPath = window.prompt("请输入服务端本地文件夹路径", "");
      if (folderPath == null) return;
      const normalizedPath = String(folderPath).trim();
      if (!normalizedPath) return;
      const recursive = window.confirm("是否递归读取子目录？\n确定：递归\n取消：仅当前目录");
      this.folderLoading = true;
      try {
        const res = await fetch(`${API_BASE}/api/documents/parse_folder`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            folder_path: normalizedPath,
            recursive,
          }),
        });
        if (!res.ok) {
          throw new Error(await this.extractApiError(res));
        }
        const data = await res.json();
        const docs = data.documents || [];
        if (!docs.length) {
          this.createLocalErrorAttachment(
            { name: normalizedPath, type: "" },
            "该文件夹下没有读取到支持的文档文件。"
          );
          return;
        }
        this.appendFolderDocuments(docs);
      } catch (err) {
        this.createLocalErrorAttachment(
          { name: normalizedPath, type: "" },
          String(err?.message || err)
        );
      } finally {
        this.folderLoading = false;
      }
    },
    removeAttachment(idx) {
      const att = this.attachments[idx];
      if (att?.id) {
        const controller = this._parseControllerByAttachmentId.get(att.id);
        if (controller) {
          controller.abort();
          this._parseControllerByAttachmentId.delete(att.id);
          this._parseControllers.delete(controller);
        }
      }
      this.attachments.splice(idx, 1);
    },
    async retryParse(idx) {
      const att = this.attachments[idx];
      if (!att || att.kind !== "document" || !att.file || this.uiBusy) return;
      att.status = "parsing";
      att.error = "";
      await this.uploadDocumentAttachment(att, att.file);
    },
    /** 从用户消息恢复输入区（编辑 / 多模态） */
    prefillFromUserEdit({ text = "", images = [], documents = [] }) {
      this.draft = text;
      this.attachments = [];
      (images || []).forEach((im) => {
        if (im && im.url) {
          this.attachments.push({
            id: this.newAttachmentId(),
            name: im.name || "图片",
            kind: "image",
            type: im.type || "image/png",
            data: im.url,
            status: "ok",
          });
        }
      });
      (documents || []).forEach((d) => {
        if (d && d.name) {
          this.attachments.push({
            id: this.newAttachmentId(),
            name: d.name,
            kind: "document",
            status: d.status === "ok" ? "ok" : "error",
            doc: d.status === "ok" ? d : null,
            error: d.error,
          });
        }
      });
    },
    async onFileChange(e) {
      const files = e.target.files;
      if (!files || !files.length) return;
      const list = Array.from(files);
      this.$refs.fileInput.value = "";
      const existingSigs = this.existingAttachmentSignatures();
      const selectedSigs = new Set();
      const readyFiles = [];

      for (const file of list) {
        const signature = this.attachmentSignature(file);
        if (existingSigs.has(signature) || selectedSigs.has(signature)) {
          this.createLocalErrorAttachment(file, "该文件已在附件列表中，无需重复上传。");
          continue;
        }
        selectedSigs.add(signature);

        if (this.isImageFile(file)) {
          this.createLocalErrorAttachment(file, "图片暂未接入后端模型输入，请改传文档或文本。", "image");
          continue;
        }
        if (!this.isSupportedDocumentFile(file)) {
          this.createLocalErrorAttachment(
            file,
            "暂不支持该格式。当前支持常见文档、配置和代码文件，如 py、java、js、ts、go、rs、c/c++、sql、yaml、vue 等。"
          );
          continue;
        }
        if (file.size > MAX_ATTACHMENT_BYTES) {
          this.createLocalErrorAttachment(
            file,
            `文件过大：${this.formatFileSize(file.size)}，当前单文件上限为 ${this.formatFileSize(MAX_ATTACHMENT_BYTES)}。`
          );
          continue;
        }
        readyFiles.push(file);
      }

      if (readyFiles.length === 0) return;

      const items = readyFiles.map((f) => {
        const att = {
          id: this.newAttachmentId(),
          name: f.name,
          kind: "document",
          status: "parsing",
          doc: null,
          error: "",
          file: f,
        };
        this.attachments.push(att);
        return { file: f, att };
      });
      await this.runUploadQueue(items);
    },
    send() {
      const t = this.draft.trim();
      const documents = this.attachments
        .filter((a) => a.kind === "document" && a.status === "ok" && a.doc)
        .map((a) => a.doc);
      if (!t && documents.length === 0) return;
      if (!this.canSend) return;

      this.$emit("send", {
        content: t,
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
  flex-wrap: wrap;
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
.att-error {
  max-width: 240px;
  color: #fca5a5;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.att-retry {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid #4b5c78;
  background: #2f3a4d;
  color: #a5b4fc;
  cursor: pointer;
}
.att-retry:hover {
  border-color: rgba(129, 140, 248, 0.5);
  color: #e0e7ff;
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
.dd-search.locked .dd-trigger-pill {
  opacity: 0.72;
  cursor: not-allowed;
}
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
