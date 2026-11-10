<script setup>
/**
 * 设置 → 解析模板（v41 从 ManageView.vue 拆出）。
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
import { settingsApi, workbenchApi } from '../../api/client'

const toast = inject('toast')

// 解析模板
const templates = ref([])
const tplEditing = ref(null)
const tplName = ref('')
const tplBody = ref('')
const tplDesc = ref('')

// Prompt 模板
const promptTemplate = ref('')
const promptSaved = ref(false)

async function loadTemplates() {
  try {
    const data = await workbenchApi.templates()
    templates.value = data.templates || []
  } catch {
    /* 忽略 */
  }
}

async function loadPrompt() {
  try {
    const data = await settingsApi.getPrompt()
    promptTemplate.value = data.template || ''
  } catch {
    /* 忽略 */
  }
}

// ---- 解析模板 ----
function startNewTpl() {
  tplEditing.value = 'new'
  tplName.value = ''
  tplDesc.value = ''
  tplBody.value = ''
}

function startEditTpl(t) {
  tplEditing.value = t.id
  tplName.value = t.name
  tplDesc.value = t.description || ''
  tplBody.value = t.body || ''
}

async function saveTpl() {
  if (!tplName.value.trim() || !tplBody.value.trim()) {
    toast('模板名与内容不能为空', 'warn')
    return
  }
  try {
    if (tplEditing.value === 'new') {
      await workbenchApi.createTemplate({
        name: tplName.value,
        body: tplBody.value,
        description: tplDesc.value
      })
      toast('模板已新增', 'success')
    } else {
      await workbenchApi.updateTemplate(tplEditing.value, {
        name: tplName.value,
        body: tplBody.value,
        description: tplDesc.value
      })
      toast('模板已更新', 'success')
    }
    tplEditing.value = null
    await loadTemplates()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  }
}

async function removeTpl(t) {
  if (!confirm(`删除模板「${t.name}」？`)) return
  try {
    await workbenchApi.deleteTemplate(t.id)
    toast('模板已删除', 'success')
    await loadTemplates()
  } catch (e) {
    toast('删除失败：' + e.message, 'error')
  }
}

// ---- Prompt 模板 ----
async function savePrompt() {
  try {
    await settingsApi.savePrompt(promptTemplate.value)
    toast('Prompt 模板已保存', 'success')
    promptSaved.value = true
    setTimeout(() => (promptSaved.value = false), 2000)
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  }
}

onMounted(() => Promise.all([loadTemplates(), loadPrompt()]))
</script>

<template>
  <div class="manage-stack manage-section">
    <div class="card">
      <div class="card-title">
        <span>☰</span> 解析模板
        <span class="badge accent">{{ templates.length }}</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="startNewTpl">新增模板</button>
      </div>

      <div v-if="tplEditing" style="margin-bottom: 12px">
        <input v-model="tplName" type="text" placeholder="模板名" style="margin-bottom: 8px" />
        <input
          v-model="tplDesc"
          type="text"
          placeholder="一句话描述（供 AI 匹配时参考）"
          style="margin-bottom: 8px"
        />
        <textarea
          v-model="tplBody"
          rows="5"
          placeholder="模板正文：告诉 AI 按什么结构输出"
        ></textarea>
        <div class="row" style="margin-top: 8px">
          <button class="btn primary" @click="saveTpl">保存</button>
          <button class="btn ghost" @click="tplEditing = null">取消</button>
        </div>
      </div>

      <div v-for="t in templates" :key="t.id" class="list-item">
        <div class="grow">
          <div class="title">{{ t.name }}</div>
          <div class="sub">{{ t.description || '（无描述）' }}</div>
        </div>
        <span class="badge">{{
          t.source === 'ai' ? 'AI' : t.source === 'system' ? '内置' : '手动'
        }}</span>
        <button class="btn ghost sm" @click="startEditTpl(t)">编辑</button>
        <button class="btn ghost sm" @click="removeTpl(t)">删除</button>
      </div>
    </div>

    <details class="prompt-box">
      <summary>高级：AI 摘要的全局提示词</summary>
      <div style="margin-top: 10px">
        <div class="card">
          <div class="card-title">
            <span>✎</span> 摘要 Prompt 模板
            <div class="spacer" style="flex: 1"></div>
            <button class="btn primary" @click="savePrompt">
              {{ promptSaved ? '已保存 ✓' : '保存' }}
            </button>
          </div>
          <textarea v-model="promptTemplate" rows="20" class="mono"></textarea>
        </div>
      </div>
    </details>
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.manage-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 折叠的「全局摘要 Prompt」 */
.prompt-box {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 14px;
  background: var(--bg-panel);
}
.prompt-box summary {
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-muted);
  list-style: none;
}
.prompt-box summary::-webkit-details-marker {
  display: none;
}
.prompt-box summary::before {
  content: '▸ ';
  color: var(--text-dim);
}
.prompt-box[open] summary::before {
  content: '▾ ';
}
</style>
