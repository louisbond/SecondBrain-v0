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
 *   · 解析工作区（原文/字幕全文 + 配图）
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
async function doParse() {
  if (!rawText.value.trim()) {
    toast('请先粘贴分享文案或链接', 'warn')
    return
  }
  parsing.value = true
  try {
    parsed.value = await collectApi.parse(rawText.value)
    if (!parsed.value.count) toast('没有解析到任何链接，请检查粘贴内容', 'warn')
  } catch (e) {
    toast('解析失败：' + e.message, 'error')
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
    const msg = `已入库 ${validItems.value.length} 条，正在后台解析`
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
  <div>
    <!-- 采集在上、解析在下（不并列；状态独立成页 /status） -->
    <div class="wb-col">
      <div class="card">
          <div class="card-title">
            <span>⇪</span> 采集
            <span class="dim" style="font-weight:400;font-size:11.5px">
              经济工具拿原文 · 处理完自动送加工区做 AI 深度总结
            </span>
            <div class="spacer" style="flex:1"></div>
            <button class="btn ghost sm" @click="showQuickNote = !showQuickNote">速记</button>
          </div>

          <div v-if="showQuickNote" style="margin-bottom:10px">
            <textarea
              v-model="quickNoteText"
              rows="3"
              placeholder="随手记一段想法，直接入队列"
            ></textarea>
            <button class="btn sm" style="margin-top:8px" @click="doQuickNote">
              加入队列
            </button>
          </div>

          <textarea
            v-model="rawText"
            rows="5"
            placeholder="把分享文案整段粘进来（链接 / 标题 / 简介一起），自动识别平台"
          ></textarea>
          <div class="row" style="margin-top:8px">
            <button class="btn" :disabled="parsing" @click="doParse">
              {{ parsing ? '解析中…' : '智能解析' }}
            </button>
            <button
              class="btn primary"
              :disabled="submitting || !validItems.length"
              @click="doSubmit"
            >
              入库（{{ validItems.length }}）
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

      <!-- ========== 中：状态横条（采集与解析之间的跳转口） ========== -->
      <StatusStrip
        :items="states"
        :stats="stats"
        :per-group="3"
        @select="pickTask"
      />

      <!-- ==================== 下：解析区 ==================== -->
      <div>
        <div v-if="!current" class="card empty">
          <div class="icon">▤</div>
          <div>选一条任务开始解析</div>
        </div>

        <template v-else>
          <div class="card">
            <div class="card-title">
              <span>▤</span> 解析工作区
              <div class="spacer" style="flex:1"></div>
              <span class="badge" :class="stateClass(current.state)">
                {{ current.stage }}
              </span>
            </div>
            <div style="font-size:15px;font-weight:600;margin-bottom:4px">
              {{ current.title }}
            </div>
            <div class="dim mono" style="font-size:11.5px">
              {{ current.source_url || '速记' }} · 原文 {{ current.raw_len }} 字
            </div>

            <!-- 解析生成已归入「加工区」 -->
            <div class="alert info" style="margin-top:12px">
              要按模板生成解析结果？去
              <a href="/process">加工区 →</a>
              （那里可选模板与字数档位，生成后能直接当创作底稿）。
            </div>
          </div>

          <!-- 原文 / 字幕 / 转写 -->
          <div class="card">
            <div class="card-title">
              <span>📄</span> {{ current.asr ? '语音转写全文' : '原始字幕 / 正文' }}
              <span class="badge">{{ (content?.body || '').length }} 字</span>
              <div class="spacer" style="flex:1"></div>
              <span v-if="loadingContent" class="dim">加载中…</span>
            </div>
            <pre
              class="mono"
              style="white-space:pre-wrap;max-height:320px;overflow:auto;margin:0"
            >{{ content?.body || '（还没有原文）' }}</pre>
          </div>

          <!-- 配图 -->
          <div class="card">
            <div class="card-title">
              <span>🖼</span> 配图
              <span class="badge">{{ shots.length }}</span>
            </div>
            <div class="row wrap">
              <label class="row" style="margin:0">
                位置
                <input
                  v-model.number="shotPosition"
                  type="number"
                  min="0"
                  style="width:80px"
                />
                段
              </label>
              <input
                v-model="shotCaption"
                type="text"
                placeholder="图注（可选）"
                style="flex:1;min-width:140px"
              />
              <input
                v-model="shotUrl"
                type="text"
                placeholder="图片地址，或选文件"
                style="flex:1;min-width:160px"
              />
              <input type="file" accept="image/*" style="width:auto" @change="onPickFile" />
              <button class="btn" :disabled="shotBusy" @click="insertShot">
                {{ shotBusy ? '插入中…' : '插入配图' }}
              </button>
            </div>
            <div class="row wrap" style="margin-top:8px">
              <span
                v-for="(s, i) in shots"
                :key="i"
                class="badge"
                style="cursor:pointer"
                @click="removeShot(i)"
              >
                {{ s }} ×
              </span>
              <span v-if="!shots.length" class="dim">还没有配图</span>
            </div>
          </div>

        </template>
      </div>
    </div>
  </div>
</template>
