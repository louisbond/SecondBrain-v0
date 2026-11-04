<script setup>
/**
 * 加工区 · 核准页：确认 AI 的摘要、分区、标签，产出 B 层素材。
 *
 * 左：待加工队列
 * 右：AI 摘要 → 分区树（层级）→ 标签 → 确认入库
 *
 * 「原创区」是独立一级入口（/original）——本页的解析结果一键送去那里。
 */
import { ref, computed, onMounted, inject, watch } from 'vue'
import { useRouter } from 'vue-router'
import { reviewApi, itemsApi, workbenchApi, searchApi } from '../api/client'
import { saveDraft } from '../utils/draft'
import StatusStrip from '../components/StatusStrip.vue'

const toast = inject('toast')
const refreshSignal = inject('refreshSignal')
const router = useRouter()

// ---------------- 队列 ----------------
const queue = ref([])
const manualQueue = ref([])
const current = ref(null)
const loading = ref(false)
const saving = ref(false)

// ---------------- 核准表单 ----------------
const form = ref({
  title: '',
  ai_summary: '',
  personal_summary: '',
  tags: [],
  partitions: [],
  confirmed_related: [],
  new_tags: []
})
const vocab = ref({ tags: [], partitions: [] })
const partitionTree = ref([])
const expanded = ref({})
const newPartition = ref('')
const reanalyzing = ref(false)

// ---------------- 模板化解析（并回核准：先设模板/字数 → AI 总结）----------------
const templateId = ref(null)
const templates = ref([])
const minChars = ref(800)
const charPresets = ref([200, 500, 800, 1000, 1500, 2000, 3000, 5000])
const parsingBusy = ref(false)
const parseResult = ref(null)
const parseText = ref('')

const platformMeta = {
  bilibili: { label: 'B站', color: 'var(--bilibili)' },
  douyin: { label: '抖音', color: 'var(--douyin)' },
  kuaishou: { label: '快手', color: 'var(--kuaishou)' },
  xiaohongshu: { label: '小红书', color: 'var(--xiaohongshu)' },
  web: { label: '网页', color: 'var(--web)' },
  quick_note: { label: '速记', color: 'var(--ok)' }
}
const _platformOf = (p) => platformMeta[p] || { label: p, color: 'var(--web)' }

const summaryTypeLabel = computed(() => {
  const t = current.value?.ai_summary_type
  return { bullets: '要点式', structured: '结构化', one_liner: '一句话' }[t] || '要点式'
})

/**
 * AI 来源的可读名称（v32）。
 *
 * 坑：`ai_model` 存的是**内部 provider 名**（openai_compat / ollama /
 * workbuddy），直接展示成 badge 就是「OPENAI_COMPAT」——
 * 用户完全不知道这是什么（截图里右上角那个）。
 * 现在映射成人话：「豆包（火山方舟）/ ollama 本地」。
 */
const aiModelLabel = computed(() => {
  const m = String(current.value?.ai_model || '').toLowerCase()
  if (!m) return ''
  if (m.includes('openai_compat') || m.includes('ark') || m.includes('doubao')) {
    return '豆包（火山方舟）'
  }
  if (m.includes('ollama')) return 'ollama 本地'
  return m
})

/** 这条素材当前处在哪一步（可读描述，替代裸状态码）。 */
const currentStageHint = computed(() => {
  const s = current.value?.item?.status
  if (s === 'raw_ready') return { text: '原文就绪 · 尚未做 AI 提取', cls: 'accent' }
  if (s === 'pending_review') return { text: 'AI 已提取 · 等你确认', cls: '' }
  if (s === 'ai_failed') return { text: 'AI 提取失败 · 可重试', cls: 'warn' }
  if (s === 'extract_failed') return { text: '抓取失败 · 可重试', cls: 'warn' }
  return { text: current.value?.item?.status || '', cls: '' }
})

// v31：采集完成后停在「原文就绪」（raw_ready），没跑过 AI。
// 这时按钮该叫「AI 提取要点」而不是「重新分析」——语义完全不同。
const isRawReady = computed(() => current.value?.item?.status === 'raw_ready')

/**
 * 加工队列的两行分组（v37）。
 *
 * 与采集区「完成/失败/进行中/等待」不同，加工区关心的是**下一步做什么**：
 *   待加工 = pending_review（AI 已提取，等你确认）
 *   待手核 = manualQueue（原文就绪 raw_ready，或抓取/解析失败要人接手）
 */
const queueGroups = computed(() => [
  { key: 'pending', label: '待加工', icon: '✎', cls: 'accent', items: queue.value },
  {
    key: 'manual',
    label: manualQueue.value.some((i) => i.status !== 'raw_ready') ? '待手核 · 含异常' : '待手核',
    icon: '✋',
    cls: 'warn',
    items: manualQueue.value
  }
])

/** 点队列里的 chip = 选中该条目（和原来点 chip 的行为一致）。 */
function pickQueue(item) {
  if (item && item.id) selectItem(item.id)
}

const otherTags = computed(() => {
  const used = new Set([...(current.value?.suggested_tags || []), ...form.value.tags])
  return (vocab.value.tags || [])
    .map((t) => (typeof t === 'string' ? t : t.name))
    .filter((name) => !used.has(name))
})

// 队列分组的标题（v35：用户要求「等你加工」改成「待手核」）。
// raw_ready = 采集完的原文就绪条目，等着用户在加工区**手工核对**
// （补手稿 / 确认分区与标签 / 或先跑一次 AI 提取要点）。
async function loadQueue() {
  loading.value = true
  try {
    const data = await reviewApi.pending()
    queue.value = data.items || []
    // manual 已包含 ai_failed（后端 MANUAL_CONFIRMABLE 覆盖），无需单独取
    manualQueue.value = data.manual || []
    const all = [...queue.value, ...manualQueue.value]
    if (all.length && !current.value) await selectItem(all[0].id)
    if (!all.length) current.value = null
  } catch (e) {
    toast('加载队列失败：' + e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function loadVocab() {
  try {
    const [t, p] = await Promise.all([
      searchApi.vocabulary(),
      fetch('/api/partitions').then((r) => r.json())
    ])
    // 词表保持对象数组
    vocab.value = {
      tags: t.tags || [],
      partitions: p.partitions || []
    }
    partitionTree.value = p.tree || []
  } catch {
    /* 忽略 */
  }
}

async function loadTemplates() {
  try {
    const data = await workbenchApi.templates()
    templates.value = data.templates || []
  } catch {
    /* 忽略 */
  }
  try {
    const data = await workbenchApi.charsPresets()
    charPresets.value = data.presets || charPresets.value
    minChars.value = data.default || 800
  } catch {
    /* 忽略 */
  }
}

async function runParse() {
  if (!current.value) {
    toast('先选一条素材', 'warn')
    return
  }
  parsingBusy.value = true
  parseResult.value = null
  parseText.value = ''
  try {
    const r = await workbenchApi.parse({
      item_id: current.value.item.id,
      template_id: templateId.value,
      min_chars: minChars.value
    })
    parseResult.value = r
    parseText.value = r.content || ''
    if (r.ok) {
      toast(`已生成 ${r.chars} 字`, 'success')
    } else {
      // 后端返回了业务失败（如「重试 3 次仍不足 800 字」）——
      // 把原因留在面板上，别只弹一句 toast 就没了。
      toast('生成失败：' + (r.error || '未知原因'), 'error')
    }
  } catch (e) {
    // 网络/500 异常：catch 里要把错误**留在面板上**。
    // 早先这里只弹 toast，parseResult 是 null → 错误区永远不显示，
    // 用户既看不到失败原因，也没有重试的抓手。
    parseResult.value = { ok: false, error: e.message || String(e) }
    toast('生成失败：' + e.message, 'error')
  } finally {
    parsingBusy.value = false
  }
}

/** 把解析结果送到原创区（sessionStorage 草稿）。 */
function sendToOriginal() {
  if (!parseText.value) {
    toast('还没有解析结果', 'warn')
    return
  }
  saveDraft({
    title: form.value.title || current.value?.item?.title || '',
    body: parseText.value,
    tags: [...form.value.tags],
    source: current.value?.item?.id ? `item:${current.value.item.id}` : ''
  })
  toast('已送到原创区，可继续修改', 'success')
  router.push('/original')
}

async function selectItem(id) {
  current.value = { id, item: { id } }
  try {
    const data = await reviewApi.get(id)
    current.value = data
    form.value = {
      title: data.item.title || '',
      ai_summary: data.ai_summary || '',
      personal_summary: '',
      tags: [...(data.suggested_tags || [])],
      partitions: [...(data.suggested_partitions || [])],
      confirmed_related: (data.embedding_related || []).map((r) => r.note_path),
      // 待定标签**默认不勾选**——「词表外的进待定标签，用户勾选才入库」。
      // 之前这里直接铺开全部 pending_tags，等于没经用户确认就写进受控词表。
      new_tags: []
    }
    parseResult.value = null
    parseText.value = ''
  } catch (e) {
    toast('读取条目失败：' + e.message, 'error')
  }
}

// ---------------- 核准交互 ----------------
function togglePartition(name) {
  const i = form.value.partitions.indexOf(name)
  if (i >= 0) form.value.partitions.splice(i, 1)
  else form.value.partitions.push(name)
}

function togglePartPath(path) {
  const i = form.value.partitions.indexOf(path)
  if (i >= 0) form.value.partitions.splice(i, 1)
  else form.value.partitions.push(path)
}

function toggleVocabTag(name) {
  const i = form.value.tags.indexOf(name)
  if (i >= 0) form.value.tags.splice(i, 1)
  else form.value.tags.push(name)
}

function toggleNewTag(name) {
  const i = form.value.new_tags.findIndex((t) => t.name === name)
  if (i >= 0) {
    form.value.new_tags.splice(i, 1)
  } else {
    // 保留 AI 给的理由（chip 的 tooltip 要显示它）
    const src = (current.value?.pending_tags || []).find((t) => t.name === name)
    form.value.new_tags.push({ name, reason: src?.reason || '' })
  }
}

function isNewTagChecked(name) {
  return form.value.new_tags.some((t) => t.name === name)
}

// 待定分区：勾选即视为确认该分区（后端 ensure_by_name 会自动建分区）
function toggleNewPartition(name) {
  const i = form.value.partitions.indexOf(name)
  if (i >= 0) form.value.partitions.splice(i, 1)
  else form.value.partitions.push(name)
}

function isNewPartitionChecked(name) {
  return form.value.partitions.includes(name)
}

function addManualTag(event) {
  const value = event.target.value.trim()
  if (!value) return
  if (!form.value.tags.includes(value)) form.value.tags.push(value)
  event.target.value = ''
}

async function createPartition(event) {
  const value = event.target.value.trim()
  if (!value) return
  try {
    const res = await fetch('/api/partitions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: value })
    })
    if (!res.ok) throw new Error((await res.json()).detail || '创建失败')
    event.target.value = ''
    await loadVocab()
    if (!form.value.partitions.includes(value)) form.value.partitions.push(value)
    toast(`已新建分区「${value}」`, 'success')
  } catch (e) {
    toast('新建分区失败：' + e.message, 'error')
  }
}

function toggleExpand(path) {
  expanded.value[path] = !expanded.value[path]
}

function partSelected(path) {
  return form.value.partitions.includes(path)
}

function partCount(node) {
  let n = node.count || 0
  for (const c of node.children || []) n += partCount(c)
  return n
}

/** 把解析结果送到原创区（跨页草稿，见 utils/draft.js）。 */
async function confirm() {
  if (!current.value) return
  saving.value = true
  try {
    const tags = [...form.value.tags]
    for (const t of form.value.new_tags) {
      if (!tags.includes(t.name)) tags.push(t.name)
    }
    const result = await reviewApi.confirm(current.value.item.id, {
      title: form.value.title,
      personal_summary: form.value.personal_summary,
      tags,
      partitions: form.value.partitions,
      confirmed_related: form.value.confirmed_related,
      ai_summary: form.value.ai_summary
    })
    toast(result.digested ? '已入库（含手稿）' : '已入库，手稿待补', 'success')
    current.value = null
    await loadQueue()
  } catch (e) {
    toast('入库失败：' + e.message, 'error')
  } finally {
    saving.value = false
  }
}

/** 重新让 AI 分析当前条目（摘要/分区/标签不满意时用）。 */
async function reanalyze() {
  if (!current.value || reanalyzing.value) return
  reanalyzing.value = true
  try {
    await itemsApi.retry(current.value.item.id)
    toast('已重新提交分析，稍等片刻…', 'success')
    // 等 AI 跑完再刷新当前条目
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 4000))
      const data = await reviewApi.get(current.value.item.id)
      if (data.ai_summary && data.item.status === 'pending_review') {
        await selectItem(current.value.item.id)
        toast('重新分析完成', 'success')
        return
      }
    }
    toast('分析还没完成，稍后刷新看看', 'error')
  } catch (e) {
    toast('重新分析失败：' + e.message, 'error')
  } finally {
    reanalyzing.value = false
  }
}

async function skip() {
  if (!current.value) return
  try {
    await reviewApi.skip(current.value.item.id)
    toast('已跳过', 'info')
    current.value = null
    await loadQueue()
  } catch (e) {
    toast('跳过失败：' + e.message, 'error')
  }
}

watch(refreshSignal, () => {
  loadQueue()
  loadVocab()
})

onMounted(async () => {
  await loadQueue()
  await loadVocab()
  loadTemplates()
})
</script>

<template>
  <div>
    <div class="proc-col">
      <!--
        v37：上方的「待加工」+「待手核」改成**采集区同款状态条**。

        用户："加工区的待加工和待手核参考采集区的任务状态改进，窗口大小也要相同。"
        原来这里是两张独立的 card（待加工一张、需人工处理一张），样式和采集区
        完全不是一回事，切页面视觉上跳。

        现在直接复用 StatusStrip（同一个组件 → 同一套 padding/字号/圆角），
        紧凑形态（v38）：用户说"看着别扭，高度减一半左右" ——
        2 行按采集区那套 195px 铺会飘在大片空白里，这里 ~100px。
      -->
      <StatusStrip
        :groups="queueGroups"
        title="加工队列"
        :show-all="false"
        :per-group="8"
        dense
        class="strip-top"
        @select="pickQueue"
      />

      <!-- ============ 下：核准（内容区自己滚，页面框架不滚） ============ -->
      <div class="scroll-col">
        <div v-if="!current" class="card empty">
          <div class="icon">▤</div>
          <div>左边选一条素材开始<strong>核准</strong></div>
          <div class="dim" style="margin-top: 6px; font-size: 12px; line-height: 1.7">
            核准：确认<strong>分区</strong>与标签 →
            选模板做<strong>模板化解析</strong>（字数档位可调）→
            生成结果可直接当创作底稿（字数为可调档位）
          </div>
        </div>

        <template v-else>
          <!-- 上半：核准 -->
          <div class="card">
            <!-- v32：头部重排（用户：右上角的状态显示逻辑不清晰）。
                 原来一行里挤着：卡片标题 + 步骤说明 + 「要点式」badge
                 + 「OPENAI_COMPAT」badge + 两个按钮 —— 技术枚举和操作
                 混在一起，用户看不出哪个是"状态"、哪个是"能点的"。
                 现在分三层：
                   ① 标题行：只放标题 + 状态徽标 + 动作按钮
                   ② 步骤行：流程提示（灰字，不抢视觉）
                   ③ 来源行：AI 来源与摘要样式（可读中文 + tooltip） -->
            <div class="card-title">
              <span>🤖</span> 核准
              <span class="badge" :class="currentStageHint.cls">
                {{ currentStageHint.text }}
              </span>
              <div class="spacer" style="flex: 1"></div>
              <button
                class="btn ghost sm"
                :disabled="reanalyzing"
                :title="
                  isRawReady
                    ? '跑一次 AI 提取要点（生成分区/标签建议）'
                    : '摘要或分区标签不满意时，重新让 AI 分析一次'
                "
                @click="reanalyze"
              >
                {{
                  reanalyzing
                    ? isRawReady
                      ? '分析中…'
                      : '重新分析中…'
                    : isRawReady
                      ? 'AI 提取要点'
                      : '重新分析'
                }}
              </button>
              <button class="btn ghost sm" @click="skip">跳过本条</button>
            </div>

            <div class="step-hint">① 解析 → ② 加手稿 → ③ 确认分区与标签 → 入库</div>

            <div v-if="aiModelLabel" class="source-hint">
              AI 来源：{{ aiModelLabel }}
              <span class="sep">·</span>
              摘要样式：{{ summaryTypeLabel }}
            </div>

            <input
              v-model="form.title"
              type="text"
              style="margin: 8px 0; font-size: 15px; font-weight: 600"
              placeholder="标题（默认取 AI 建议）"
            />

            <div v-if="form.ai_summary" class="ai-summary" style="margin-bottom: 10px">
              {{ form.ai_summary }}
            </div>
            <div v-else class="dim" style="margin-bottom: 10px">
              （本次没有 AI 摘要——可在下方用模板重新生成，或直接手写。）
            </div>

            <!-- ① 解析：模板 + 目标字数 + 生成键（同一行） -->
            <div class="section">
              <label>① 解析（选模板与字数档位，让 AI 生成）</label>
              <div class="row wrap parse-row">
                <select v-model="templateId" style="width: auto; min-width: 230px">
                  <option :value="null">AI 自动匹配模板</option>
                  <option v-for="t in templates" :key="t.id" :value="t.id">
                    {{ t.name }}
                  </option>
                </select>
                <select v-model.number="minChars" style="width: auto; min-width: 96px">
                  <option v-for="c in charPresets" :key="c" :value="c">{{ c }} 字</option>
                </select>
                <button class="btn primary" :disabled="parsingBusy" @click="runParse">
                  {{
                    parsingBusy
                      ? '生成中…'
                      : parseResult && !parseResult.ok
                        ? '重新生成'
                        : 'AI 生成解析结果'
                  }}
                </button>
                <button v-if="parseText" class="btn ghost" @click="sendToOriginal">
                  送去原创区 →
                </button>
              </div>
              <div class="dim" style="font-size: 11.5px; margin-top: 4px">
                低于目标字数会自动重新生成（最多 3 次）
              </div>
              <div v-if="parsingBusy" class="dim" style="margin-top: 6px; font-size: 11.5px">
                正在生成（长内容需要 1–3 分钟）
              </div>
              <div v-if="parseResult && !parseResult.ok" class="alert warn" style="margin-top: 8px">
                {{ parseResult.error }}
              </div>
              <div v-if="parseResult && parseResult.ok" class="alert info" style="margin-top: 8px">
                已按模板「{{ parseResult.template?.name }}」生成 {{ parseResult.chars }} 字
                <span v-if="parseResult.matched_by === 'ai'">（AI 自动匹配）</span>
              </div>
              <textarea
                v-if="parseText"
                v-model="parseText"
                rows="8"
                placeholder="解析结果（可直接编辑，满意就送去原创区）"
                style="margin-top: 8px"
              ></textarea>
            </div>

            <!-- ② 个人手稿（对解析不满意时写你的理解） -->
            <div class="section">
              <label>② 个人手稿（这段是你的，图谱里的主节点）</label>
              <textarea
                v-model="form.personal_summary"
                rows="3"
                placeholder="写下你的理解、疑问、行动点"
              ></textarea>
            </div>

            <!-- 分区树选择 -->
            <div class="section">
              <label>③ 分区（选父分区即含其子树）</label>
              <div class="tree">
                <div v-for="node in partitionTree" :key="node.id" class="tree-node">
                  <div class="tree-row">
                    <button class="tree-toggle" @click="toggleExpand(node.path)">
                      {{ expanded[node.path] ? '▾' : '▸' }}
                    </button>
                    <span
                      class="tree-label"
                      :class="{ checked: partSelected(node.path) }"
                      @click="togglePartPath(node.path)"
                      >{{ node.name }}</span
                    >
                    <span class="dim mono" style="margin-left: auto; font-size: 11px">
                      {{ partCount(node) }}
                    </span>
                  </div>
                  <div v-if="expanded[node.path]" class="tree-children">
                    <div
                      v-for="child in node.children"
                      :key="child.id"
                      class="tree-row"
                      style="padding-left: 16px"
                    >
                      <span
                        class="tree-label"
                        :class="{ checked: partSelected(child.path) }"
                        @click="togglePartPath(child.path)"
                        >{{ child.name }}</span
                      >
                      <span class="dim mono" style="margin-left: auto; font-size: 11px">
                        {{ partCount(child) }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
              <div class="row" style="margin-top: 8px">
                <input
                  v-model="newPartition"
                  type="text"
                  placeholder="新分区，回车创建并选中"
                  style="flex: 1"
                  @keydown.enter.prevent="createPartition($event)"
                />
              </div>
              <div
                v-if="(current.pending_partitions || []).length"
                class="row wrap"
                style="margin-top: 8px"
              >
                <span class="dim" style="font-size: 11.5px; width: 100%">
                  待定分区（勾选后自动创建）：
                </span>
                <span
                  v-for="p in current.pending_partitions || []"
                  :key="p.name"
                  class="check-chip pending"
                  :class="{ checked: isNewPartitionChecked(p.name) }"
                  :title="p.reason"
                  @click="toggleNewPartition(p.name)"
                >
                  <span>{{ isNewPartitionChecked(p.name) ? '☑' : '☐' }}</span> +{{ p.name }}
                </span>
              </div>
              <div v-if="form.partitions.length" class="row wrap" style="margin-top: 8px">
                <span
                  v-for="p in form.partitions"
                  :key="p"
                  class="check-chip checked"
                  @click="togglePartition(p)"
                  >{{ p }} ✕</span
                >
              </div>
            </div>

            <!-- 标签 -->
            <div class="section">
              <label>④ 标签（勾选后才写入受控词表）</label>
              <div class="row wrap">
                <span
                  v-for="t in current.suggested_tags || []"
                  :key="t"
                  class="check-chip"
                  :class="{ checked: form.tags.includes(t) }"
                  @click="toggleVocabTag(t)"
                >
                  <span>{{ form.tags.includes(t) ? '☑' : '☐' }}</span> {{ t }}
                </span>
                <span
                  v-for="t in otherTags"
                  :key="t"
                  class="check-chip"
                  :class="{ checked: form.tags.includes(t) }"
                  @click="toggleVocabTag(t)"
                >
                  <span>{{ form.tags.includes(t) ? '☑' : '☐' }}</span> {{ t }}
                </span>
                <span
                  v-for="t in current.pending_tags || []"
                  :key="t.name"
                  class="check-chip pending"
                  :class="{ checked: isNewTagChecked(t.name) }"
                  @click="toggleNewTag(t.name)"
                >
                  <span>{{ isNewTagChecked(t.name) ? '☑' : '☐' }}</span> +{{ t.name }}
                </span>
              </div>
              <div class="row" style="margin-top: 8px">
                <input
                  type="text"
                  placeholder="手动添加标签（回车）"
                  style="flex: 1"
                  @keydown.enter.prevent="addManualTag"
                />
              </div>
            </div>

            <!-- ⑤ 确认入库：分区与标签都定了才入库 -->
            <div
              class="row"
              style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line)"
            >
              <span class="dim" style="font-size: 11.5px">
                分区与标签确认后入库，手稿会成为图谱主节点
              </span>
              <div class="spacer" style="flex: 1"></div>
              <button class="btn primary" :disabled="saving" @click="confirm">
                {{ saving ? '入库中…' : '确认入库' }}
              </button>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 步骤提示：灰字细排，不抢标题的视觉重量 */
.step-hint {
  font-size: 11.5px;
  color: var(--text-dim);
  margin: -2px 0 6px;
}
/* AI 来源：比步骤提示更弱，用户想查的时候能找到就行 */
.source-hint {
  font-size: 11px;
  color: var(--text-dim);
  opacity: 0.75;
  margin-bottom: 10px;
}
.source-hint .sep {
  margin: 0 5px;
  opacity: 0.6;
}
.badge.accent {
  color: var(--accent);
  border-color: var(--accent-border);
}
.badge.warn {
  color: var(--warn, #e5a23d);
  border-color: var(--warn, #e5a23d);
}
/* 解析行：模板 + 字数 + 生成键同一行 */
.parse-row {
  gap: 8px;
  align-items: center;
}
/* 下拉与按钮同高（全局 select padding 偏大，这里压平） */
.parse-row select {
  height: 32px;
  padding: 4px 9px;
  font-size: 13px;
}

/* 加工区：纵向排（待加工工作区在上、核准区在下） */
.proc-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}
/* 队列条不参与拉伸（下面核准区吃剩余高度） */
.strip-top {
  flex: 0 0 auto;
  padding-bottom: 4px;
}

/* 待加工条目：横向排（纵向区块、内部横排） */
.queue-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 108px;
  overflow-y: auto;
}
.queue-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 300px;
  padding: 4px 12px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--bg-hover);
  color: var(--text-dim);
  font-size: 12px;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-chip:hover {
  color: var(--text);
  background: var(--accent-soft);
}
.queue-chip.active {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  color: var(--text);
}
.queue-chip.warn {
  border-color: var(--danger, #e5484d);
}
/* 内容区自己滚：页面框架（顶栏/导航）不动，右侧长内容独立滚动 */
.scroll-col {
  max-height: calc(100vh - 300px);
  min-height: 320px;
  overflow-y: auto;
  padding-right: 6px;
}

.section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.tree-node {
  margin: 2px 0;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 4px;
  border-radius: 6px;
  cursor: pointer;
}
.tree-row:hover {
  background: var(--accent-soft);
}
.tree-toggle {
  width: 16px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  padding: 0;
}
.tree-label {
  font-size: 12.5px;
}
.tree-label.checked {
  color: var(--accent);
  font-weight: 600;
}
.tree-children {
  border-left: 1px solid var(--line);
  margin-left: 8px;
}
.vditor-host {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
</style>
