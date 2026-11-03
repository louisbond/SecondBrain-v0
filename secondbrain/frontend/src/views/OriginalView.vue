<script setup>
/**
 * 原创区：写你自己的东西（与加工区并列的一级入口）。
 *
 * - 空数据模板：选一个模板生成骨架（带建议分区/标签），填详情即可归档
 * - 加工区「送去原创区」的解析稿会自动载入（sessionStorage）
 * - 产出 C 层笔记
 */
import { ref, computed, onMounted, onUnmounted, inject, watch } from 'vue'
import { notesApi, noteTemplatesApi } from '../api/client'
import { loadDraft, clearDraft } from '../utils/draft'
// **必须引入 Vditor 的样式**（v35 修）。
// 坑：从接入 Vditor 起就一直漏了这句。Javascript 能跑、DOM 能建、
// setValue 也能写进去（所以测试一直"通过"），但没有 CSS 的编辑器
// 是**裸 DOM** —— 工具栏图标按原始尺寸撑成巨图，用户看到的是
// 一张笑脸 + 超大的 B / T 图标，完全没法编辑。
// 教训：DOM 断言（editable=True / toolbar=True）证明不了"能用"，
// 视觉类组件必须截图看。
import 'vditor/dist/index.css'

const toast = inject('toast')

const editorEl = ref(null)
const draftTitle = ref('')
const draftTags = ref([])
const inspiration = ref([])
const inspirationAvailable = ref(true)
const saving = ref(false)
const activeTpl = ref('')
let editor = null
let editorReady = null
let inspirationTimer = null

// 空数据模板：**从后端读**（v37）。
//
// 之前这 6 个模板硬编码在这里（知识提炼 / 知识聚合 / 日记 / 周回顾 /
// 规则模板 / 基本概念），想加一个得改代码重新打包。
// 用户："原创区的模板可在管理区添加" → 现在落库，管理区增删改，这里只读。
//
// 注意与「解析模板」的区别：
//   解析模板（/api/workbench/templates）是给 **AI** 的提示词；
//   创作模板（/api/note-templates）是给 **人** 的 Markdown 骨架。
//
// 约定（历史上踩过的坑，模板正文照此写）：
//   · **不写 YAML frontmatter** —— 后端 `notes_service.create` 会自己生成
//     （title/type/tags/partitions/created），正文再写一段 --- 就成双重
//     frontmatter，Obsidian 打不开。
//   · 占位符 `{{date}}` `{{time}}` `{{title}}` 由 fillPlaceholders 替换。
//   · `[[双链]]` 会被后端抽成 citations。
const templates = ref([])
const templatesError = ref('')

/** 拿不到后端时的兜底：至少能写空白页（不阻塞创作）。 */
const FALLBACK_TEMPLATES = [
  { id: 'blank', name: '空白页', desc: '', sections: '', suggest_tags: [], suggest_partition: '' }
]

async function loadTemplates() {
  try {
    const r = await noteTemplatesApi.list()
    const list = (r.templates || []).map((t) => ({
      id: t.id,
      name: t.name,
      desc: t.description || '',
      sections: t.body || '',
      suggest_tags: t.suggest_tags || [],
      suggest_partition: t.suggest_partition || ''
    }))
    // 管理区把模板全删了也给个空白页兜底，否则按钮区空着没法写
    templates.value = list.length
      ? [...list, ...FALLBACK_TEMPLATES]
      : [...FALLBACK_TEMPLATES]
    templatesError.value = ''
  } catch (e) {
    templates.value = [...FALLBACK_TEMPLATES]
    templatesError.value = '模板加载失败（' + (e.message || e) + '），先给个空白页。'
  }
}

/**
 * 替换 Obsidian 占位符。
 *
 * 支持 `{{date}}` / `{{time}}` / `{{title}}`，
 * 也支持 Obsidian 核心模板的带格式写法 `{{date:YYYY-MM-DD}}`。
 * 这里只实现最常用的三个日期格式，够日常用。
 */
function fillPlaceholders(text) {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const ymd = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  const hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`
  return String(text || '')
    .replace(/\{\{\s*date\s*(?::\s*([^}]+))?\s*\}\}/g, (_m, fmt) => {
      if (!fmt) return ymd
      // 只支持 Obsidian 最常用的几个格式串
      return String(fmt)
        .replace(/YYYY/g, String(now.getFullYear()))
        .replace(/MM/g, pad(now.getMonth() + 1))
        .replace(/DD/g, pad(now.getDate()))
        .replace(/HH/g, pad(now.getHours()))
        .replace(/mm/g, pad(now.getMinutes()))
    })
    .replace(/\{\{\s*time\s*\}\}/g, hm)
    .replace(/\{\{\s*title\s*\}\}/g, draftTitle.value.trim() || '未命名')
}

/** 应用空模板：生成骨架，便于填详情 + 分区。 */
function applyTemplate(tpl) {
  activeTpl.value = tpl.id
  if (!draftTitle.value && tpl.name !== '空白页') {
    draftTitle.value = tpl.name + '·'
  }
  if (tpl.sections) setEditorValue(fillPlaceholders(tpl.sections))
  for (const t of tpl.suggest_tags) {
    if (!draftTags.value.includes(t)) draftTags.value.push(t)
  }
  scheduleInspiration()
}

// ---------------- Vditor 编辑器 ----------------
// 三个坑叠在一起，症状都是"编辑器空白"或"写不进去"：
//
// 1) Vditor 3.11 起 cache.enable 默认 true，且**必须**同时给 cache.id，
//    否则构造时直接抛 "need options.cache.id" → 编辑器整个不渲染。
//    （报错在 Promise 里，页面看不出异常，只是「编辑器空白」）
// 2) cdn 必须指向**本地**资源（public/vditor/）：默认走 unpkg.com，
//    而 SecondBrain 是本机离线应用，CDN 取不到 lute.min.js 时
//    编辑器框架能出来但内容写不进去。
// 3) `new Vditor()` 是**异步初始化**的：内部先 loadScript(i18n) → 再 loadScript(lute)
//    → 才调 init() 建出真正的编辑器。构造函数返回时 `this.vditor` 还是 undefined，
//    此时调 setValue() 会抛 "Cannot read properties of undefined (reading 'currentMode')"。
//    所以必须等 `editor.vditor` 出现（= init 完成）才能写内容。

const VEDITOR_CDN = '/vditor'

function ensureEditor() {
  if (editor) return Promise.resolve(editor)
  if (editorReady) return editorReady
  editorReady = import('vditor').then(({ default: Vditor }) => {
    editor = new Vditor(editorEl.value, {
      // v35：撑满剩余高度（父容器 .vditor-host 有确定高度）
      height: '100%',
      // v36：**深色主题**。用户："原创区的编辑区域白色不和背景，变深色"。
      //
      // Vditor 的暗色不是另装一套 CSS，而是给根节点加 `.vditor--dark`，
      // 靠覆盖 CSS 变量（--panel-background-color / --textarea-*）实现。
      // 默认暗色是 GitHub 的 #24292e，和本站的 #161920 不搭，所以下面
      // <style> 里再把变量对齐到 --bg-panel / --bg-input。
      theme: 'dark',
      placeholder: '写下你的思考。解析结果、引用建议都可以一键插入。',
      mode: 'sv',
      cache: { enable: false },
      cdn: VEDITOR_CDN,
      // 预览栏的内容主题也要 dark（否则右侧白底黑字，一半亮一半暗）。
      // 注意：这个 css 是从 {cdn}/dist/css/content-theme/dark.css 加载的
      // 静态文件，必须真的存在（`sync-vditor-assets.mjs` 已加进拷贝清单）。
      preview: {
        theme: { current: 'dark', path: `${VEDITOR_CDN}/dist/css/content-theme` }
      },
      toolbar: ['emoji', 'bold', 'italic', 'strike', '|', 'list', 'ordered', 'check', '|', 'quote', 'link', 'undo', 'redo']
    })
    return waitEditorReady(editor)
  })
  return editorReady
}

/** 等 Vditor 异步初始化完成（`editor.vditor` 就位才算 ready）。 */
function waitEditorReady(ed, timeout = 8000) {
  return new Promise((resolve, reject) => {
    const started = Date.now()
    const tick = () => {
      if (ed && ed.vditor) {
        resolve(ed)
        return
      }
      if (Date.now() - started > timeout) {
        reject(new Error('编辑器初始化超时'))
        return
      }
      setTimeout(tick, 60)
    }
    tick()
  })
}

function setEditorValue(v) {
  ensureEditor()
    .then((ed) => ed.setValue(v))
    .catch((e) => {
      /* 编辑器加载失败：提示而不是静默吞掉 */
      toast('编辑器加载失败：' + (e?.message || e), 'error')
    })
}

// ---------------- 引用建议 ----------------
async function refreshInspiration() {
  const body = (editor?.getValue() || '').slice(0, 800)
  if (!draftTitle.value && body.length < 20) {
    inspiration.value = []
    return
  }
  try {
    const data = await notesApi.inspiration(draftTitle.value, body, 5)
    inspiration.value = data.results || []
    inspirationAvailable.value = data.available !== false
  } catch (e) {
    inspirationAvailable.value = false
  }
}

function scheduleInspiration() {
  clearTimeout(inspirationTimer)
  inspirationTimer = setTimeout(refreshInspiration, 900)
}

function insertCitation(item) {
  if (!editor) return
  const link = `[[${item.title || item.note_path}]]`
  const value = editor.getValue() || ''
  editor.setValue(value + (value && !value.endsWith('\n') ? '\n' : '') + link + ' ')
  editor.focus()
  scheduleInspiration()
}

// ---------------- 草稿载入 ----------------
function loadFromDraft() {
  const d = loadDraft()
  if (!d) return
  draftTitle.value = d.title || ''
  draftTags.value = d.tags || []
  setEditorValue(d.body || '')
  if (d.title) scheduleInspiration()
  clearDraft()
}

// ---------------- 保存 ----------------
async function saveDraftNote() {
  const body = editor?.getValue() || ''
  if (!draftTitle.value.trim()) {
    toast('先给稿子起个标题', 'warn')
    return
  }
  if (body.trim().length < 10) {
    toast('正文太短了，至少写 10 个字', 'warn')
    return
  }
  saving.value = true
  try {
    const res = await notesApi.create({
      title: draftTitle.value.trim(),
      // 字段名必须是 body —— 后端 CreateNoteRequest.body。
      // 之前写的是 content，后端不认（body 默认 ""），
      // 正文被静默丢弃，存出来的 md 只有 frontmatter 没有正文。
      body,
      tags: draftTags.value,
      partitions: []
    })
    toast('创作笔记已保存', 'success')
    draftTitle.value = ''
    draftTags.value = []
    setEditorValue('')
    await refreshInspiration()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  } finally {
    saving.value = false
  }
}

function addDraftTag(event) {
  const value = event.target.value.trim()
  if (!value) return
  if (!draftTags.value.includes(value)) draftTags.value.push(value)
  event.target.value = ''
}

watch(draftTitle, scheduleInspiration)

onMounted(() => {
  loadTemplates()
  ensureEditor().catch(() => {
    /* 已在 setEditorValue 里提示 */
  })
  loadFromDraft()
})

onUnmounted(() => {
  clearTimeout(inspirationTimer)
  if (editor) {
    try {
      editor.destroy()
    } catch (e) {
      /* 忽略 */
    }
    editor = null
  }
})
</script>

<template>
  <div class="compose-page">
    <div class="card">
      <div class="card-title">
        <span>✍️</span> 创作
        <span class="dim" style="font-weight:400">（产出 C 层笔记）</span>
        <div class="spacer" style="flex:1"></div>
        <button class="btn primary" :disabled="saving" @click="saveDraftNote">
          {{ saving ? '保存中…' : '保存为创作笔记' }}
        </button>
      </div>

      <div class="row wrap" style="margin-bottom:8px">
        <span class="dim" style="font-size:11.5px">空模板：</span>
        <button
          v-for="tpl in templates"
          :key="tpl.id"
          class="btn ghost sm"
          :class="{ primary: activeTpl === tpl.id }"
          @click="applyTemplate(tpl)"
        >
          {{ tpl.name }}
        </button>
      </div>

      <input
        v-model="draftTitle"
        type="text"
        placeholder="原创稿标题"
        style="margin:8px 0"
      />

      <div ref="editorEl" class="vditor-host"></div>

      <div class="row wrap" style="margin-top:8px">
        <span v-for="t in draftTags" :key="t" class="check-chip checked">
          {{ t }} ✕
        </span>
        <input
          type="text"
          placeholder="加标签（回车）"
          style="width:180px"
          @keydown.enter.prevent="addDraftTag"
        />
      </div>

      <div v-if="inspiration.length" style="margin-top:10px">
        <label>你可能想引用</label>
        <div class="row wrap">
          <span
            v-for="item in inspiration"
            :key="item.note_path"
            class="check-chip"
            @click="insertCitation(item)"
          >
            {{ item.title || item.note_path }}
          </span>
        </div>
      </div>
      <div
        v-else-if="!inspirationAvailable"
        class="dim"
        style="margin-top:8px;font-size:11.5px"
      >
        引用推荐不可用（向量后端降级）
      </div>
    </div>
  </div>
</template>

<style scoped>
/* v35：原创区也**正好填满窗口**（用户：每个区最好都这样）。
   编辑器吃掉剩余高度，不再固定 460px 留一片空白。 */
.compose-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}
/* v35：外层 card 先撑开（它只有 313px 高，编辑器自然吃不满），
   再让 card 变成 flex column，编辑器才能 flex:1 吃掉剩余高度。 */
.compose-page > .card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* v35：外层 card 先撑开（它只有 313px 高，编辑器自然吃不满），
   再让 card 变成 flex column，编辑器才能 flex:1 吃掉剩余高度。 */
.compose-page > .card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.vditor-host {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  flex: 1;
  min-height: 0;
}
/* Vditor 根节点也撑满 */
.vditor-host :deep(.vditor) {
  height: 100% !important;
}
.vditor-host :deep(.vditor-content) {
  height: calc(100% - 35px);
}

/* ===== v36：把 Vditor 的暗色对齐到本站配色 =====
   用户："原创区的编辑区域白色不和背景，变深色"。
   `theme: 'dark'` 只加 `.vditor--dark` 类并覆盖了一批 CSS 变量，
   默认值是 GitHub 暗色的 #24292e / #2f363d —— 和本站 #161920 差一截，
   看着像"另一块颜色"。这里把变量重指到 --bg-panel / --bg-input。 */
.vditor-host :deep(.vditor--dark) {
  --panel-background-color: var(--bg-panel);
  --toolbar-background-color: var(--bg-elevated);
  --textarea-background-color: var(--bg-input);
  --textarea-text-color: var(--text);
  --border-color: var(--border);
  --second-color: var(--border-light);
  --toolbar-icon-color: var(--text-muted);
  --toolbar-icon-hover-color: var(--text);
  --toolbar-icon-active-color: var(--accent);
}
/* 源码面板（sv 模式左栏）底色 / 滚动条 */
.vditor-host :deep(.vditor-sv) {
  background: var(--bg-input);
}
.vditor-host :deep(.vditor-sv .vditor-reset) {
  color: var(--text);
}
/* 预览栏（右栏）底色：dark.css 只改文字色，底板要自己压深 */
.vditor-host :deep(.vditor-preview) {
  background: var(--bg-panel);
}
.vditor-host :deep(.vditor-preview .vditor-reset) {
  color: var(--text);
}
/* 工具栏分隔线在暗底上太黑，提亮一档 */
.vditor-host :deep(.vditor-toolbar) {
  border-bottom: 1px solid var(--border);
}
</style>
