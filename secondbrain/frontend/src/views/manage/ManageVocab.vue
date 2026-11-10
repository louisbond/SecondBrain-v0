<script setup>
/**
 * 设置 → 分区与标签（v41 从 ManageView.vue 拆出）。
 *
 * 拆分前 ManageView.vue 有 952 行：6 个页签的状态、API 调用、模板全挤在
 * 一个 setup 里，改「创作模板」要在 900 行里找对应变量。
 *
 * 现在每个页签是一个自治组件：自己持有状态、自己 onMounted 拉数据。
 * 父组件只管 tab 切换 + KeepAlive 缓存。
 * 共享的只有 inject('toast')（App.vue 提供），不需要 props 穿透。
 *
 * 注意：本文件的模板/逻辑是从原文件**逐行切片**搬来的，未改业务逻辑。
 */
import { ref, onMounted, inject } from 'vue'
import { tagsApi, partitionsApi } from '../../api/client'

const toast = inject('toast')

// 分区树
const partTree = ref([])
const partFlat = ref([])
const expanded = ref({})
const newPartition = ref('')
const newPartParent = ref(null)

// 标签
const tagList = ref([])
const newTag = ref('')
const merging = ref(false)
const mergeSrc = ref('')
const mergeDst = ref('')

async function loadPartitions() {
  try {
    const data = await partitionsApi.list()
    partTree.value = data.tree || []
    partFlat.value = data.partitions || []
  } catch (e) {
    toast('分区加载失败：' + e.message, 'error')
  }
}

async function loadTags() {
  try {
    const data = await tagsApi.vocabulary()
    tagList.value = data.tags || []
  } catch (e) {
    toast('标签加载失败：' + e.message, 'error')
  }
}

function toggleExpand(path) {
  expanded.value[path] = !expanded.value[path]
}

// ---- 分区操作 ----
async function addPartition() {
  const name = newPartition.value.trim()
  if (!name) return
  try {
    await partitionsApi.create(name, newPartParent.value)
    toast(`已新增分区「${name}」`, 'success')
    newPartition.value = ''
    await loadPartitions()
  } catch (e) {
    toast('新增失败：' + e.message, 'error')
  }
}

async function removePartition(node) {
  if (!confirm(`删除分区「${node.path}」？子分区会上移一级。`)) return
  try {
    await partitionsApi.remove(node.id)
    toast('分区已删除', 'success')
    await loadPartitions()
  } catch (e) {
    toast('删除失败：' + e.message, 'error')
  }
}

async function renamePartition(node) {
  const name = prompt('新的分区名', node.name)
  if (!name || name === node.name) return
  try {
    await partitionsApi.update(node.id, { name })
    toast('已改名（子树路径同步更新）', 'success')
    await loadPartitions()
  } catch (e) {
    toast('改名失败：' + e.message, 'error')
  }
}

async function movePartition(node) {
  const target = prompt('移动到哪个父分区？留空表示移到顶层', node.parent_id ? '' : '')
  const parentId = target ? Number(target) : null
  try {
    await partitionsApi.update(node.id, { parent_id: parentId })
    toast('已移动', 'success')
    await loadPartitions()
  } catch (e) {
    toast('移动失败：' + e.message, 'error')
  }
}

async function seedPartitions() {
  if (!confirm('用 vault 目录树补齐分区（已存在的不动）？')) return
  try {
    const res = await partitionsApi.seedFromVault()
    toast(`新增 ${res.created} 个分区`, 'success')
    await loadPartitions()
  } catch (e) {
    toast('导入失败：' + e.message, 'error')
  }
}

// ---- 标签操作 ----
async function addTag() {
  const name = newTag.value.trim()
  if (!name) return
  try {
    await tagsApi.add(name)
    toast('已添加标签', 'success')
    newTag.value = ''
    await loadTags()
  } catch (e) {
    toast('添加失败：' + e.message, 'error')
  }
}

async function deactivateTag(t) {
  if (!confirm(`停用标签「${t.name}」？`)) return
  try {
    await tagsApi.deactivate(t.name)
    toast('已停用', 'success')
    await loadTags()
  } catch (e) {
    toast('停用失败：' + e.message, 'error')
  }
}

async function doMerge() {
  if (!mergeSrc.value || !mergeDst.value) {
    toast('请选择源与目标标签', 'warn')
    return
  }
  try {
    const res = await tagsApi.merge(mergeSrc.value, mergeDst.value)
    toast(`已合并 ${res.affected || 0} 篇笔记`, 'success')
    merging.value = false
    mergeSrc.value = ''
    mergeDst.value = ''
    await loadTags()
  } catch (e) {
    toast('合并失败：' + e.message, 'error')
  }
}

onMounted(() => Promise.all([loadPartitions(), loadTags()]))
</script>

<template>
  <div class="grid-2 vocab-grid manage-section">
    <div class="card">
      <div class="card-title">
        <span>▤</span> 分区树
        <span class="badge accent">{{ partFlat.length }}</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="seedPartitions">从目录树导入</button>
      </div>

      <div class="row" style="margin-bottom: 10px">
        <input
          v-model="newPartition"
          type="text"
          placeholder="新分区名，回车添加"
          style="flex: 1"
          @keydown.enter.prevent="addPartition"
        />
        <select v-model="newPartParent" style="width: auto">
          <option :value="null">顶层</option>
          <option v-for="p in partFlat" :key="p.id" :value="p.id">
            {{ p.path }}
          </option>
        </select>
        <button class="btn" @click="addPartition">添加</button>
      </div>

      <div class="tree">
        <div v-for="node in partTree" :key="node.id" class="tree-node">
          <div class="tree-row">
            <button class="tree-toggle" @click="toggleExpand(node.path)">
              {{ expanded[node.path] ? '▾' : '▸' }}
            </button>
            <span class="tree-label">▤ {{ node.name }}</span>
            <span class="dim mono" style="margin-left: auto; font-size: 11px">
              {{ node.count }} 篇
            </span>
            <button class="btn ghost sm" @click="renamePartition(node)">改名</button>
            <button class="btn ghost sm" @click="movePartition(node)">移动</button>
            <button class="btn ghost sm" @click="removePartition(node)">删除</button>
          </div>
          <div v-if="expanded[node.path]" class="tree-children">
            <div
              v-for="child in node.children"
              :key="child.id"
              class="tree-row"
              style="padding-left: 16px"
            >
              <span class="tree-label">· {{ child.name }}</span>
              <span class="dim mono" style="margin-left: auto; font-size: 11px">
                {{ child.count }} 篇
              </span>
              <button class="btn ghost sm" @click="renamePartition(child)">改名</button>
              <button class="btn ghost sm" @click="removePartition(child)">删除</button>
            </div>
          </div>
        </div>
      </div>
      <div v-if="!partTree.length" class="dim">还没有分区——先「从目录树导入」或手动添加。</div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>#</span> 标签词表
        <span class="badge accent">{{ tagList.length }}</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="merging = !merging">合并工具</button>
      </div>

      <div class="row" style="margin-bottom: 10px">
        <input
          v-model="newTag"
          type="text"
          placeholder="新标签，回车添加"
          style="flex: 1"
          @keydown.enter.prevent="addTag"
        />
        <button class="btn" @click="addTag">添加</button>
      </div>

      <div v-if="merging" class="alert info" style="margin-bottom: 10px">
        <div class="row wrap">
          <select v-model="mergeSrc" style="width: auto">
            <option value="">源标签</option>
            <option v-for="t in tagList" :key="t.id" :value="t.name">{{ t.name }}</option>
          </select>
          <span>→</span>
          <input
            v-model="mergeDst"
            type="text"
            placeholder="目标标签名（不存在会自动创建）"
            style="flex: 1"
          />
          <button class="btn" @click="doMerge">合并</button>
        </div>
      </div>

      <div class="row wrap">
        <span v-for="t in tagList" :key="t.id" class="check-chip">
          {{ t.name }}
          <span class="dim mono" style="margin-left: 4px">{{ t.count }}</span>
          <button class="btn ghost sm" style="padding: 0 4px" @click="deactivateTag(t)">×</button>
        </span>
      </div>
      <div v-if="!tagList.length" class="dim">还没有标签</div>
    </div>
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.tree-node {
  margin: 2px 0;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px;
  border-radius: 6px;
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
  font-size: 13px;
}
.tree-children {
  border-left: 1px solid var(--line);
  margin-left: 8px;
}

/* 两栏都撑满高度：内容多了在卡片内部滚，不整页滚 */
.vocab-grid {
  grid-template-columns: minmax(0, 1fr) 380px;
  align-items: stretch;
}
.vocab-grid > .card {
  min-height: 0;
  overflow-y: auto;
}
</style>
