<script setup>
/**
 * 任务状态页：采集/加工的所有任务在这条流水线上的位置与结果。
 *
 * 从采集区、加工区跳转过来。每条任务显示：阶段、进度、耗时、
 * 失败原因（可读文本）+ 重试按钮。
 */
import { ref, computed, onMounted, onUnmounted, inject, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { workbenchApi, itemsApi, noteRoute } from '../api/client'
import AudioTranscript from '../components/AudioTranscript.vue'
import { useItemActions } from '../composables/useItemActions'

const toast = inject('toast')
const router = useRouter()
const route = useRoute()

const states = ref([])
const stats = ref({})
const loading = ref(false)

// v33：从采集区的状态条点某个状态跳过来时带 ?filter=done，
// 落地就该是那一类，不用再手动点一次筛选。
const VALID_FILTERS = ['all', 'run', 'wait', 'done', 'fail']
const filter = ref(
  VALID_FILTERS.includes(String(route.query.filter || '')) ? String(route.query.filter) : 'all'
)

// 停留在本页时再点别的状态（例如从侧边栏）也要跟着变
watch(
  () => route.query.filter,
  (v) => {
    const f = String(v || '')
    if (VALID_FILTERS.includes(f)) filter.value = f
  }
)

const filtered = computed(() => {
  if (filter.value === 'all') return states.value
  return states.value.filter((i) => i.state === filter.value)
})

let timer = null

const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'run', label: '进行中' },
  { id: 'wait', label: '等待' },
  { id: 'done', label: '已完成' },
  { id: 'fail', label: '失败' }
]

const stateClass = (s) => ({ run: 'accent', done: 'ok', fail: 'danger', wait: '' })[s] || ''

async function load() {
  loading.value = true
  try {
    const data = await workbenchApi.status(100)
    states.value = data.items || []
    stats.value = data.stats || {}
  } catch (e) {
    toast('读取任务状态失败：' + e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function retry(id) {
  try {
    await itemsApi.retry(id)
    toast('已重新提交', 'success')
    await load()
  } catch (e) {
    toast('重试失败：' + e.message, 'error')
  }
}

// ---- 行内编辑 / 删除（逻辑拆到 useItemActions，避免本文件超 500 行）----
const { editing, editTitle, editSummary, editBusy, removing, openEdit, saveEdit, removeItem } =
  useItemActions(toast, load)

const filling = ref(null)
const fillText = ref('')
const fillBusy = ref(false)

// v0.7.9：本地视频 → 口播台词。不依赖解析服务/分享链接/Cookie，最可靠的一条路。
const fileInput = ref(null)
const uploadTarget = ref(null)
const uploading = ref(false)

// v0.7.9：口播台词靠「上传视频 → 本机转写」。
// 只要有还没有 ASR 结果的视频类条目就显示入口 —— 不只是失败条目：
// 抖音/快手本地适配器「成功」时拿到的也常只是标题+简介，同样需要转写。
// 注意不能拿 item.state 判断「在跑」：那是 STAGE_MAP 的配色（extracted 也是 run），
// 真在跑要看 job.state。
const VIDEO_PLATFORMS = new Set(['douyin', 'kuaishou', 'xiaohongshu', 'bilibili'])
function canTranscribe(item) {
  const busy = item.job && item.job.state === 'running'
  return !busy && !item.asr && VIDEO_PLATFORMS.has(item.platform)
}

// v0.8：听音频核对。文本丢了语气/停顿/重音，要确认「他到底是什么意思」
// 只能听原声。展开后在行内嵌播放器 + 带时间码的分段。
const listening = ref(null)
function toggleListen(id) {
  listening.value = listening.value === id ? null : id
}

function pickMedia(id) {
  uploadTarget.value = id
  fileInput.value?.click()
}

async function onMediaChosen(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  const id = uploadTarget.value
  if (!file || !id) return
  uploading.value = true
  try {
    const r = await itemsApi.uploadMedia(id, file)
    toast(r.message || '已排队转写', 'success', 6000)
    await load()
  } catch (err) {
    toast('上传失败：' + err.message, 'error')
  } finally {
    uploading.value = false
    uploadTarget.value = null
  }
}

function openFill(item) {
  filling.value = item.id
  fillText.value = ''
}

async function submitFill(id) {
  if (!fillText.value.trim()) {
    toast('先粘贴文案', 'warn')
    return
  }
  fillBusy.value = true
  try {
    await itemsApi.manualFill(id, fillText.value.trim())
    toast('已补充文案并重新入库', 'success')
    filling.value = null
    fillText.value = ''
    await load()
  } catch (e) {
    toast('补充失败：' + e.message, 'error')
  } finally {
    fillBusy.value = false
  }
}

/** 打开这条任务的 A/B 层原文。
 *
 * 用路由跳转而不是 window.location.href——后者会整页重载，
 * 丢掉 SPA 状态，还会让页面重新挂载一遍所有视图。 */
function openItem(item) {
  if (!item.inbox_path && !item.processed_path) return
  router.push(noteRoute(item.processed_path || item.inbox_path))
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onUnmounted(() => {
  clearInterval(timer)
})
</script>

<template>
  <div class="status-page">
    <!-- 统计条 -->
    <div class="card">
      <div class="stat-row">
        <div class="stat">
          <div class="num">{{ stats.total || 0 }}</div>
          <div class="dim">全部</div>
        </div>
        <div class="stat">
          <div class="num accent">{{ stats.running || 0 }}</div>
          <div class="dim">进行中</div>
        </div>
        <div class="stat">
          <div class="num">{{ stats.wait || 0 }}</div>
          <div class="dim">等待</div>
        </div>
        <div class="stat">
          <div class="num ok">{{ stats.done || 0 }}</div>
          <div class="dim">已完成</div>
        </div>
        <div class="stat">
          <div class="num danger">{{ stats.fail || 0 }}</div>
          <div class="dim">失败</div>
        </div>
        <div class="spacer" style="flex: 1"></div>
        <div class="dim mono" style="align-self: center">
          <!-- v31：进度只统计**未完成**的条目（后端已改）。
               全都完成了显示「已全部完成」，比一个孤零零的 100% 清楚。 -->
          {{
            stats.pending
              ? `进行中进度 ${Math.round((stats.avg_progress || 0) * 100)}%`
              : stats.total
                ? '已全部完成'
                : '暂无任务'
          }}
        </div>
        <button class="btn ghost sm" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- 筛选 + 列表 -->
    <div class="card">
      <div class="tabs" style="margin-bottom: 10px">
        <button
          v-for="f in FILTERS"
          :key="f.id"
          class="tab"
          :class="{ active: filter === f.id }"
          @click="filter = f.id"
        >
          {{ f.label }}
        </button>
      </div>

      <template v-for="item in filtered" :key="item.id">
        <div class="task-item" @click="openItem(item)">
          <div class="grow">
            <div class="title">
              {{ item.title || '未命名' }}
              <span class="badge" :class="stateClass(item.state)" style="margin-left: 6px">
                {{ item.stage || item.status }}
              </span>
            </div>
            <div v-if="item.error_detail" class="sub danger-text">
              {{ item.error_detail }}
            </div>
            <div class="sub dim mono" style="font-size: 11px">
              {{ item.platform_label || item.platform }} ·
              {{ (item.source_url || '').slice(0, 60) }}
            </div>
            <div class="progress-track" style="margin-top: 6px">
              <div
                class="progress-bar"
                :class="{ fail: item.state === 'fail' }"
                :style="{ width: Math.round((item.progress || 0) * 100) + '%' }"
              ></div>
            </div>
            <!-- 进度条旁边的人话：光有百分比时，卡在 45% 两分钟完全不知道在干嘛 -->
            <div v-if="item.stage_text" class="sub dim stage-text">
              {{ item.stage_text }}
            </div>
          </div>
          <div class="row wrap" style="align-self: center; gap: 6px">
            <button v-if="item.has_audio" class="btn ghost sm" @click.stop="toggleListen(item.id)">
              {{ listening === item.id ? '收起' : '听音频' }}
            </button>
            <button v-if="item.state === 'fail'" class="btn ghost sm" @click.stop="openFill(item)">
              粘贴文案
            </button>
            <button
              v-if="canTranscribe(item)"
              class="btn ghost sm"
              :disabled="uploading"
              @click.stop="pickMedia(item.id)"
            >
              {{
                uploading && uploadTarget === item.id
                  ? '上传中…'
                  : item.asr
                    ? '重新转写'
                    : '转写视频'
              }}
            </button>
            <button v-if="item.state === 'fail'" class="btn ghost sm" @click.stop="retry(item.id)">
              重试
            </button>
            <button class="btn ghost sm" @click.stop="openEdit(item)">
              {{ editing === item.id ? '收起' : '编辑' }}
            </button>
            <button
              class="btn ghost sm danger-text"
              :disabled="removing === item.id"
              @click.stop="removeItem(item)"
            >
              {{ removing === item.id ? '删除中…' : '删除' }}
            </button>
          </div>
        </div>

        <div v-if="editing === item.id" class="edit-box" @click.stop>
          <label class="dim">标题</label>
          <input v-model="editTitle" type="text" placeholder="标题" />
          <label class="dim" style="margin-top: 8px">简介</label>
          <textarea v-model="editSummary" rows="3" placeholder="一句话简介 / AI 摘要"></textarea>
          <div class="row" style="margin-top: 8px">
            <button class="btn primary sm" :disabled="editBusy" @click.stop="saveEdit(item.id)">
              {{ editBusy ? '保存中…' : '保存' }}
            </button>
            <button class="btn ghost sm" @click.stop="editing = null">取消</button>
          </div>
        </div>

        <div v-if="listening === item.id" class="listen-box" @click.stop>
          <AudioTranscript
            :item-id="item.id"
            :has-audio="item.has_audio"
            :duration-ms="item.duration_ms || 0"
          />
        </div>

        <div v-if="item.state === 'fail' && filling === item.id" class="fill-box" @click.stop>
          <textarea v-model="fillText" rows="4" placeholder="粘贴作品文案 / 正文…"></textarea>
          <div class="row wrap" style="margin-top: 8px">
            <button class="btn" :disabled="fillBusy" @click.stop="submitFill(item.id)">
              {{ fillBusy ? '保存中…' : '保存并入库' }}
            </button>
            <button class="btn ghost sm" @click.stop="filling = null">取消</button>
          </div>
        </div>
      </template>

      <input
        ref="fileInput"
        type="file"
        accept="video/*,audio/*,.mp4,.m4a,.mp3,.webm,.mov,.wav,.flv,.mkv,.aac"
        style="display: none"
        @change="onMediaChosen"
      />

      <div v-if="!filtered.length" class="empty" style="padding: 24px 8px">
        <div class="icon">≡</div>
        <div>{{ filter === 'all' ? '还没有任务' : '这一类暂时没有' }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.stat-row {
  display: flex;
  gap: 26px;
  align-items: stretch;
}
.stat .num {
  font-size: 22px;
  font-weight: 650;
}
.stat .num.accent {
  color: var(--accent);
}
.stat .num.ok {
  color: var(--ok, #35c48e);
}
.stat .num.danger {
  color: var(--danger, #e5484d);
}
.task-item {
  display: flex;
  gap: 10px;
  padding: 9px 6px;
  border-top: 1px solid var(--line);
  cursor: pointer;
}
.task-item:first-of-type {
  border-top: none;
}
.task-item:hover {
  background: var(--bg-hover);
  border-radius: 8px;
}
.danger-text {
  color: var(--danger, #e5484d);
}
.progress-track {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-hover);
  overflow: hidden;
  max-width: 320px;
}
.progress-bar {
  height: 100%;
  background: var(--accent);
  border-radius: 2px;
  transition: width 0.4s;
}
.progress-bar.fail {
  background: var(--danger, #e5484d);
}
.fill-box {
  padding: 10px 12px 12px;
  margin: 0 0 8px;
  border-radius: 8px;
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}
.fill-box textarea {
  width: 100%;
  box-sizing: border-box;
  line-height: 1.6;
}

.edit-box {
  padding: 10px 12px 12px;
  margin: 0 0 8px;
  border-radius: 8px;
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}
.edit-box label {
  display: block;
  font-size: 11.5px;
  margin-bottom: 4px;
}
.edit-box input,
.edit-box textarea {
  width: 100%;
  box-sizing: border-box;
  line-height: 1.6;
}
.edit-box textarea {
  resize: vertical;
}

/* 进度条下面那句「识别语音 3/13 段 · 预计还需 1m40s」 */
.stage-text {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.5;
}

/* 听音频的展开区，和 fill-box 保持同一套视觉 */
.listen-box {
  padding: 10px 12px 12px;
  margin: 0 0 8px;
  border-radius: 8px;
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}

/* 手机上三个按钮挤在右侧会把标题压成两三行，改到正文下方整行排开 */
@media (max-width: 720px) {
  .task-item {
    flex-wrap: wrap;
  }
  .task-item > .row.wrap {
    width: 100%;
    justify-content: flex-start;
    align-self: stretch;
  }
}
</style>
