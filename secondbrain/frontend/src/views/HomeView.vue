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
import { searchApi, notesApi, noteRoute } from '../api/client'
import Graph3D from '../components/Graph3D.vue'
import FileTree from '../components/FileTree.vue'
import ManageVocab from './manage/ManageVocab.vue'

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
// 分区与标签管理（从设置页移到首页，搜索区就地管理）
const showVocabMgr = ref(false)

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

/** 点图谱节点：联动右侧文件树（展开 + 高亮 + 滚动），不跳走页面。 */
async function openGraphNode(node) {
  graphSel.value = node
  if (!node) return
  if ((node.kind === 'note' || node.kind === 'manuscript') && node.path) {
    activeNotePath.value = node.path
  }
}

/** 文件树单击：高亮该笔记（图谱同步聚焦），不跳转。 */
function onTreeFocus(path) {
  activeNotePath.value = path
}

// ---------------- 回顾 ----------------
// （v35：回顾卡片已从首页移除，下面这些状态/函数不再需要）
// 后端 /api/recap/card 返回的是「计数 + 一条加权未引用旧笔记」，
// 不是 {title, items[]}——前端原先按后者写，回顾块永远是空的。

// ---------------- 右侧文件树 ----------------
const notes = ref([])
const activeNotePath = ref('')

async function loadNotes() {
  try {
    const d = await notesApi.list({ limit: 500 })
    notes.value = d.notes || []
  } catch (e) {
    /* 树加载失败不影响搜索/图谱 */
  }
}

/** 拖拽移动后：图谱和树都刷新（图谱按新位置重排） */
async function reloadAfterMove() {
  await Promise.all([loadGraph(), loadNotes()])
}

/** 首页直接删除笔记（二次确认） */
async function removeNote(r) {
  const name = r.title || r.note_path
  if (!confirm(`删除笔记「${name}」？文件会一并删掉，不可恢复。`)) return
  try {
    await notesApi.remove(r.note_path)
    results.value = (results.value || []).filter((x) => x.note_path !== r.note_path)
    total.value = Math.max(0, (total.value || 1) - 1)
    toast('已删除', 'success')
  } catch (e) {
    toast('删除失败：' + (e.message || e), 'error')
  }
}

onMounted(async () => {
  loadNotes()
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
        <div class="row" style="margin-bottom: 8px; grid-column: 1 / -1">
          <div class="spacer" style="flex: 1"></div>
          <button class="btn ghost sm" @click="showVocabMgr = true">
            ⚙ 管理分区与标签
          </button>
        </div>
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
          <button
            class="btn ghost sm del-btn"
            title="删除这篇笔记"
            @click.stop="removeNote(r)"
          >
            ✕
          </button>
        </div>
      </div>
    </div>

    <!-- ② 左：图谱 · 右：文件树（拖拽移动后图谱跟着变） -->
    <div class="home-main">
      <div class="card home-graph">
      <div class="card-title">
        <span>◈</span> 知识图谱
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
        :focus-path="activeNotePath"
        @select="openGraphNode"
      />
      <div v-if="graphSel" class="alert info graph-sel" style="margin-top: 6px">
        已选中：<b>{{ graphSel.note_title || graphSel.label }}</b>
        <span class="dim">
          （{{ graphSel.kind === 'manuscript' ? '个人手稿' : graphSel.kind }}）
        </span>
      </div>
      </div>

      <FileTree
        class="home-tree"
        :notes="notes"
        :active-path="activeNotePath"
        @focus="onTreeFocus"
        @open="openNote"
        @moved="reloadAfterMove"
      />
    </div>

    <!-- 分区与标签管理（复用设置页原来的组件） -->
    <div v-if="showVocabMgr" class="vocab-mask" @click.self="showVocabMgr = false">
      <div class="vocab-modal">
        <div class="row" style="margin-bottom: 10px">
          <b>管理分区与标签</b>
          <div class="spacer" style="flex: 1"></div>
          <button class="btn ghost sm" @click="showVocabMgr = false">关闭</button>
        </div>
        <div class="vocab-body">
          <ManageVocab />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.del-btn {
  flex: none;
  color: var(--danger, #e5484d);
  opacity: 0.55;
}

.list-item:hover .del-btn {
  opacity: 1;
}

.vocab-mask {
  position: fixed;
  inset: 0;
  z-index: 80;
  background: rgb(0 0 0 / 55%);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
}

.vocab-modal {
  width: min(920px, 92vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  border: 1px solid var(--line, #33353d);
  background: var(--bg-panel, #1b1c22);
  padding: 16px 18px;
  box-shadow: 0 16px 50px rgb(0 0 0 / 55%);
}

.vocab-body {
  overflow: auto;
  min-height: 0;
}

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
.home-main {
  display: flex;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.home-tree {
  /* 首页右侧文件树宽度：300 → 120 → 150（与导航栏同宽，用户 2026-11-09）。 */
  width: 150px;
  flex: none;
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
