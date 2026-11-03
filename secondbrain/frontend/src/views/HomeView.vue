<script setup>
/**
 * 首页：一眼看清整个库。
 *
 * 纵向三块（不再用 Tab 切换）：
 *   ① 搜索（上）—— 输入即查，结果内联
 *   ② 图谱（中）—— 3D 知识图谱（布局稳定不乱跳），个人手稿是主节点
 *   ③ 回顾（下）—— 今日该回顾什么
 */
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { searchApi, recapApi, notesApi, reviewApi, noteRoute } from '../api/client'
import Graph3D from '../components/Graph3D.vue'

const toast = inject('toast')
const router = useRouter()

// ---------------- 搜索 ----------------
const query = ref('')
const results = ref([])
const total = ref(0)
const elapsed = ref(0)
const searchMode = ref('')
const searching = ref(false)

// ---------------- 筛选（分区 / 标签 / 范围）----------------
const selectedTags = ref([])
const selectedParts = ref([])
const includeInbox = ref(false)
const includeArchived = ref(false)
const tagList = ref([])
const partitionTree = ref([])
const expanded = ref({})
const showFilters = ref(false)

async function loadVocab() {
  try {
    const [t, p] = await Promise.all([
      searchApi.vocabulary(),
      fetch('/api/partitions').then((r) => (r.ok ? r.json() : { partitions: [], tree: [] }))
    ])
    tagList.value = (t.tags || []).map((x) =>
      typeof x === 'string' ? { id: x, name: x, count: 0 } : x
    )
    partitionTree.value = p.tree || []
  } catch (e) {
    /* 词表加载失败不阻塞搜索 */
  }
}

function toggleTag(name) {
  const i = selectedTags.value.indexOf(name)
  if (i >= 0) selectedTags.value.splice(i, 1)
  else selectedTags.value.push(name)
}

function togglePart(path) {
  const i = selectedParts.value.indexOf(path)
  if (i >= 0) selectedParts.value.splice(i, 1)
  else selectedParts.value.push(path)
}

function toggleExpand(path) {
  expanded.value[path] = !expanded.value[path]
}

function partChecked(path) {
  return selectedParts.value.includes(path)
}

function partCount(node) {
  let n = node.count || 0
  for (const c of node.children || []) n += partCount(c)
  return n
}

const activeFilterCount = computed(
  () =>
    selectedTags.value.length +
    selectedParts.value.length +
    (includeInbox.value ? 1 : 0) +
    (includeArchived.value ? 1 : 0)
)

async function doSearch() {
  if (!query.value.trim()) {
    toast('请输入关键词', 'warn')
    return
  }
  searching.value = true
  try {
    const data = await searchApi.query({
      query: query.value,
      tags: selectedTags.value,
      partitions: selectedParts.value,
      include_inbox: includeInbox.value,
      include_archived: includeArchived.value,
      limit: 20
    })
    results.value = data.results || []
    total.value = data.total || 0
    elapsed.value = data.elapsed_ms || 0
    searchMode.value = data.search_mode || ''
  } catch (e) {
    toast('搜索失败：' + e.message, 'error')
  } finally {
    searching.value = false
  }
}

/** 打开阅读页。用路由跳转而不是 window.location.href——
 *  后者会整页重载，丢掉 SPA 状态（筛选/滚动位置全丢），还会重复拉一遍数据。 */
function openNote(path) {
  if (!path) return
  router.push(noteRoute(path))
}

// ---------------- 图谱 ----------------
const graph = ref({ nodes: [], edges: [], stats: {} })
const graphSel = ref(null)
const loadingGraph = ref(false)

async function loadGraph() {
  loadingGraph.value = true
  try {
    const g = await fetch('/api/workbench/graph?limit=400').then((r) => r.json())
    graph.value = g
  } catch (e) {
    toast('图谱加载失败：' + e.message, 'error')
  } finally {
    loadingGraph.value = false
  }
}

async function openGraphNode(node) {
  graphSel.value = node
  if (!node) return
  if ((node.kind === 'note' || node.kind === 'manuscript') && node.path) {
    openNote(node.path)
  }
}

// ---------------- 回顾 ----------------
// 后端 /api/recap/card 返回的是「计数 + 一条加权未引用旧笔记」，
// 不是 {title, items[]}——前端原先按后者写，回顾块永远是空的。
const card = ref(null)
const needManuscript = ref([])
const showNeedManuscript = ref(false)

// ---- 每周回顾（v32）----
// 用户：一级导航把「首页」换成「每周回顾」。
// 周报由后端 recap.weekly 生成（纯统计 + 模板渲染，不调 AI，很快），
// 结果同时落盘到 vault/_reports/YYYY-Www.md。
const weeklyBusy = ref(false)
const weeklyReport = ref(null)
const weeklyError = ref('')
const showWeeklyBody = ref(false)
const weekStats = ref({})

async function loadCard() {
  try {
    card.value = await recapApi.card()
  } catch (e) {
    card.value = { error: e.message }
  }
}

/** 生成本周周报（幂等：同一天重复点只是重写同一份文件）。 */
async function makeWeekly() {
  if (weeklyBusy.value) return
  weeklyBusy.value = true
  weeklyError.value = ''
  try {
    const r = await recapApi.weekly(0)
    weeklyReport.value = r
    weekStats.value = {
      collected: r.collected,
      reviewed: r.reviewed,
      created: r.created
    }
    showWeeklyBody.value = true
    toast('周报已生成', 'success')
  } catch (e) {
    weeklyError.value = '生成失败：' + (e.message || e)
    toast(weeklyError.value, 'error')
  } finally {
    weeklyBusy.value = false
  }
}

/** 待补手稿清单（已入库但手稿为空的 B 层笔记）。 */
async function loadNeedManuscript() {
  try {
    const data = await reviewApi.needManuscript()
    needManuscript.value = data.items || []
  } catch (e) {
    needManuscript.value = []
  }
}

async function toggleNeedManuscript() {
  showNeedManuscript.value = !showNeedManuscript.value
  if (showNeedManuscript.value && !needManuscript.value.length) {
    await loadNeedManuscript()
  }
}

onMounted(async () => {
  loadVocab()
  await Promise.all([loadCard(), loadGraph()])
})
</script>

<template>
  <div class="home-page">
    <!-- ① 每周回顾（v32：一级导航第一项，落地第一眼就该看到本周概况） -->
    <div class="card">
      <div class="card-title">
        <span>◷</span> 每周回顾
        <span class="dim" style="font-weight:400;font-size:11.5px">
          本周采集 / 入库 / 创作，以及生成的周报
        </span>
        <div class="spacer" style="flex:1"></div>
        <button
          class="btn ghost sm"
          :disabled="weeklyBusy"
          @click="makeWeekly"
        >
          {{ weeklyBusy ? '生成中…' : '生成本周周报' }}
        </button>
        <button class="btn ghost sm" @click="loadCard">刷新</button>
      </div>

      <!-- 本周统计（优先显示周报数据，没生成过就退回卡片数据） -->
      <div class="recap-stats">
        <span>
          本周采集 <b>{{ weekStats.collected ?? card?.collected_week ?? 0 }}</b>
        </span>
        <span>
          本周入库 <b>{{ weekStats.reviewed ?? 0 }}</b>
        </span>
        <span>
          本周创作 <b>{{ weekStats.created ?? card?.created_notes_week ?? 0 }}</b>
        </span>
        <span>
          待核准 <b class="accent">{{ card?.pending_review || 0 }}</b>
        </span>
      </div>

      <!-- 生成的周报正文（可折叠全文） -->
      <div v-if="weeklyReport" style="margin-top:10px">
        <div class="row" style="align-items:center;gap:8px">
          <span class="badge ok">已生成</span>
          <span class="dim mono" style="font-size:11.5px">
            {{ weeklyReport.week }} · {{ weeklyReport.week_label || '' }}
          </span>
          <div class="spacer" style="flex:1"></div>
          <button class="btn ghost sm" @click="showWeeklyBody = !showWeeklyBody">
            {{ showWeeklyBody ? '收起全文' : '展开全文' }}
          </button>
        </div>
        <pre
          v-if="showWeeklyBody"
          class="mono weekly-body"
        >{{ weeklyReport.body || '' }}</pre>
      </div>
      <div v-else-if="weeklyError" class="alert warn" style="margin-top:10px">
        {{ weeklyError }}
      </div>

      <div v-if="!card" class="dim" style="margin-top:10px">加载中…</div>
      <div v-else-if="card.error" class="alert warn" style="margin-top:10px">
        {{ card.error }}
      </div>
      <template v-else>
        <div class="recap-row" style="margin-top:8px">
          <!-- 待补手稿（软门槛的提醒 + 补写入口） -->
          <div
            class="list-item"
            style="cursor:pointer"
            @click="toggleNeedManuscript"
          >
            <div class="grow">
              <div class="title">
                待补手稿
                <span class="badge" :class="card.need_manuscript ? 'warn' : 'ok'">
                  {{ card.need_manuscript || 0 }}
                </span>
              </div>
              <div class="sub">
                {{
                  card.need_manuscript
                    ? '已入库但手稿为空——点这里补写'
                    : '手稿都补齐了'
                }}
              </div>
            </div>
            <span class="dim">{{ showNeedManuscript ? '收起' : '▸' }}</span>
          </div>

          <!-- 加权未引用旧笔记 -->
          <div
            v-if="card.unreferenced_note"
            class="list-item"
            style="cursor:pointer"
            @click="openNote(card.unreferenced_note.note_path)"
          >
            <div class="grow">
              <div class="title">{{ card.unreferenced_note.title }}</div>
              <div class="sub">{{ card.unreferenced_note.reason }}</div>
            </div>
          </div>
          <div v-else class="dim" style="padding:4px">
            暂时没有需要回顾的内容
          </div>
        </div>

        <div v-if="showNeedManuscript" class="recap-row" style="margin-top:6px">
          <div
            v-for="(m, i) in needManuscript"
            :key="i"
            class="list-item"
            style="cursor:pointer"
            @click="openNote(m.note_path)"
          >
            <div class="grow">
              <div class="title">{{ m.title }}</div>
              <div class="sub">{{ m.created_at || '' }}</div>
            </div>
          </div>
          <div v-if="!needManuscript.length" class="dim" style="padding:4px">
            没有待补手稿的条目
          </div>
        </div>
      </template>
    </div>

    <!-- ② 搜索（中，保留：知识库检索） -->
    <div class="card">
      <div class="row">
        <input
          v-model="query"
          type="text"
          placeholder="搜索整个知识库…（回车）"
          style="flex:1"
          @keydown.enter="doSearch"
        />
        <button class="btn primary" :disabled="searching" @click="doSearch">
          {{ searching ? '搜索中…' : '搜索' }}
        </button>
        <button
          class="btn ghost sm"
          :class="{ primary: showFilters || activeFilterCount }"
          title="按分区 / 标签 / 范围筛选"
          @click="showFilters = !showFilters"
        >
          筛选<span v-if="activeFilterCount"> · {{ activeFilterCount }}</span>
        </button>
      </div>

      <!-- 筛选面板（可折叠，不占默认视线） -->
      <div v-if="showFilters" class="filter-panel">
        <div class="filter-col">
          <div class="filter-label">分区</div>
          <div class="tree">
            <div v-for="node in partitionTree" :key="node.id" class="tree-node">
              <div class="tree-row">
                <button class="tree-toggle" @click="toggleExpand(node.path)">
                  {{ expanded[node.path] ? '▾' : '▸' }}
                </button>
                <span
                  class="tree-label"
                  :class="{ checked: partChecked(node.path) }"
                  @click="togglePart(node.path)"
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
                    :class="{ checked: partChecked(child.path) }"
                    @click="togglePart(child.path)"
                  >{{ child.name }}</span>
                  <span class="dim mono" style="margin-left:auto;font-size:11px">
                    {{ partCount(child) }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="!partitionTree.length" class="dim" style="font-size:11.5px">
            还没有分区
          </div>
        </div>

        <div class="filter-col">
          <div class="filter-label">标签</div>
          <div class="row wrap">
            <span
              v-for="t in tagList"
              :key="t.id"
              class="check-chip"
              :class="{ checked: selectedTags.includes(t.name) }"
              @click="toggleTag(t.name)"
            >
              <span>{{ selectedTags.includes(t.name) ? '☑' : '☐' }}</span> {{ t.name }}
              <span class="dim mono">{{ t.count }}</span>
            </span>
            <span v-if="!tagList.length" class="dim" style="font-size:11.5px">
              还没有标签
            </span>
          </div>
          <div class="filter-label" style="margin-top:10px">范围</div>
          <div class="row">
            <label class="row" style="margin:0">
              <input v-model="includeInbox" type="checkbox" /> 含收件箱
            </label>
            <label class="row" style="margin:0">
              <input v-model="includeArchived" type="checkbox" /> 含归档
            </label>
          </div>
        </div>
      </div>

      <div v-if="total" class="dim mono" style="font-size:11.5px;margin-top:6px">
        {{ total }} 条 · {{ elapsed }}ms · {{ searchMode }}
      </div>
      <div
        v-if="results.length"
        class="search-results"
      >
        <div
          v-for="r in results"
          :key="r.note_path"
          class="list-item"
          style="cursor:pointer"
          @click="openNote(r.note_path)"
        >
          <div class="grow">
            <div class="title">{{ r.title }}</div>
            <div class="sub">{{ r.snippet }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ③ 图谱（下，保留：3D 知识图谱） -->
    <div class="card">
      <div class="card-title">
        <span>◈</span> 知识图谱
        <span class="dim" style="font-weight:400;font-size:11.5px">
          手稿是主节点 · 拖拽旋转 · 滚轮缩放 · 点击打开
        </span>
        <div class="spacer" style="flex:1"></div>
        <span class="dim mono" style="font-size:11.5px">
          {{ graph.stats?.notes || 0 }} 笔记 · {{ graph.stats?.manuscripts || 0 }} 手稿 ·
          {{ graph.stats?.partitions || 0 }} 分区 · {{ graph.stats?.tags || 0 }} 标签
        </span>
        <button class="btn ghost sm" :disabled="loadingGraph" @click="loadGraph">
          {{ loadingGraph ? '加载中…' : '刷新' }}
        </button>
      </div>
      <Graph3D
        :nodes="graph.nodes || []"
        :edges="graph.edges || []"
        :height="376"
        @select="openGraphNode"
      />
      <div v-if="graphSel" class="alert info" style="margin-top:8px">
        已选中：<b>{{ graphSel.note_title || graphSel.label }}</b>
        <span class="dim">
          （{{ graphSel.kind === 'manuscript' ? '个人手稿' : graphSel.kind }}）
        </span>
      </div>
    </div>


  </div>
</template>

<style scoped>
.home-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.filter-panel {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 14px;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.filter-label {
  font-size: 11.5px;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.tree-node {
  margin: 1px 0;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px;
  border-radius: 6px;
  cursor: pointer;
}
.tree-row:hover {
  background: var(--accent-soft);
}
.tree-toggle {
  width: 14px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  padding: 0;
  font-size: 10px;
}
.tree-label {
  font-size: 12px;
}
.tree-label.checked {
  color: var(--accent);
  font-weight: 600;
}
.tree-children {
  border-left: 1px solid var(--line);
  margin-left: 7px;
}
.search-results {
  margin-top: 6px;
  max-height: 180px;
  overflow-y: auto;
}
.recap-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
}
.recap-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  font-size: 12px;
  color: var(--text-dim);
}
.recap-stats b {
  color: var(--text);
  font-weight: 650;
}
.recap-stats b.accent {
  color: var(--accent);
}
/* 周报正文：等宽 + 限高内滚，避免把首页撑长 */
.weekly-body {
  white-space: pre-wrap;
  max-height: 260px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-hover);
  font-size: 12px;
  line-height: 1.65;
}
</style>
