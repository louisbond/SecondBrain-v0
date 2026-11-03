<script setup>
/**
 * 原创区：写你自己的东西（与加工区并列的一级入口）。
 *
 * - 空数据模板：选一个模板生成骨架（带建议分区/标签），填详情即可归档
 * - 加工区「送去原创区」的解析稿会自动载入（sessionStorage）
 * - 产出 C 层笔记
 */
import { ref, computed, onMounted, onUnmounted, inject, watch } from 'vue'
import { notesApi } from '../api/client'
import { loadDraft, clearDraft } from '../utils/draft'
// **必须引入 Vditor 的样式**（v35 修）。
// 坑：从接入 Vditor 起就一直漏了这句。Javascript 能跑、DOM 能建、
// setValue 也能写进去（所以测试一直"通过"），但没有 CSS 的编辑器
// 是**裸 DOM** —— 工具栏图标按原始尺寸撑成巨图，用户看到的是
// 一张笑脸 + 超大的 B / T 图标，完全没法编辑。
// 教训：DOM 断言（editable=True / toolbar=True）证明不了"能用"，
// 视觉类组件必须截图看。
import 'vditor/dist/index.css'
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

// 空数据模板（v35：换成**兵哥库「我的知识库」的 B 层标准格式**）
//
// 依据用户桌面 `兵哥知识库管理系统309/我的知识库/规则模板/` 的实际规范：
//   知识提炼/   B层，文件名「一句话总结_YYYYMMDDHHMMSS.md」
//              字段：type / 分类 / 原始链接 / 作者
//              正文：# 一句话总结 → 关键词 → 摘要(≤120字) → 详细内容(≥600字) → 双向链接
//   知识聚合/   C层，按专题，人工写，用 [[B层笔记]] 引用
//   日记/       Daily Note
//   规则模板/   D层，流程与提示词
//
// 约定：
//   · **不写 YAML frontmatter** —— 后端 create_note 会自己生成
//     （title/type/tags/partitions/created），正文再写一段 --- 会变成双重
//     frontmatter，Obsidian 打开就不认了。所以这里的「字段」用列表写。
//   · 占位符 `{{date}}` `{{time}}` `{{title}}`，应用模板时替换（见 fillPlaceholders）。
//   · `[[双链]]` 会被后端抽成 citations。
const templates = [
  {
    id: 'distill',
    name: '知识提炼',
    desc: 'B层标准格式：一句话总结 + 关键词 + 摘要 + ≥600字详细内容',
    sections: `# {{title}}

- 分类：
- 原始链接：
- 作者：

**关键词**：

**摘要**：

## 详细内容


## 逻辑树

- 主论点一
  - 支撑论据
- 主论点二

## 双向链接
- [[ ]]
`,
    suggest_tags: ['提炼'],
    suggest_partition: ''
  },
  {
    id: 'aggregate',
    name: '知识聚合',
    desc: 'C层：按专题整合多篇提炼笔记，用它自己的话写',
    sections: `# {{title}}

- 专题：
- 更新：{{date}}

## 这个专题在讲什么


## 相关提炼笔记
- [[ ]]

## 我的整合


## 结论 / 可复用的东西

`,
    suggest_tags: ['聚合'],
    suggest_partition: ''
  },
  {
    id: 'daily',
    name: '日记',
    desc: 'Daily Note：当日要做的 + 记录 + 随想',
    sections: `# {{date}}

## 今天要做的
- [ ] 

## 记录


## 随想


## 今天关联到
- [[ ]]
`,
    suggest_tags: ['日记'],
    suggest_partition: ''
  },
  {
    id: 'weekly',
    name: '周回顾',
    desc: '一周复盘：做成什么、卡在哪、下周重点',
    sections: `# {{date}} 周回顾

## 本周做成的事


## 卡住的地方


## 学到的东西


## 下周最重要的一件事
- [ ] 

## 本周笔记
- [[ ]]
`,
    suggest_tags: ['复盘'],
    suggest_partition: ''
  },
  {
    id: 'template-note',
    name: '规则模板',
    desc: 'D层：流程规范 / 提示词模板 / 操作手册',
    sections: `# {{title}}

- 适用场景：
- 更新：{{date}}

## 规则 / 步骤

1. 
2. 

## 注意事项

`,
    suggest_tags: ['模板'],
    suggest_partition: ''
  }
]

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
      placeholder: '写下你的思考。解析结果、引用建议都可以一键插入。',
      mode: 'sv',
      cache: { enable: false },
      cdn: VEDITOR_CDN,
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
</style>
