<script setup>
/**
 * 解析段（含配图）（v42 从 WorkbenchView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { useWorkbench } from '../../composables/useWorkbench'
import { itemsApi } from '../../api/client'

const toast = inject('toast')
const showFull = ref(false)

const {
  current,
  loadingContent,
  retrying,
  canRetry,
  stateClass,
  bodyLines,
  bodyCharCount,
  retryCurrent,
  states,
  loadStatus,
  selectItem,
  pickTask,
  shots,
  shotPosition,
  shotCaption,
  shotUrl,
  shotBusy,
  onPickFile,
  insertShot,
  removeShot
} = useWorkbench()

// -- 解析正文下方的入库动作 ------------------------------------------------
// 用户是「看完正文」才决定收不收的，所以动作要贴在正文下面，
// 不能只放在上方列表或页面底部（那样得来回找）。
const actionBusy = ref('')

const canCollect = computed(() => {
  const s = current.value?.status
  return s === 'extracted' || s === 'extract_failed'
})

/** 动作完成后刷新状态条，并跳到下一条待核实素材（没有就清空右侧）。 */
async function afterAction() {
  await loadStatus()
  const left = (states.value || []).filter(
    (i) => i.status === 'extracted' || i.status === 'extract_failed'
  )
  if (left.length) await pickTask(left[0])
  else current.value = null
}

async function doCollect() {
  const id = current.value?.id
  if (!id) return
  actionBusy.value = 'collect'
  try {
    await itemsApi.confirmCollect(id)
    toast?.('已入库，送进「未加工」', 'success')
    await afterAction()
  } catch (e) {
    toast?.('入库失败：' + (e.message || e), 'error')
  } finally {
    actionBusy.value = ''
  }
}

async function doRecollect() {
  const id = current.value?.id
  if (!id) return
  actionBusy.value = 'retry'
  try {
    await itemsApi.retry(id)
    toast?.('已重新提交采集，稍等…', 'success')
    await afterAction()
  } catch (e) {
    toast?.('重新采集失败：' + (e.message || e), 'error')
  } finally {
    actionBusy.value = ''
  }
}

async function doRemove() {
  const it = current.value
  if (!it?.id) return
  if (!confirm(`删除「${it.title || it.id}」？原始文件一并清掉，不可恢复。`)) return
  actionBusy.value = 'del'
  try {
    await itemsApi.del(it.id)
    toast?.('已删除', 'success')
    await afterAction()
  } catch (e) {
    toast?.('删除失败：' + (e.message || e), 'error')
  } finally {
    actionBusy.value = ''
  }
}

// -- 采集失败时：直接转写本地视频 ------------------------------------------
// 抓取失败最常见的原因是平台不给直链（抖音/快手匿名请求），
// 这时唯一可靠的路是「在 App 里保存视频 → 从这里传上来转写」。
const mediaTarget = ref(null)
const mediaInput = ref(null)
const uploadingMedia = ref(false)

const canUploadMedia = computed(() => current.value?.status === 'extract_failed')

function pickMediaFor(it) {
  mediaTarget.value = it
  mediaInput.value?.click()
}

async function onMediaChosen(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  const it = mediaTarget.value
  if (!file || !it) return
  if (
    !confirm(
      `上传「${file.name}」并转写这条素材？\n\n转写会覆盖现有正文，视频越长越慢（本机 CPU，不外发）。`
    )
  ) {
    mediaTarget.value = null
    return
  }
  uploadingMedia.value = true
  try {
    const r = await itemsApi.uploadMedia(it.id, file)
    toast?.(r.message || '已排队转写', 'success', 6000)
    await loadStatus()
    if (current.value?.id === it.id) await selectItem(it.id)
  } catch (err) {
    toast?.('上传失败：' + err.message, 'error')
  } finally {
    uploadingMedia.value = false
    mediaTarget.value = null
  }
}

// -- 点击标题改名（采集区「待加工工作区」列表）------------------------------
// 这里改的是已入库条目：PATCH 后后端 rename_inbox 会同步重命名 A 层 md 文件。
const editingTitle = ref(false)
const titleDraft = ref('')

function startTitleEdit() {
  titleDraft.value = current.value?.title || ''
  editingTitle.value = true
}

function cancelTitleEdit() {
  editingTitle.value = false
}

async function saveTitleEdit() {
  const t = titleDraft.value.trim()
  const id = current.value?.id
  if (!id || !t) return cancelTitleEdit()
  if (t === String(current.value?.title || '')) return cancelTitleEdit()
  try {
    const r = await itemsApi.patch(id, { title: t })
    current.value.title = t
    if (r?.renamed_to) current.value.inbox_path = r.renamed_to
    // 通知其它页面（加工区核准卡）刷新标题，否则那边还显示缓存的旧名字
    window.dispatchEvent(
      new CustomEvent('sb:item-updated', { detail: { id, title: t } })
    )
    toast('已改名，文件名同步更新', 'success')
    editingTitle.value = false
  } catch (e) {
    toast('改名失败：' + (e.message || e), 'error')
  }
}

// 小窗口里看几千字转写稿很难受，点一下全屏看，顺手能复制。
const fullText = computed(() => bodyLines.value.join('\n'))

async function copyFull() {
  try {
    await navigator.clipboard.writeText(fullText.value)
    toast?.('已复制全文', 'success')
  } catch (e) {
    toast?.('复制失败：' + e.message, 'error')
  }
}

function onKey(e) {
  if (e.key === 'Escape') showFull.value = false
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="parse-section" :class="{ 'parse-section--empty': !current }">
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
        <div class="spacer" style="flex: 1"></div>
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
        <template v-if="!editingTitle">
          <span
            class="mb-cur-title title-clickable"
            title="点击改标题（会同步重命名 A 层文件）"
            @click="startTitleEdit"
            >{{ current.title }}</span
          >
        </template>
        <template v-else>
          <input
            v-model="titleDraft"
            class="title-edit"
            @keyup.enter="saveTitleEdit"
            @keyup.esc="cancelTitleEdit"
          />
          <button class="btn sm" @click="saveTitleEdit">确定</button>
          <button class="btn ghost sm" @click="cancelTitleEdit">取消</button>
        </template>
        <span class="dim mono mb-cur-meta">
          {{ current.source_url || '本地导入' }} · 原文 {{ current.raw_len }} 字
        </span>
      </div>

      <div v-if="current.error_detail" class="alert warn mb-err">
        {{ current.error_detail }}
      </div>

      <div class="mb-body-label">
        <span>{{ current.asr ? '📄 语音转写' : '📄 原文' }}</span>
        <span class="badge">{{ bodyLines.length }} 行</span>
        <span class="badge">{{ bodyCharCount }} 字</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="showFull = true">⤢ 放大查看</button>
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
          style="cursor: pointer"
          @click="removeShot(i)"
        >
          {{ s }} ×
        </span>
      </div>

      <!-- 原素材入库 / 重新采集 / 删除：贴在解析正文下面（看完正文就地决定收 / 退）
           注意：这里是**原素材入库**（存抓到的原文原文），和核准区的「总结入库」
           （AI 摘要 + 手稿 + 标签 + 分区）不是一回事。 -->
      <div v-if="canCollect" class="parse-actions">
        <button
          v-if="canUploadMedia"
          class="btn ghost sm"
          :disabled="uploadingMedia"
          @click="pickMediaFor(current)"
        >
          {{ uploadingMedia ? '上传中…' : '📤 转写视频' }}
        </button>
        <button class="btn ghost sm" :disabled="!!actionBusy" @click="doRecollect">
          {{ actionBusy === 'retry' ? '提交中…' : '重新采集' }}
        </button>
        <button class="btn ghost sm parse-danger" :disabled="!!actionBusy" @click="doRemove">
          🗑 删除
        </button>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn primary sm" :disabled="!!actionBusy" @click="doCollect">
          {{ actionBusy === 'collect' ? '入库中…' : '✓ 素材入库' }}
        </button>
      </div>

      <input
        ref="mediaInput"
        type="file"
        accept=".mp4,.m4a,.mp3,.webm,.mov,.wav,.flv,.mkv,.aac"
        style="display: none"
        @change="onMediaChosen"
      />
    </template>
  </div>

  <Teleport to="body">
    <div v-if="showFull" class="full-mask" @click.self="showFull = false">
      <div class="full-panel">
        <div class="full-head">
          <b>{{ current?.title || '原文' }}</b>
          <span class="dim">{{ bodyCharCount }} 字 · {{ bodyLines.length }} 行</span>
          <div style="flex: 1"></div>
          <button class="btn ghost sm" @click="copyFull">复制全文</button>
          <button class="btn ghost sm" @click="showFull = false">关闭 (Esc)</button>
        </div>
        <pre class="full-body">{{ fullText }}</pre>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 标题：默认只读，点一下才变输入框 + 确定/取消（同步重命名 A 层文件） */
.title-clickable {
  cursor: text;
  padding: 2px 5px;
  border-radius: 5px;
  border: 1px dashed transparent;
}

.title-clickable:hover {
  border-color: var(--line, #33353d);
  background: var(--bg-elevated, #22242b);
}

.title-edit {
  min-width: 260px;
  padding: 3px 7px;
  border-radius: 6px;
  border: 1px solid var(--ai, #7c6cf0);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-size: 14px;
  font-weight: 600;
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

/* 逐层 flex：.wb-merge(列) → 本组件(列, flex:1) → .code-box(flex:1)。
   这样转写框仍吃掉卡片剩余高度，窗口变高/变矮都自适应。 */
.parse-section {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 没选任务时不拉伸，避免一张大空卡 */
.parse-section--empty {
  flex: 0 0 auto;
}

/* 全文放大：Teleport 到 body，不受卡片 overflow 裁剪 */
.full-mask {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.55);
}
.full-panel {
  width: min(1000px, 94vw);
  height: min(86vh, 900px);
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  background: var(--bg, #1b1d21);
  border: 1px solid var(--border);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.45);
  overflow: hidden;
}
.full-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
}
.full-body {
  flex: 1;
  margin: 0;
  padding: 16px 20px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.85;
  font-family: inherit;
}
.parse-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--line, #33353d);
}

.parse-danger {
  color: var(--danger, #e5484d);
}
</style>
