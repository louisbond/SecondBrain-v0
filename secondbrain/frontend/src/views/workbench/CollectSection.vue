<script setup>
/**
 * 采集段（v42 从 WorkbenchView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { ref, inject } from 'vue'
import { useWorkbench } from '../../composables/useWorkbench'
import { itemsApi } from '../../api/client'
import ModelPicker from '../../components/ModelPicker.vue'

const {
  rawText,
  parsed,
  parsing,
  submitting,
  collectAudio,
  items,
  validItems,
  dupCount,
  noUrlCount,
  parseAndSubmit,
  loadStatus
} = useWorkbench()

const toast = inject('toast')

// -- 粘贴时给链接补空格 ----------------------------------------------------
/**
 * 每次粘贴，自动在**每个链接后面加两个空格**。
 *
 * 为什么要加：分享文案里链接后面常常紧跟着中文/复制提示
 * （"…的作品 https://v.douyin.com/xxx 复制此链接"），
 * 不加分隔的话链接边界容易被后面的字吃掉 —— 解析出来的标题会变成
 * 一串莫名其妙的数字（截图里三条都叫「41」），多链接也容易粘连。
 *
 * 规则：
 *   · 链接后面是空白（空格/制表）→ 归一成正好两个空格
 *   · 链接后面紧跟非空白字符   → 补两个空格
 *   · 链接后面是换行           → **不动**（换行是分块依据，吃掉会粘连）
 *
 * URL 的字符集与后端 `parser._URL_RE` 保持一致（纯 ASCII），
 * 所以中文标点不会被误吞进链接里。
 *
 * 实现注意：**不能用 lookahead 写法**（如 `(url)(?=[^\s])`）。
 * 贪心的 `+` 在匹配失败时会回溯 —— "https://x.com/1" 会被拆成
 * "https://x.com/" + "  " + "1"，URL 结尾字符被甩到空格后面。
 * 必须用 matchAll 拿到完整匹配后手工拼接。
 */
const URL_RE = /https?:\/\/[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+/g

function padLinks(text) {
  const src = String(text || '')
  if (!src) return src
  let out = ''
  let last = 0
  // matchAll 需要带 g 标志的正则（上面的 URL_RE 已带）
  for (const m of src.matchAll(URL_RE)) {
    const s = m.index
    const e = s + m[0].length
    out += src.slice(last, s)
    out += m[0]
    const rest = src.slice(e)
    const ws = /^[ \t]*/.exec(rest)
    const wsText = ws ? ws[0] : ''
    if (wsText) {
      // 已有水平空白 → 归一成两个空格
      out += '  '
      last = e + wsText.length
    } else if (rest && !rest.startsWith('\n')) {
      // 紧跟非空白字符 → 补两个空格
      out += '  '
      last = e
    } else {
      // 换行结尾（或文本结束）→ 不动
      last = e
    }
  }
  out += src.slice(last)
  return out
}

/**
 * 粘贴处理器。
 *
 * 优先用 `document.execCommand('insertText')` —— 它走浏览器原生插入通道，
 * **撤销栈（Ctrl+Z）还在**，用户能撤销这次粘贴。
 * 少数环境禁用了 execCommand，兜底改成手动改 value + 派发 input 事件。
 */
function onPaste(e) {
  const dt = e.clipboardData
  if (!dt) return
  const raw = dt.getData('text/plain')
  if (!raw) return
  e.preventDefault()

  const patched = padLinks(raw)
  const ta = e.target
  if (typeof document !== 'undefined' && document.execCommand) {
    const ok = document.execCommand('insertText', false, patched)
    if (ok) return
  }
  // 兜底：手动插入
  const start = ta.selectionStart ?? ta.value.length
  const end = ta.selectionEnd ?? start
  ta.value = ta.value.slice(0, start) + patched + ta.value.slice(end)
  const pos = start + patched.length
  ta.setSelectionRange(pos, pos)
  ta.dispatchEvent(new Event('input', { bubbles: true }))
}

/**
 * 直接导入视频文件 —— 不走链接。
 *
 * 抖音/快手匿名请求不给视频直链，小红书图文没有音轨。用户实际的做法
 * 是「在 App 里保存视频，然后导入」。老流程要求先粘链接建条目再上传，
 * 但这类链接连标题都抓不出来，等于逼用户造假链接。
 *
 * 支持多选：一次拖 20 个文件进来，每条各自排队、各自报进度。
 */
const fileInput = ref(null)
const uploading = ref(false)
const uploadDone = ref(0)
const uploadTotal = ref(0)
const uploadResults = ref([])

function pickFiles() {
  fileInput.value?.click()
}

// 与后端 [media] max_file_mb 对应。真正拦截在后端，这里只是提前告知，
// 免得用户等 500MB 传完才收到拒绝。
const UPLOAD_MAX_MB = 500
// 一次导入 20 个文件时不能串行等（每个 500MB 会很慢），也不能全并发把
// 本机打满。每批 2 个，每个文件仍是一条独立 item + 一条独立 job。
const BATCH_SIZE = 2

async function uploadOne(file) {
  if (file.size > UPLOAD_MAX_MB * 1024 * 1024) {
    const mb = Math.round(file.size / 1024 / 1024)
    uploadDone.value += 1
    return { name: file.name, kind: 'fail', detail: `${mb}MB 超过 ${UPLOAD_MAX_MB}MB 上限` }
  }
  try {
    const r = await itemsApi.uploadNew(file)
    return {
      name: file.name,
      kind: r.duplicate ? 'dup' : 'ok',
      detail: r.duplicate ? '已存在，跳过' : `任务 #${r.item_id} 已排队`,
      itemId: r.item_id
    }
  } catch (err) {
    return { name: file.name, kind: 'fail', detail: err.message }
  } finally {
    uploadDone.value += 1
  }
}

async function onFilesChosen(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length) return

  uploading.value = true
  uploadTotal.value = files.length
  uploadDone.value = 0
  uploadResults.value = []

  const results = []
  for (let i = 0; i < files.length; i += BATCH_SIZE) {
    const batch = files.slice(i, i + BATCH_SIZE)
    results.push(...(await Promise.all(batch.map(uploadOne))))
  }
  uploadResults.value = results

  uploading.value = false
  const ok = results.filter((r) => r.kind === 'ok').length
  const dup = results.filter((r) => r.kind === 'dup').length
  const failed = results.filter((r) => r.kind === 'fail').length
  const parts = []
  if (ok) parts.push(`${ok} 条已排队转写`)
  if (dup) parts.push(`${dup} 条重复已跳过`)
  if (failed) parts.push(`${failed} 条失败`)
  toast(parts.join('，') || '没有导入任何文件', failed ? 'warn' : 'success', 6000)
  if (failed)
    console.warn(
      '[导入失败]',
      results.filter((r) => r.kind === 'fail')
    )

  // 关键：上传完立刻刷新状态条。以前只靠 3 秒轮询，用户会以为「没反应」，
  // 多文件导入时更看不出每个文件各自排了一条任务。
  await loadStatus()
}
</script>

<template>
  <div class="collect-section">
    <div class="mb-head">
      <span class="mb-icon">⇪</span>
      <span class="mb-title">采集</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" :disabled="uploading" @click="pickFiles">
        {{ uploading ? `导入中 ${uploadDone}/${uploadTotal}` : '导入视频/音频' }}
      </button>
    </div>

    <input
      ref="fileInput"
      type="file"
      multiple
      accept="video/*,audio/*,.mp4,.m4a,.mp3,.webm,.mov,.wav,.flv,.mkv,.aac"
      style="display: none"
      @change="onFilesChosen"
    />

    <div v-if="uploadResults.length" class="mb-uploads">
      <div v-for="(r, i) in uploadResults" :key="i" class="mb-upload-row">
        <span :class="['up-dot', r.kind]" />
        <span class="up-name" :title="r.name">{{ r.name }}</span>
        <span class="dim up-detail">{{ r.detail }}</span>
      </div>
    </div>

    <textarea
      v-model="rawText"
      rows="2"
      placeholder="把分享文案整段粘进来（链接 / 标题 / 简介一起），自动识别平台"
      @paste="onPaste"
    ></textarea>

    <div class="row mb-actions">
      <!-- 提取模型**位居最左**（用户 2026-11-09）；采集音频勾选紧随其后；
           右侧「解析链接」开始采集（一步到位）。整行不换行。 -->
      <ModelPicker kind="extract" label="提取模型" />
      <label class="audio-toggle" title="勾选后除文本外，还下载视频直链抽音频转写（归档口播台词 + MP3）">
        <input v-model="collectAudio" type="checkbox" />
        采集音频
      </label>
      <div class="spacer" style="flex: 1"></div>
      <span v-if="parsed" class="dim mb-count">
        识别到 {{ items.length }} 条{{ submitting ? '，正在提交…' : '' }}
        <template v-if="items.length">
          · 待提交 {{ validItems.length }}
          <template v-if="dupCount">· 已存在 {{ dupCount }}</template>
          <template v-if="noUrlCount">· 无链接 {{ noUrlCount }}</template>
        </template>
      </span>
      <button
        class="btn primary"
        :disabled="parsing || submitting || !rawText.trim()"
        @click="parseAndSubmit"
      >
        {{ parsing ? '解析中…' : submitting ? '提交中…' : '解析链接' }}
      </button>
    </div>
  </div>
</template>

<style scoped>

.title-edit {
  width: 100%;
  padding: 2px 6px;
  border-radius: 5px;
  border: 1px solid var(--ai, #7c6cf0);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-size: 13px;
  font-weight: 600;
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
  flex: none;
}
.mb-title {
  font-size: 13.5px;
  font-weight: 600;
  flex: none;
}
.mb-hint {
  font-size: 11.5px;
  font-weight: 400;
  /* 说明文字可截断：容器变窄时不让它把右侧按钮挤到下一行 */
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mb-actions {
  margin-top: 8px;
  align-items: center;
  /* 允许整行折行：模型控件本身不可收缩（组件内已锁死），
     空间不够时让后面的按钮换到下一行，而不是把控件压变形。 */
  flex-wrap: wrap;
}

.audio-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-dim, #9a9daa);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.audio-toggle input {
  margin: 0;
}
.mb-count {
  font-size: 11.5px;
  margin-left: 2px;
}
/* 多文件导入：每个文件一行，能看出「几个文件 = 几条独立任务」 */
.mb-uploads {
  margin-top: 8px;
  max-height: 132px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px 8px;
}
.mb-upload-row {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  padding: 3px 0;
}
.up-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ok);
}
.up-dot.dup {
  background: var(--warn, #d9a441);
}
.up-dot.fail {
  background: var(--err, #e05555);
}
.up-name {
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.up-detail {
  margin-left: auto;
  white-space: nowrap;
}

/* 原来由主壳的 `.wb-merge textarea` 提供。拆成子组件后 scoped 选择器
   跨不过组件边界（只有子组件**根元素**带父 scope id），所以移到这里。 */
textarea {
  min-height: 46px;
}
.collect-section {
  flex: 0 0 auto;
}
</style>
