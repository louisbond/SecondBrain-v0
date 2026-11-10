<script setup>
/**
 * 设置 → 创作模板（v41 从 ManageView.vue 拆出）。
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
import { noteTemplatesApi } from '../../api/client'

const toast = inject('toast')

// 创作模板（原创区空模板，v37）
const noteTpls = ref([])
const ntEditing = ref(null)
const ntName = ref('')
const ntDesc = ref('')
const ntBody = ref('')
const ntTags = ref('')

async function loadNoteTemplates() {
  try {
    const data = await noteTemplatesApi.list()
    noteTpls.value = data.templates || []
  } catch {
    /* 忽略：管理区其他部分照常可用 */
  }
}

// ---- 创作模板 ----
function startNewNoteTpl() {
  ntEditing.value = 'new'
  ntName.value = ''
  ntDesc.value = ''
  ntTags.value = ''
  ntBody.value = '# {{ title }}\n\n'
}

function startEditNoteTpl(t) {
  ntEditing.value = t.id
  ntName.value = t.name
  ntDesc.value = t.description || ''
  ntTags.value = (t.suggest_tags || []).join(', ')
  ntBody.value = t.body || ''
}

/** 逗号 / 中文逗号 / 空格 都能当分隔符（用户不会只敲英文逗号）。 */
function splitTags(raw) {
  return String(raw || '')
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean)
}

async function saveNoteTpl() {
  if (!ntName.value.trim()) {
    toast('模板名不能为空', 'warn')
    return
  }
  const payload = {
    name: ntName.value.trim(),
    description: ntDesc.value,
    body: ntBody.value,
    suggest_tags: splitTags(ntTags.value)
  }
  try {
    if (ntEditing.value === 'new') {
      await noteTemplatesApi.create(payload)
      toast('创作模板已新增', 'success')
    } else {
      await noteTemplatesApi.update(ntEditing.value, payload)
      toast('创作模板已更新', 'success')
    }
    ntEditing.value = null
    await loadNoteTemplates()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  }
}

async function removeNoteTpl(t) {
  if (!confirm(`删除创作模板「${t.name}」？原创区的按钮会一起消失。`)) return
  try {
    await noteTemplatesApi.remove(t.id)
    toast('已删除（可用「恢复内置」拿回内置模板）', 'success')
    await loadNoteTemplates()
  } catch (e) {
    toast('删除失败：' + e.message, 'error')
  }
}

async function restoreNoteTpls() {
  try {
    const r = await noteTemplatesApi.restoreDefaults()
    toast(r.added ? `已补回 ${r.added} 个内置模板` : '内置模板都在，无需恢复', 'info')
    await loadNoteTemplates()
  } catch (e) {
    toast('恢复失败：' + e.message, 'error')
  }
}

onMounted(loadNoteTemplates)
</script>

<template>
  <div class="card manage-section">
    <div class="card-title">
      <span>✎</span> 创作模板
      <span class="badge accent">{{ noteTpls.length }}</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" @click="restoreNoteTpls">恢复内置</button>
      <button class="btn ghost sm" @click="startNewNoteTpl">新增模板</button>
    </div>
    <div v-if="ntEditing" class="nt-editor">
      <div class="row">
        <input
          v-model="ntName"
          type="text"
          placeholder="模板名（显示在原创区按钮上）"
          style="flex: 1"
        />
        <input v-model="ntTags" type="text" placeholder="建议标签，逗号分隔" style="width: 220px" />
      </div>
      <input
        v-model="ntDesc"
        type="text"
        placeholder="一句话说明（列表里显示）"
        style="margin-top: 8px"
      />
      <textarea
        v-model="ntBody"
        rows="10"
        class="mono"
        style="margin-top: 8px"
        placeholder="模板正文（Markdown）"
      ></textarea>
      <div class="row" style="margin-top: 8px">
        <button class="btn primary" @click="saveNoteTpl">保存</button>
        <button class="btn ghost" @click="ntEditing = null">取消</button>
      </div>
    </div>

    <div v-for="t in noteTpls" :key="t.id" class="list-item">
      <div class="grow">
        <div class="title">
          {{ t.name }}
          <span v-if="t.source === 'builtin'" class="badge">内置</span>
        </div>
        <div class="sub">{{ t.description || '（无说明）' }}</div>
      </div>
      <span class="dim mono" style="font-size: 11px">{{ (t.body || '').length }} 字</span>
      <span v-for="tg in t.suggest_tags" :key="tg" class="badge">{{ tg }}</span>
      <button class="btn ghost sm" @click="startEditNoteTpl(t)">编辑</button>
      <button class="btn ghost sm" @click="removeNoteTpl(t)">删除</button>
    </div>
    <div v-if="!noteTpls.length" class="dim">
      还没有模板。点「新增模板」，或「恢复内置」把默认那几套拿回来。
    </div>
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 创作模板的编辑区 */
.nt-editor {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  background: var(--bg-panel);
}
.nt-editor textarea {
  font-size: 12.5px;
  line-height: 1.6;
}
</style>
