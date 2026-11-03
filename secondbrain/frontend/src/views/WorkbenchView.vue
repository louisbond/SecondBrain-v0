<script setup>
/**
 * 采集区：收集素材 + 图文加工存储。
 *
 * 定位：这里的处理用**经济的工具**（本地解析/便宜模型），只做"拿到原文"这一步；
 * 原文到手 → 自动送入加工区做 AI 深度总结（豆包/DeepSeek 级深度思考）。
 *
 * 纵向三段：
 *   · 采集卡（粘贴分享文案 / 速记）
 *   · 状态横条（跳转口，「全部 →」进 /status 完整页）
 *   · 解析（原文/字幕全文 + 配图），与采集**并列**（v33）
 *
 * 模板化解析（选模板 + 字数档位 + AI 生成）在**加工区**。
 */
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import {
  collectApi,
  itemsApi,
  workbenchApi,
  uploadApi
} from '../api/client'
import StatusStrip from '../components/StatusStrip.vue'

const toast = inject('toast')

// ---------------- 采集输入 ----------------
const rawText = ref('')
const parsed = ref(null)
const parsing = ref(false)
const submitting = ref(false)
const quickNoteText = ref('')
const showQuickNote = ref(false)

// ---------------- 状态 ----------------
const states = ref([])
const stats = ref({})
const loading = ref(false)
const current = ref(null)
const content = ref(null)
const loadingContent = ref(false)
const retrying = ref(false)

// ---------------- 解析 ----------------

// ---------------- 配图 ----------------
const shotPosition = ref(0)
const shotCaption = ref('')
const shotFile = ref(null)
const shotUrl = ref('')
const shotBusy = ref(false)
const shots = ref([])

const platformMeta = {
  bilibili: { label: 'B站', color: 'var(--bilibili)' },
  douyin: { label: '抖音', color: 'var(--douyin)' },
  kuaishou: { label: '快手', color: 'var(--kuaishou)' },
  xiaohongshu: { label: '小红书', color: 'var(--xiaohongshu)' },
  web: { label: '网页', color: 'var(--web)' },
  quick_note: { label: '速记', color: 'var(--ok)' }
}
const platformOf = (p) => platformMeta[p] || { label: p, color: 'var(--web)' }

const stateClass = (s) =>
  ({ run: 'accent', done: 'ok', fail: 'danger', wait: '' }[s] || '')

const items = computed(() => parsed.value?.items || [])
// 可入库 = 有链接 + 没重复。
// 「只有标题没链接」的条目（比如粘贴文本里的说明行）不能提交——
// 提交了后端会 422，前端还只能显示 [object Object]。
const validItems = computed(() =>
  items.value.filter((i) => i.url && !i.existing)
)
const dupCount = computed(
  () => items.value.filter((i) => i.existing).length
)
const noUrlCount = computed(
  () => items.value.filter((i) => !i.url).length
)

// ---------------- 数据加载 ----------------
async function loadStatus() {
  loading.value = true
  try {
    const data = await workbenchApi.status(50)
    states.value = data.items || []
    stats.value = data.stats || {}
    if (!current.value && states.value.length) {
      await selectItem(states.value[0].id)
    }
  } catch (e) {
    toast('加载状态失败：' + e.message, 'error')
  } finally {
    loading.value = false
  }
}

/** 状态条上点条目 → 选中该任务（下方解析区联动） */
async function pickTask(item) {
  if (!item || item.id === current.value?.id) return
  await selectItem(item.id)
}

/** 当前任务能不能重试。 */
const canRetry = computed(() => {
  const s = current.value?.status
  return s === 'extract_failed' || s === 'ai_failed' || s === 'raw_ready'
})

/**
 * 转写正文按行切分（v36：用户要求「转写区要有行标」）。
 *
 * 为什么不用一个 <pre> 里塞全文、旁边画一条行号列：
 * `white-space: pre-wrap` 下长行会折行，行号就与实际视觉行错位了。
 * 每行一个 flex 行（行号 + 文本）能保证**行号永远对着逻辑行**，
 * 折行只是这一行的第二视觉行，符合代码编辑器的常规手感。
 */
const bodyLines = computed(() => {
  const body = content.value?.body
  if (!body) return ['（还没有原文）']
  return String(body).replace(/\r\n/g, '\n').split('\n')
})

const bodyCharCount = computed(() => (content.value?.body || '').length)

/**
 * 重试当前任务（v31：采集区直接重试，不用跳去状态页）。
 *
 * 后端 /api/items/{id}/retry 会按状态决定重跑哪一段：
 *   extract_failed → 重跑抓取
 *   ai_failed       → 重跑 AI
 *   raw_ready       → 重跑 AI（原文就绪，等你点 AI 提取要点）
 */
async function retryCurrent() {
  if (!current.value || retrying.value) return
  retrying.value = true
  try {
    await itemsApi.retry(current.value.id)
    toast('已重新提交，稍等片刻…', 'success')
    // 状态条是轮询的，等一轮让它自己刷新
    await new Promise((r) => setTimeout(r, 1500))
    await loadStatus()
    const fresh = states.value.find((s) => s.id === current.value.id)
    if (fresh) current.value = fresh
  } catch (e) {
    toast('重试失败：' + e.message, 'error')
  } finally {
    retrying.value = false
  }
}

async function selectItem(id) {
  current.value = states.value.find((s) => s.id === id) || null
  loadingContent.value = true
  try {
    const data = await itemsApi.content(id)
    content.value = data
    shots.value = (data.frontmatter?.screenshots || []).slice()
    shotPosition.value = 0
  } catch (e) {
    content.value = null
    toast('读取原文失败：' + e.message, 'error')
  } finally {
    loadingContent.value = false
  }
}

// ---------------- 采集 ----------------

/** 只解析，不提交（保留给「先看看抓到了什么」的场合）。 */
async function doParse() {
  if (!rawText.value.trim()) {
    toast('请先粘贴分享文案或链接', 'warn')
    return false
  }
  parsing.value = true
  try {
    parsed.value = await collectApi.parse(rawText.value)
    if (!parsed.value.count) {
      toast('没有解析到任何链接，请检查粘贴内容', 'warn')
      return false
    }
    return true
  } catch (e) {
    toast('解析失败：' + e.message, 'error')
    return false
  } finally {
    parsing.value = false
  }
}

function removeItem(index) {
  parsed.value.items.splice(index, 1)
}

function removeDuplicates() {
  parsed.value.items = validItems.value.slice()
}

async function doSubmit() {
  if (!validItems.value.length) {
    toast(
      noUrlCount.value
        ? `没有可入库的条目（${noUrlCount.value} 条只有文字没有链接）`
        : '没有可入库的条目',
      'warn'
    )
    return
  }
  submitting.value = true
  try {
    const res = await collectApi.submit({
      entries: validItems.value.map((i) => ({
        url: i.url,
        platform: i.platform,
        title: i.title
      }))
    })
    const skipped = (res && res.skipped) || []
    const msg = `已入库 ${validItems.value.length} 条，正在后台抓取原文`
    toast(
      skipped.length ? `${msg}（跳过 ${skipped.length} 条无链接）` : msg,
      'success'
    )
    rawText.value = ''
    parsed.value = null
    await loadStatus()
  } catch (e) {
    toast('入库失败：' + e.message, 'error')
  } finally {
    submitting.value = false
  }
}

/**
 * 一键「解析并入库」（v31：合并原来的两步）。
 *
 * 流程：解析 → 立刻提交。
 * 解析失败就停在这一步，textarea 内容保留，用户改完再点一次即可 ——
 * 不需要"先点解析、再点入库"两个来回。
 */
async function parseAndSubmit() {
  if (submitting.value) return
  const ok = await doParse()
  if (!ok) return
  await doSubmit()
}

async function doQuickNote() {
  const text = quickNoteText.value.trim()
  if (!text) {
    toast('速记内容不能为空', 'warn')
    return
  }
  try {
    await collectApi.quickNote(text)
    toast('速记已加入队列', 'success')
    quickNoteText.value = ''
    showQuickNote.value = false
    await loadStatus()
  } catch (e) {
    toast('速记失败：' + e.message, 'error')
  }
}


// ---------------- 配图 ----------------
function onPickFile(e) {
  shotFile.value = e.target.files?.[0] || null
  if (!shotFile.value) shotUrl.value = ''
}

async function insertShot() {
  if (!current.value) return
  let ref = shotUrl.value.trim()
  if (shotFile.value) {
    shotBusy.value = true
    try {
      const up = await uploadApi.screenshot(shotFile.value)
      ref = up.relative || up.path
    } catch (e) {
      toast('图片上传失败：' + e.message, 'error')
      shotBusy.value = false
      return
    }
  }
  if (!ref) {
    toast('请选择图片文件或填写图片地址', 'warn')
    shotBusy.value = false
    return
  }
  shotBusy.value = true
  try {
    const r = await workbenchApi.insertScreenshot(current.value.id, {
      position: shotPosition.value,
      image_ref: ref,
      caption: shotCaption.value
    })
    shots.value = r.screenshots || []
    shotCaption.value = ''
    shotFile.value = null
    shotUrl.value = ''
    toast(`配图已插入第 ${r.position + 1} 段前`, 'success')
    await selectItem(current.value.id)
  } catch (e) {
    toast('插入配图失败：' + e.message, 'error')
  } finally {
    shotBusy.value = false
  }
}

async function removeShot(index) {
  if (!current.value) return
  try {
    const r = await workbenchApi.removeScreenshot(current.value.id, index)
    shots.value = r.screenshots || []
    toast('配图已移除', 'success')
    await selectItem(current.value.id)
  } catch (e) {
    toast('移除失败：' + e.message, 'error')
  }
}

// ---------------- 生命周期 ----------------
let timer = null
onMounted(async () => {
  await loadStatus()
  timer = setInterval(loadStatus, 3000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="wb-page">
    <!-- 任务状态置顶（v32：用户要求置顶并放大；v35 改成四行纵向） -->
    <div class="wb-col">
      <StatusStrip
        :items="states"
        :stats="stats"
        :per-group="8"
        class="strip-top"
        @select="pickTask"
      />

      <!--
        v36：采集与解析**真正合成一张卡**。

        用户："采集和解析没有合并啊，合并后不要太占空间"。
        v35 只是把解析从右栏挪到采集下方，仍是两张独立的卡 ——
        视觉上还是两个模块、两条边框、两份内边距。现在：
        一个 .card = 采集段 + 虚线分隔 + 解析段，整卡内部滚动。
      -->
      <div class="wb-scroll">
        <div class="card wb-merge" :class="{ 'wb-merge--empty': !current }">
          <!-- ==================== 采集 ==================== -->
          <div class="mb-head">
            <span class="mb-icon">⇪</span>
            <span class="mb-title">采集</span>
            <span class="dim mb-hint">粘贴即入库</span>
            <div class="spacer" style="flex:1"></div>
            <button class="btn ghost sm" @click="showQuickNote = !showQuickNote">
              速记
            </button>
          </div>

          <div v-if="showQuickNote" class="mb-quicknote">
            <textarea
              v-model="quickNoteText"
              rows="2"
              placeholder="随手记一段想法，直接入队列"
            ></textarea>
            <button class="btn sm" @click="doQuickNote">加入队列</button>
          </div>

          <textarea
            v-model="rawText"
            rows="2"
            placeholder="把分享文案整段粘进来（链接 / 标题 / 简介一起），自动识别平台"
          ></textarea>

          <div class="row mb-actions">
            <button
              class="btn primary"
              :disabled="parsing || submitting || !rawText.trim()"
              @click="parseAndSubmit"
            >
              {{ parsing ? '解析中…' : submitting ? '入库中…' : '解析并入库' }}
            </button>
            <span v-if="parsed" class="dim mb-count">
              识别到 {{ parsed.count }} 条{{ submitting ? '，正在入库…' : '' }}
            </span>
            <div class="spacer" style="flex:1"></div>
            <button v-if="dupCount" class="btn ghost sm" @click="removeDuplicates">
              去掉重复 {{ dupCount }}
            </button>
          </div>

          <div v-if="parsed && items.length" class="mb-parsed">
            <div v-for="(it, i) in items" :key="i" class="list-item mb-parsed-row">
              <span
                class="platform-dot"
                :style="{ background: platformOf(it.platform).color }"
              ></span>
              <div class="grow">
                <div class="title">{{ it.title || it.url }}</div>
                <div class="sub">{{ it.platform }} · {{ it.url }}</div>
              </div>
              <span v-if="it.existing" class="badge warn">已存在</span>
              <button class="btn ghost sm" @click.stop="removeItem(i)">×</button>
            </div>
          </div>

          <!-- ==================== 解析（同一张卡） ==================== -->
          <div class="mb-split"></div>

          <div v-if="!current" class="mb-empty dim">
            ▤ 解析 — 选一条任务开始（点上面的任务，或去
            <a href="/status">状态页</a>）
          </div>

          <template v-else>
            <div class="mb-head">
              <span class="mb-icon">▤</span>
              <span class="mb-title">解析</span>
              <span class="badge" :class="stateClass(current.state)">
                {{ current.stage }}
              </span>
              <div class="spacer" style="flex:1"></div>
              <span v-if="loadingContent" class="dim mb-hint">加载中…</span>
              <button
                v-if="current.state === 'fail' || canRetry"
                class="btn ghost sm"
                :disabled="retrying"
                @click="retryCurrent"
              >
                {{ retrying ? '重试中…' : '重试' }}
              </button>
            </div>

            <div class="mb-cur">
              <span class="mb-cur-title">{{ current.title }}</span>
              <span class="dim mono mb-cur-meta">
                {{ current.source_url || '速记' }} · 原文 {{ current.raw_len }} 字
              </span>
            </div>

            <div v-if="current.error_detail" class="alert warn mb-err">
              {{ current.error_detail }}
            </div>

            <div class="mb-body-label">
              <span>{{ current.asr ? '📄 语音转写' : '📄 原文' }}</span>
              <span class="badge">{{ bodyLines.length }} 行</span>
              <span class="badge">{{ bodyCharCount }} 字</span>
              <div class="spacer" style="flex:1"></div>
              <a class="mb-goto" href="/process">按模板生成 → 加工区</a>
            </div>

            <!-- 转写正文：**带行号**（v36 用户要求） -->
            <div class="code-box">
              <div v-for="(ln, i) in bodyLines" :key="i" class="code-line">
                <span class="code-ln">{{ i + 1 }}</span>
                <span class="code-lt">{{ ln }}</span>
              </div>
            </div>

            <!-- 配图工具栏（v35 并进同一张卡；v36 保留一行） -->
            <div class="shot-bar">
              <span class="shot-label">🖼 配图 {{ shots.length }}</span>
              <label class="row shot-pos">
                第
                <input v-model.number="shotPosition" type="number" min="0" />
                段
              </label>
              <input v-model="shotCaption" type="text" placeholder="图注" class="shot-in" />
              <input
                v-model="shotUrl"
                type="text"
                placeholder="图片地址 / 选文件"
                class="shot-in"
              />
              <input type="file" accept="image/*" class="shot-file" @change="onPickFile" />
              <button class="btn sm" :disabled="shotBusy" @click="insertShot">
                {{ shotBusy ? '插入中…' : '插入' }}
              </button>
            </div>
            <div v-if="shots.length" class="shot-chips">
              <span
                v-for="(s, i) in shots"
                :key="i"
                class="badge"
                style="cursor:pointer"
                @click="removeShot(i)"
              >
                {{ s }} ×
              </span>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 采集区：状态条置顶 + 下方一张合并卡（采集 + 解析） */
.wb-page {
  height: 100%;
  min-height: 0;
}
.wb-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}

/* 状态条置顶后要更显眼（用户：面积调大一点） */
.strip-top {
  flex: 0 0 auto;
  padding-bottom: 4px;
}

/* 合并卡所在的可滚区域 */
.wb-scroll {
  flex: 1;
  min-height: 120px;
  /* v36：改成 flex 列 + 卡片吃满剩余高度 —— 这样「状态条 + 合并卡」
     正好一屏，不用滚一下才看到配图行（用户：合并后不要太占空间）。
     用 flex 而不是写死 max-height：窗口高度变了也不会有残留滚动条。 */
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding-right: 6px;
}

/* ===== 合并卡（v36）：采集段 + 解析段共用一张卡 =====
   省掉第二张卡的边框 + 内边距 + 标题行，约省 80~100px 竖向空间。 */
.wb-merge {
  padding: 10px 14px 12px;
  margin-bottom: 0;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
/* 没选任务：卡片只占内容高，不撑满屏幕（否则一张大空卡） */
.wb-merge--empty {
  flex: 0 0 auto;
  overflow: visible;
}
.wb-merge textarea {
  min-height: 46px;
}

.mb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  min-height: 24px;
}
.mb-icon {
  font-size: 13px;
  color: var(--text-dim);
}
.mb-title {
  font-size: 13.5px;
  font-weight: 600;
}
.mb-hint {
  font-size: 11.5px;
  font-weight: 400;
}
.mb-actions {
  margin-top: 8px;
  align-items: center;
}
.mb-count {
  font-size: 11.5px;
  margin-left: 2px;
}
.mb-quicknote {
  margin-bottom: 8px;
}
.mb-quicknote .btn {
  margin-top: 6px;
}

/* 采集预览列表：限高内滚，避免把解析段挤下去 */
.mb-parsed {
  margin-top: 8px;
  max-height: 140px;
  overflow-y: auto;
}
.mb-parsed-row {
  padding: 4px 2px;
}

/* 采集段与解析段之间的分隔线（不新增卡片） */
.mb-split {
  margin: 12px -14px 10px;
  border-top: 1px dashed var(--line);
}

.mb-empty {
  font-size: 12.5px;
  padding: 14px 2px 6px;
}

/* 解析段的当前任务 */
.mb-cur {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.mb-cur-title {
  font-size: 14.5px;
  font-weight: 600;
}
.mb-cur-meta {
  font-size: 11.5px;
}
.mb-err {
  margin-bottom: 8px;
  font-size: 12px;
}
.mb-body-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
  margin: 10px 0 6px;
}
.mb-goto {
  font-size: 11.5px;
  font-weight: 400;
}

/* ===== 转写正文 + 行号（v36） =====
   行号固定宽右对齐，正文 flex:1 自动换行。
   长内容**卡内滚动**（max-height 38vh），不把整页推到几屏高。 */
.code-box {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-soft, rgba(255, 255, 255, 0.02));
  /* v36：**吃掉卡片剩余高度**（不是写死 max-height）。
     上面给 .wb-merge 设了 flex 列，这里 flex:1 就能自动适配窗口高度：
     高一点的屏幕看到更多行，矮一点的屏幕少几行但整页不滚。
     min-height 保证再矮也至少能看 6 行。 */
  flex: 1 1 auto;
  min-height: 120px;
  overflow: auto;
  padding: 6px 0;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12.5px;
  line-height: 1.7;
}
.code-line {
  display: flex;
  gap: 10px;
  padding: 0 10px 0 0;
}
.code-line:hover {
  background: var(--bg-hover);
}
.code-ln {
  flex: 0 0 38px;
  text-align: right;
  color: var(--text-dim);
  opacity: 0.55;
  user-select: none;
  font-variant-numeric: tabular-nums;
}
.code-lt {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text);
}
/* 空行也要占一行高度 */
.code-lt:empty::before {
  content: '\00a0';
}

/* 配图工具栏：一行放下 */
.shot-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
  flex: 0 0 auto;
}
.shot-label {
  font-size: 12px;
  color: var(--text-dim);
  flex: 0 0 auto;
}
.shot-pos {
  margin: 0;
  flex: 0 0 auto;
  font-size: 12px;
}
.shot-pos input {
  width: 46px;
}
.shot-in {
  flex: 1 1 130px;
  min-width: 0;
}
.shot-file {
  flex: 0 1 150px;
  min-width: 0;
  font-size: 11px;
}
.shot-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
</style>
