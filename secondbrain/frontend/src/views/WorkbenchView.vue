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
    <!-- v32：任务状态**置顶**并放大（用户：任务状态置顶，面积调大一点）。
         理由：采集区的第一件事是"看有哪些任务在跑/失败了"，
         而不是先看一个粘贴框。原来状态条夹在采集与解析之间，
         位置尴尬、还被压得很扁。 -->
    <div class="wb-col">
      <StatusStrip
        :items="states"
        :stats="stats"
        :per-group="8"
        class="strip-top"
        @select="pickTask"
      />

      <!-- 采集在上、解析在下（v35：用户说解析好像没作用，合并到采集下方） -->
      <div class="card card-compact">
          <div class="card-title">
            <span>⇪</span> 采集
            <span class="dim" style="font-weight:400;font-size:11.5px">
              粘贴即入库
            </span>
            <div class="spacer" style="flex:1"></div>
            <button class="btn ghost sm" @click="showQuickNote = !showQuickNote">速记</button>
          </div>

          <div v-if="showQuickNote" style="margin-bottom:10px">
            <textarea
              v-model="quickNoteText"
              rows="2"
              placeholder="随手记一段想法，直接入队列"
            ></textarea>
            <button class="btn sm" style="margin-top:8px" @click="doQuickNote">
              加入队列
            </button>
          </div>

          <textarea
            v-model="rawText"
            rows="2"
            placeholder="把分享文案整段粘进来（链接 / 标题 / 简介一起），自动识别平台"
          ></textarea>
          <div class="row" style="margin-top:8px">
            <!--
              v31：合并「智能解析」+「入库」为一个动作。
              解析只是预览，用户拿到的最终结果都是"提交入库"，
              分两步点等于让人多点一次、还得多想一次"我现在到哪一步了"。
              现在一键：解析 → 立即提交。
              解析结果仍然展示在下方（能看清抓到了什么、有没有重复）。
            -->
            <button
              class="btn primary"
              :disabled="parsing || submitting || !rawText.trim()"
              @click="parseAndSubmit"
            >
              {{
                parsing
                  ? '解析中…'
                  : submitting
                    ? '入库中…'
                    : '解析并入库'
              }}
            </button>
            <div class="spacer" style="flex:1"></div>
            <button
              v-if="dupCount"
              class="btn ghost sm"
              @click="removeDuplicates"
            >
              去掉重复 {{ dupCount }}
            </button>
          </div>

          <div v-if="parsed" style="margin-top:10px">
            <div class="dim" style="font-size:11.5px;margin-bottom:6px">
              识别到 {{ parsed.count }} 条{{ submitting ? '，正在入库…' : '' }}
            </div>
            <div
              v-for="(it, i) in items"
              :key="i"
              class="list-item"
              style="cursor:pointer"
            >
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
      </div>

      <!-- ==================== 下：解析（含配图） ==================== -->
      <!-- v35：解析从右栏挪到**采集下方**（用户：解析好像没作用，合并到采集下方）。
           配图也并进同一个卡片（用户：配图减少面积，和转写合并）——
           原来配图单独占一张卡，输入框还占两行，纯浪费高度。 -->
      <div class="wb-scroll">
          <div v-if="!current" class="card empty">
            <div class="icon">▤</div>
            <div>选一条任务开始解析</div>
          </div>

        <template v-else>
          <div class="card">
            <div class="card-title">
              <span>▤</span> 解析
              <div class="spacer" style="flex:1"></div>
              <span class="badge" :class="stateClass(current.state)">
                {{ current.stage }}
              </span>
              <!--
                v31：失败任务在采集区**直接重试**，不用跳去状态页。
                用户反馈「采集区解析失败后竟不支持重新解析」——
                这里原来只有状态标签，没有任何可点的动作。
              -->
              <button
                v-if="current.state === 'fail' || canRetry"
                class="btn ghost sm"
                style="margin-left:8px"
                :disabled="retrying"
                @click="retryCurrent"
              >
                {{ retrying ? '重试中…' : '重试' }}
              </button>
            </div>
            <div style="font-size:15px;font-weight:600;margin-bottom:4px">
              {{ current.title }}
            </div>
            <div class="dim mono" style="font-size:11.5px">
              {{ current.source_url || '速记' }} · 原文 {{ current.raw_len }} 字
            </div>

            <!-- 失败原因（原来只在状态页能看到，这里补上） -->
            <div v-if="current.error_detail" class="alert warn" style="margin-top:10px">
              {{ current.error_detail }}
            </div>

            <!-- 解析生成已归入「加工区」 -->
            <div class="alert info" style="margin-top:10px;font-size:12px">
              按模板生成解析稿（可选字数，≥800 字）请去
              <a href="/process">加工区</a>；这里可以直接「重试」跑一次 AI 提取要点。
            </div>
          </div>

          <!-- 原文 / 转写（v35：配图并进这张卡，不再单独占一张） -->
          <div class="card">
            <div class="card-title">
              <span>📄</span> {{ current.asr ? '语音转写全文' : '原始字幕 / 正文' }}
              <span class="badge">{{ (content?.body || '').length }} 字</span>
              <div class="spacer" style="flex:1"></div>
              <span v-if="loadingContent" class="dim">加载中…</span>
            </div>
            <pre
              class="mono"
              style="white-space:pre-wrap;max-height:200px;overflow:auto;margin:0"
            >{{ content?.body || '（还没有原文）' }}</pre>

            <!-- 配图工具栏：一行放下，不再单独占一张卡 -->
            <div class="shot-bar">
              <span class="shot-label">🖼 配图 {{ shots.length }}</span>
              <label class="row" style="margin:0;flex:0 0 auto">
                第
                <input v-model.number="shotPosition" type="number" min="0" style="width:50px" />
                段
              </label>
              <input v-model="shotCaption" type="text" placeholder="图注" class="shot-in" />
              <input v-model="shotUrl" type="text" placeholder="图片地址 / 选文件" class="shot-in" />
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
          </div>

        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 采集区：纵向三段（状态条置顶 → 采集 → 解析区） */
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

/* v32：状态条置顶后要更显眼（用户：面积调大一点）。
   原来夹在中间被压得很扁，现在给它足够的呼吸空间。 */
.strip-top {
  flex: 0 0 auto;
  padding-bottom: 4px;
}

/* 紧凑卡片：采集卡只占必要高度（用户要求面积减小）。
   卡片内边距 16 → 10，textarea 由 rows=5 降到 rows=2。 */
.card-compact {
  padding: 10px 14px;
  margin-bottom: 0;
  flex: 0 0 auto;
}
.card-compact :deep(.card-title) {
  margin-bottom: 8px;
}
.card-compact textarea {
  min-height: 46px;
}

/* 解析区：吃掉剩余高度，内部滚动。 */
.wb-scroll {
  flex: 1;
  min-height: 120px;
  overflow-y: auto;
  padding-right: 6px;
}

/* 配图工具栏（v35：并进转写卡，压成一行）
   原来配图单独一张卡 + 输入框换两行，白占 ~130px。 */
.shot-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}
.shot-label {
  font-size: 12px;
  color: var(--text-dim);
  flex: 0 0 auto;
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
