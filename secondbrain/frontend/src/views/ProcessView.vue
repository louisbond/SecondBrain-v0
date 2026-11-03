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

const toast = inject('toast')
const refreshSignal = inject('refreshSignal')
const router = useRouter()

// ---------------- 队列 ----------------
const queue = ref([])
const manualQueue = ref([])
const aiFailed = ref([])
const current = ref(null)
const loading = ref(false)
const saving = ref(false)
const sourceOpen = ref(false)
const sourceContent = ref(null)

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
const platformOf = (p) => platformMeta[p] || { label: p, color: 'var(--web)' }

const summaryTypeLabel = computed(() => {
  const t = current.value?.ai_summary_type
  return { bullets: '要点式', structured: '结构化', one_liner: '一句话' }[t] || '要点式'
})

const otherPartitions = computed(() => {
  const used = new Set([
    ...(current.value?.suggested_partitions || []),
    ...form.value.partitions
  ])
  return (vocab.value.partitions || [])
    .map((x) => (typeof x === 'string' ? x : x.name))
    .filter((name) => !used.has(name))
})

const otherTags = computed(() => {
  const used = new Set([
    ...(current.value?.suggested_tags || []),
    ...form.value.tags
  ])
  return (vocab.value.tags || [])
    .map((t) => (typeof t === 'string' ? t : t.name))
    .filter((name) => !used.has(name))
})

// 分区词表：AI 建议的可能是「父/子」路径，要能和词表里的 name 对上
const partitionName = (p) => (typeof p === 'string' ? p : p.name)

// ---------------- 加载 ----------------
async function loadQueue() {
  loading.value = true
  try {
    const data = await reviewApi.pending()
    queue.value = data.items || []
    manualQueue.value = data.manual || []
    aiFailed.value = data.ai_failed || []
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
  } catch (e) {
    /* 忽略 */
  }
}

async function loadTemplates() {
  try {
    const data = await workbenchApi.templates()
    templates.value = data.templates || []
  } catch (e) {
    /* 忽略 */
  }
  try {
    const data = await workbenchApi.charsPresets()
    charPresets.value = data.presets || charPresets.value
    minChars.value = data.default || 800
  } catch (e) {
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
    }
  } catch (e) {
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
      new_tags: [...(data.pending_tags || [])],
      new_partitions: [...(data.pending_partitions || [])]
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
  if (i >= 0) form.value.new_tags.splice(i, 1)
  else form.value.new_tags.push({ name, reason: '' })
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


/** 把解析结果送到创作页（独立页面）。 */

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
      <!-- ============ 上：待加工工作区（内部横向排） ============ -->
      <div class="card">
        <div class="card-title">
          <span>≡</span> 待加工
          <span class="badge accent">{{ queue.length }}</span>
          <div class="spacer" style="flex:1"></div>
          <button class="btn ghost sm" @click="loadQueue">刷新</button>
        </div>

        <!-- 待加工条目：横向排（纵向区块、内部横排） -->
        <div class="queue-strip">
          <span
            v-for="item in queue"
            :key="item.id"
            class="queue-chip"
            :class="{ active: current?.item?.id === item.id }"
            :title="item.title || ''"
            @click="selectItem(item.id)"
          >
            <span
              class="platform-dot"
              :style="{ background: platformOf(item.platform).color }"
            ></span>
            {{ item.title || '（无标题）' }}
          </span>
        </div>

        <template v-if="manualQueue.length">
          <div class="card-title" style="margin-top:14px">
            <span>✍️</span> 需人工处理
            <span class="badge warn">{{ manualQueue.length }}</span>
          </div>
          <div class="queue-strip">
            <span
              v-for="item in manualQueue"
              :key="item.id"
              class="queue-chip warn"
              :title="item.title || ''"
              @click="selectItem(item.id)"
            >
              <span
                class="platform-dot"
                :style="{ background: platformOf(item.platform).color }"
              ></span>
              {{ item.title || '（无标题）' }}
            </span>
          </div>
        </template>

        <div v-if="!queue.length && !loading" class="empty" style="padding:24px 8px">
          <div class="icon">✓</div>
          <div>没有待加工的素材</div>
        </div>
      </div>

      <!-- ============ 下：核准（内容区自己滚，页面框架不滚） ============ -->
      <div class="scroll-col">
        <div v-if="!current" class="card empty">
          <div class="icon">▤</div>
          <div>左边选一条素材开始<strong>核准</strong></div>
          <div class="dim" style="margin-top:6px;font-size:12px;line-height:1.7">
            核准：确认<strong>分区</strong>与标签 → 选模板做<strong>模板化解析</strong>（字数档位可调）→
            生成结果可直接当创作底稿（字数为可调档位）
          </div>
        </div>

        <template v-else>
          <!-- 上半：核准 -->
          <div class="card">
            <div class="card-title">
              <span>🤖</span> 核准
              <span class="dim" style="font-weight:400;font-size:11.5px">
                ① 解析 → ② 加手稿 → ③ 确认分区与标签 → 入库
              </span>
              <span class="badge">{{ summaryTypeLabel }}</span>
              <span v-if="current.ai_model" class="badge">{{ current.ai_model }}</span>
              <div class="spacer" style="flex:1"></div>
              <button
                class="btn ghost sm"
                :disabled="reanalyzing"
                :title="'摘要或分区标签不满意时，重新让 AI 分析一次'"
                @click="reanalyze"
              >
                {{ reanalyzing ? '重新分析中…' : '重新分析' }}
              </button>
              <button class="btn ghost sm" @click="skip">跳过本条</button>
            </div>

            <input
              v-model="form.title"
              type="text"
              style="margin:8px 0;font-size:15px;font-weight:600"
              placeholder="标题（默认取 AI 建议）"
            />

            <div v-if="form.ai_summary" class="ai-summary" style="margin-bottom:10px">
              {{ form.ai_summary }}
            </div>
            <div v-else class="dim" style="margin-bottom:10px">
              （本次没有 AI 摘要——可在下方用模板重新生成，或直接手写。）
            </div>

            <!-- ① 解析：模板 + 目标字数 + 生成键（同一行） -->
            <div class="section">
              <label>① 解析（选模板与字数档位，让 AI 生成）</label>
              <div class="row wrap parse-row">
                <select v-model="templateId" style="width:auto;min-width:230px">
                  <option :value="null">AI 自动匹配模板</option>
                  <option v-for="t in templates" :key="t.id" :value="t.id">
                    {{ t.name }}
                  </option>
                </select>
                <select v-model.number="minChars" style="width:auto;min-width:96px">
                  <option v-for="c in charPresets" :key="c" :value="c">{{ c }} 字</option>
                </select>
                <button class="btn primary" :disabled="parsingBusy" @click="runParse">
                  {{ parsingBusy ? '生成中…' : 'AI 生成解析结果' }}
                </button>
                <button v-if="parseText" class="btn ghost" @click="sendToOriginal">
                  送去原创区 →
                </button>
              </div>
              <div class="dim" style="font-size:11.5px;margin-top:4px">
                低于目标字数会自动重新生成（最多 3 次）
              </div>
              <div v-if="parsingBusy" class="dim" style="margin-top:6px;font-size:11.5px">
                正在生成（长内容需要 1–3 分钟）
              </div>
              <div v-if="parseResult && !parseResult.ok" class="alert warn" style="margin-top:8px">
                {{ parseResult.error }}
              </div>
              <div v-if="parseResult && parseResult.ok" class="alert info" style="margin-top:8px">
                已按模板「{{ parseResult.template?.name }}」生成 {{ parseResult.chars }} 字
                <span v-if="parseResult.matched_by === 'ai'">（AI 自动匹配）</span>
              </div>
              <textarea
                v-if="parseText"
                v-model="parseText"
                rows="8"
                placeholder="解析结果（可直接编辑，满意就送去原创区）"
                style="margin-top:8px"
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
                    >{{ node.name }}</span>
                    <span class="dim mono" style="margin-left:auto;font-size:11px">
                      {{ partCount(node) }}
                    </span>
                  </div>
                  <div v-if="expanded[node.path]" class="tree-children">
                    <div
                      v-for="child in node.children"
                      :key="child.id"
                      class="tree-row"
                      style="padding-left:16px"
                    >
                      <span
                        class="tree-label"
                        :class="{ checked: partSelected(child.path) }"
                        @click="togglePartPath(child.path)"
                      >{{ child.name }}</span>
                      <span class="dim mono" style="margin-left:auto;font-size:11px">
                        {{ partCount(child) }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
              <div class="row" style="margin-top:8px">
                <input
                  v-model="newPartition"
                  type="text"
                  placeholder="新分区，回车创建并选中"
                  style="flex:1"
                  @keydown.enter.prevent="createPartition($event)"
                />
              </div>
              <div
                v-if="(current.pending_partitions || []).length"
                class="row wrap"
                style="margin-top:8px"
              >
                <span class="dim" style="font-size:11.5px;width:100%">
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
              <div v-if="form.partitions.length" class="row wrap" style="margin-top:8px">
                <span
                  v-for="p in form.partitions"
                  :key="p"
                  class="check-chip checked"
                  @click="togglePartition(p)"
                >{{ p }} ✕</span>
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
              <div class="row" style="margin-top:8px">
                <input
                  type="text"
                  placeholder="手动添加标签（回车）"
                  style="flex:1"
                  @keydown.enter.prevent="addManualTag"
                />
              </div>
            </div>

            <!-- ⑤ 确认入库：分区与标签都定了才入库 -->
            <div class="row" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
              <span class="dim" style="font-size:11.5px">
                分区与标签确认后入库，手稿会成为图谱主节点
              </span>
              <div class="spacer" style="flex:1"></div>
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
