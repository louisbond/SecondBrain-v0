<script setup>
/**
 * 首页：一眼看清整个库。
 *
 * 纵向两块，**正好填满一屏**（v35：用户要求去掉「今日回顾」，
 * 剩下的格子正好填满窗口）：
 *   ① 搜索（上）—— 输入即查，结果内联
 *   ② 图谱（下）—— 3D 知识图谱，吃掉剩余高度
 *
 * 回顾相关的内容已挪到侧边栏「每周回顾」面板，这一页不再有回顾卡。
 */
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { searchApi, noteRoute } from '../api/client'
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
  } catch {
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
// （v35：回顾卡片已从首页移除，下面这些状态/函数不再需要）
// 后端 /api/recap/card 返回的是「计数 + 一条加权未引用旧笔记」，
// 不是 {title, items[]}——前端原先按后者写，回顾块永远是空的。

onMounted(async () => {
  loadVocab()
  await loadGraph()
})
</script>

<template>
  <div class="home-page">
    <!-- ① 搜索（v35：今日回顾已删，搜索与图谱填满整屏） -->
    <div class="card home-search">
      <div class="row">
        <input
          v-model="query"
          type="text"
          placeholder="搜索整个知识库…（回车）"
          style="flex: 1"
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
                    :class="{ checked: partChecked(child.path) }"
                    @click="togglePart(child.path)"
                    >{{ child.name }}</span
                  >
                  <span class="dim mono" style="margin-left: auto; font-size: 11px">
                    {{ partCount(child) }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="!partitionTree.length" class="dim" style="font-size: 11.5px">还没有分区</div>
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
            <span v-if="!tagList.length" class="dim" style="font-size: 11.5px"> 还没有标签 </span>
          </div>
          <div class="filter-label" style="margin-top: 10px">范围</div>
          <div class="row">
            <label class="row" style="margin: 0">
              <input v-model="includeInbox" type="checkbox" /> 含收件箱
            </label>
            <label class="row" style="margin: 0">
              <input v-model="includeArchived" type="checkbox" /> 含归档
            </label>
          </div>
        </div>
      </div>

      <div v-if="total" class="dim mono" style="font-size: 11.5px; margin-top: 6px">
        {{ total }} 条 · {{ elapsed }}ms · {{ searchMode }}
      </div>
      <div v-if="results.length" class="search-results">
        <div
          v-for="r in results"
          :key="r.note_path"
          class="list-item"
          style="cursor: pointer"
          @click="openNote(r.note_path)"
        >
          <div class="grow">
            <div class="title">{{ r.title }}</div>
            <div class="sub">{{ r.snippet }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ② 图谱（吃掉剩余高度） -->
    <div class="card home-graph">
      <div class="card-title">
        <span>◈</span> 知识图谱
        <span class="dim" style="font-weight: 400; font-size: 11.5px">
          手稿是主节点 · 拖拽旋转 · 滚轮缩放 · 点击打开
        </span>
        <div class="spacer" style="flex: 1"></div>
        <span class="dim mono" style="font-size: 11.5px">
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
        :height="0"
        @select="openGraphNode"
      />
      <div v-if="graphSel" class="alert info graph-sel" style="margin-top: 6px">
        已选中：<b>{{ graphSel.note_title || graphSel.label }}</b>
        <span class="dim">
          （{{ graphSel.kind === 'manuscript' ? '个人手稿' : graphSel.kind }}）
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* v35：首页去掉「今日回顾」后，搜索 + 图谱要**正好填满视口**，
   不留滚动条也不留空白（用户：剩下的格子正好填满窗口）。 */
.home-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}
.home-search {
  flex: 0 0 auto;
  margin-bottom: 0;
  /* 内容少时不出滚动；结果多时自己滚 */
  max-height: 46%;
  overflow-y: auto;
}
.home-graph {
  flex: 1;
  min-height: 0;
  margin-bottom: 0;
  display: flex;
  flex-direction: column;
}
/* Graph3D 外面的容器（组件根节点）吃掉剩余高度 */
.home-graph :deep(.graph3d) {
  flex: 1;
  min-height: 0;
}
.graph-sel {
  flex: 0 0 auto;
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
</style>
