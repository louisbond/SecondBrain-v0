<script setup>
/**
 * 文件树（v0.10.5）：按知识库真实目录结构展示笔记。
 *
 * 交互：
 *   · 点目录 → 折叠/展开
 *   · 点文件 → 打开笔记
 *   · 拖 .md 到目录 → 移动文件（POST /api/notes/move），成功后再通知父组件刷新图谱
 *
 * 为什么只做两层：vault 实际结构就是「根目录 + 一级目录 + md」，
 * 递归组件在这里是过度设计，后续真出现三级目录再抽 TreeItem。
 */
import { computed, inject, nextTick, ref, watch } from 'vue'
import { notesApi } from '../api/client'

const props = defineProps({
  notes: { type: Array, default: () => [] },
  activePath: { type: String, default: '' }
})
const emit = defineEmits(['open', 'focus', 'moved'])

const toast = inject('toast')
const collapsed = ref({})
const dragging = ref(null)
const dropTarget = ref('')
const bodyEl = ref(null)

const tree = computed(() => {
  const root = { files: [], dirs: {} }
  for (const n of props.notes || []) {
    const rel = String(n.rel_path || '').replace(/\\/g, '/')
    const parts = rel.split('/').filter(Boolean)
    if (!parts.length) continue
    const fname = parts.pop()
    const item = { ...n, name: fname }
    if (!parts.length) {
      root.files.push(item)
      continue
    }
    const dirName = parts.join('/')
    if (!root.dirs[dirName]) root.dirs[dirName] = { name: dirName, files: [] }
    root.dirs[dirName].files.push(item)
  }
  const dirs = Object.values(root.dirs).sort((a, b) => a.name.localeCompare(b.name))
  dirs.forEach((d) => d.files.sort((a, b) => (a.name > b.name ? 1 : -1)))
  root.files.sort((a, b) => (a.name > b.name ? 1 : -1))
  return { dirs, files: root.files }
})

// 图谱那边点了节点 / 或外部指定了当前笔记 → 展开目录并滚到可见
watch(
  () => props.activePath,
  async (p) => {
    if (!p) return
    const hit = (props.notes || []).find((n) => n.note_path === p)
    const rel = String(hit?.rel_path || '').replace(/\\/g, '/')
    const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : ''
    if (dir && collapsed.value[dir]) {
      collapsed.value = { ...collapsed.value, [dir]: false }
    }
    await nextTick()
    const el = bodyEl.value?.querySelector(`[data-path="${CSS.escape(p)}"]`)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
)

function toggle(path) {
  collapsed.value = { ...collapsed.value, [path]: !collapsed.value[path] }
}

function onDragStart(f) {
  dragging.value = f
}

function onDrop(targetDir) {
  dropTarget.value = ''
  const f = dragging.value
  dragging.value = null
  if (!f) return
  const base = String(f.note_path || '').replace(/\\/g, '/')
  const fromDir = base.slice(0, base.lastIndexOf('/'))
  const toDir = targetDir === null ? '' : targetDir
  if (targetDir !== null && fromDir.endsWith('/' + targetDir)) return // 原地拖回，忽略
  moveTo(f, toDir)
}

async function moveTo(f, relDir) {
  const root = rootPath.value
  if (!root) {
    toast('拿不到知识库路径，无法移动', 'error')
    return
  }
  const toDir = relDir ? `${root}/${relDir}` : root
  try {
    const r = await notesApi.move(f.note_path, toDir)
    toast(r.moved ? `已移动到「${relDir || '根目录'}」` : '位置没变', 'success')
    emit('moved', r)
  } catch (e) {
    toast('移动失败：' + (e.message || e), 'error')
  }
}

/** 知识库根：由任一 note_path 与 rel_path 反推（去重后取公共前缀） */
const rootPath = computed(() => {
  for (const n of props.notes || []) {
    const abs = String(n.note_path || '').replace(/\\/g, '/')
    const rel = String(n.rel_path || '').replace(/\\/g, '/')
    if (abs.endsWith(rel)) return abs.slice(0, abs.length - rel.length - 1)
  }
  return ''
})
</script>

<template>
  <div class="file-tree">
    <div class="ft-head">
      <span>📁 知识库</span>
      <span class="dim ft-count">{{ (notes || []).length }}</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" title="刷新" @click="emit('moved', null)">⟳</button>
    </div>

    <div ref="bodyEl" class="ft-body">
      <!-- 目录 -->
      <div v-for="d in tree.dirs" :key="d.name" class="ft-dir">
        <div
          class="ft-row ft-dir-row"
          :class="{ 'drop-hint': dropTarget === d.name }"
          @click="toggle(d.name)"
          @dragover.prevent="dropTarget = d.name"
          @dragleave="dropTarget = ''"
          @drop.prevent="onDrop(d.name)"
        >
          <span class="ft-caret">{{ collapsed[d.name] ? '▸' : '▾' }}</span>
          <span class="ft-icon">📁</span>
          <span class="ft-name">{{ d.name }}</span>
          <span class="dim ft-num">{{ d.files.length }}</span>
        </div>
        <div v-show="!collapsed[d.name]" class="ft-children">
          <div
            v-for="f in d.files"
            :key="f.note_path"
            class="ft-row ft-file"
            :class="{ active: f.note_path === activePath }"
            draggable="true"
            :data-path="f.note_path"
            :title="f.rel_path + '（单击聚焦图谱 · 双击打开）'"
            @dragstart="onDragStart(f)"
            @click="emit('focus', f.note_path)"
            @dblclick="emit('open', f.note_path)"
          >
            <span class="ft-icon">📄</span>
            <span class="ft-name">{{ f.title || f.name }}</span>
          </div>
          <div v-if="!d.files.length" class="ft-empty">（空目录）</div>
        </div>
      </div>

      <!-- 根目录文件 -->
      <div
        class="ft-root-zone"
        :class="{ 'drop-hint': dropTarget === '__root__' }"
        @dragover.prevent="dropTarget = '__root__'"
        @dragleave="dropTarget = ''"
        @drop.prevent="onDrop(null)"
      >
        <div v-for="f in tree.files" :key="f.note_path" class="ft-row ft-file"
          :class="{ active: f.note_path === activePath }"
          draggable="true" :data-path="f.note_path"
          :title="f.rel_path + '（单击聚焦图谱 · 双击打开）'"
          @dragstart="onDragStart(f)"
          @click="emit('focus', f.note_path)"
          @dblclick="emit('open', f.note_path)">
          <span class="ft-icon">📄</span>
          <span class="ft-name">{{ f.title || f.name }}</span>
        </div>
        <div v-if="!tree.files.length && !tree.dirs.length" class="ft-empty">
          还没有笔记
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-tree {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  border: 1px solid var(--line, #33353d);
  border-radius: 12px;
  background: var(--bg-panel, #1b1c22);
  overflow: hidden;
}

.ft-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  font-size: 12.5px;
  font-weight: 600;
  border-bottom: 1px solid var(--line, #33353d);
  flex: none;
}

.ft-count {
  font-size: 11px;
  font-weight: 400;
}

.ft-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 6px 6px 12px;
}

.ft-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 6px;
  border-radius: 6px;
  font-size: 12.5px;
  cursor: pointer;
  user-select: none;
}

.ft-row:hover {
  background: var(--bg-hover, #262830);
}

.ft-dir-row {
  font-weight: 600;
}

.ft-caret {
  width: 12px;
  color: var(--text-dim, #9a9daa);
  font-size: 11px;
  flex: none;
}

.ft-icon {
  flex: none;
  font-size: 12px;
}

.ft-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ft-num {
  font-size: 11px;
  flex: none;
}

.ft-children {
  margin-left: 14px;
}

.ft-file.active {
  background: color-mix(in srgb, var(--ai, #7c6cf0) 22%, transparent);
  color: var(--text, #e6e7ea);
}

.ft-file:active {
  cursor: grabbing;
}

.drop-hint {
  outline: 1px dashed var(--ai, #7c6cf0);
  background: color-mix(in srgb, var(--ai, #7c6cf0) 12%, transparent);
}

.ft-empty {
  font-size: 11.5px;
  color: var(--text-dim, #9a9daa);
  padding: 4px 8px;
}

.ft-root-zone {
  margin-top: 4px;
  border-top: 1px dashed var(--line, #33353d);
  padding-top: 4px;
  min-height: 24px;
}
</style>
