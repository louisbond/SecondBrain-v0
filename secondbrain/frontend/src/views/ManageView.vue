<script setup>
/**
 * 管理区：数据治理 + 全部设置（v37 与「设置」页合并）。
 *
 * 六个页签：
 *   分区与标签   分区树 + 标签词表（原两个 tab 合并，去掉重复的「全部分区」卡）
 *   解析模板     给 AI 的提示词模板（含折叠的「全局摘要 Prompt」）
 *   创作模板     给**人**的 Markdown 骨架（原创区「空模板」读这里，v37 新增）
 *   AI 与密钥    AI Provider 链 + API 密钥（原设置页 ai 组）
 *   采集与存储   Vault / 索引 / 语音转写（原设置页 storage 组）+ 平台能力
 *   维护与系统   自检诊断 / 清理缓存 / 备份（原管理区）+ 网络 / 自启 / 服务（原设置页 system 组）
 */
import { ref, onMounted, inject } from 'vue'
import { tagsApi, settingsApi, workbenchApi, partitionsApi, noteTemplatesApi } from '../api/client'
import SettingsView from './SettingsView.vue'

const toast = inject('toast')

const section = ref('vocab')

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

// 创作模板（原创区空模板，v37）
const noteTpls = ref([])
const ntEditing = ref(null)
const ntName = ref('')
const ntDesc = ref('')
const ntBody = ref('')
const ntTags = ref('')

// 解析模板
const templates = ref([])
const tplEditing = ref(null)
const tplName = ref('')
const tplBody = ref('')
const tplDesc = ref('')

// Prompt 模板
const promptTemplate = ref('')
const promptSaved = ref(false)

// 平台适配器能力（从设置区搬来的只读展示）
const adapters = ref([])
const supportLevels = {
  full: { label: '完整可用', cls: 'ok' },
  partial: { label: '部分可用', cls: 'warn' },
  unstable: { label: '不稳定', cls: 'danger' },
  manual: { label: '仅手动兜底', cls: '' }
}

// 诊断 / 维护
const diagnosis = ref(null)
const busy = ref(false)

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

async function loadTemplates() {
  try {
    const data = await workbenchApi.templates()
    templates.value = data.templates || []
  } catch {
    /* 忽略 */
  }
}

async function loadNoteTemplates() {
  try {
    const data = await noteTemplatesApi.list()
    noteTpls.value = data.templates || []
  } catch {
    /* 忽略：管理区其他部分照常可用 */
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

async function loadDiagnosis() {
  try {
    diagnosis.value = await workbenchApi.diagnose()
  } catch (e) {
    diagnosis.value = { ok: false, checks: [{ name: '诊断失败', ok: false, detail: e.message }] }
  }
}

/** 平台适配器能力（只读清单，来自 /api/settings 的 adapters 字段）。 */
async function loadAdapters() {
  try {
    const data = await settingsApi.get()
    adapters.value = data.adapters || []
  } catch {
    adapters.value = []
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

// ---- 维护 ----
async function cleanCache() {
  if (!confirm('清理缓存（音频与临时文件）？数据库与 md 不受影响。')) return
  busy.value = true
  try {
    const res = await workbenchApi.cleanCache()
    toast('缓存已清理', 'success')
    void res
  } catch (e) {
    toast('清理失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function doBackup() {
  busy.value = true
  try {
    const res = await workbenchApi.backup()
    toast(`备份完成：${res.path}`, 'success', 6000)
  } catch (e) {
    toast('备份失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    loadPartitions(),
    loadTags(),
    loadTemplates(),
    loadPrompt(),
    loadDiagnosis(),
    loadAdapters(),
    loadNoteTemplates()
  ])
})
</script>

<template>
  <div class="manage-page">
    <div class="row" style="margin-bottom: 14px; flex: 0 0 auto">
      <div class="tabs">
        <button class="tab" :class="{ active: section === 'vocab' }" @click="section = 'vocab'">
          分区与标签
        </button>
        <button
          class="tab"
          :class="{ active: section === 'templates' }"
          @click="section = 'templates'"
        >
          解析模板
        </button>
        <button
          class="tab"
          :class="{ active: section === 'note-templates' }"
          @click="section = 'note-templates'"
        >
          创作模板
        </button>
        <button class="tab" :class="{ active: section === 'ai' }" @click="section = 'ai'">
          AI 与密钥
        </button>
        <button class="tab" :class="{ active: section === 'storage' }" @click="section = 'storage'">
          采集与存储
        </button>
        <button class="tab" :class="{ active: section === 'ops' }" @click="section = 'ops'">
          维护与系统
        </button>
      </div>
      <div class="spacer" style="flex: 1"></div>
      <span class="dim" style="font-size: 11.5px">管理与设置已合并 · 同一页切换</span>
    </div>

    <!-- ==================== 分区与标签 ====================
         v37：原来「分区树」「标签」是两个 tab，各自一张卡
         （分区页右侧还有一张「全部分区」卡 —— 那是分区树的重复展示，删掉）。
         现在合成一页：左边树、右边标签词表。 -->
    <div v-if="section === 'vocab'" class="grid-2 vocab-grid">
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

    <!-- ==================== 解析模板 ====================
         给 **AI** 的提示词：按什么结构把原文整理成解析结果。
         原来「Prompt」是单独一个 tab，其实就是"默认摘要用哪套提示词"，
         和解析模板是一回事，折叠到同一页底部。 -->
    <div v-else-if="section === 'templates'" class="manage-stack">
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
        <summary>高级：AI 摘要的全局提示词（默认摘要用，不影响上面的模板）</summary>
        <div style="margin-top: 10px">
          <div class="card">
            <div class="card-title">
              <span>✎</span> 摘要 Prompt 模板
              <div class="spacer" style="flex: 1"></div>
              <button class="btn primary" @click="savePrompt">
                {{ promptSaved ? '已保存 ✓' : '保存' }}
              </button>
            </div>
            <div class="dim" style="margin-bottom: 8px; font-size: 11.5px">
              这里改的是「AI 摘要」用的提示词；「解析模板」在上一栏。
            </div>
            <textarea v-model="promptTemplate" rows="20" class="mono"></textarea>
          </div>
        </div>
      </details>
    </div>

    <!-- ==================== 创作模板（v37 新增） ====================
         用户："原创区的模板可在管理区添加"。
         与解析模板的区别：这个是给 **人** 的 Markdown 骨架。 -->
    <div v-else-if="section === 'note-templates'" class="card">
      <div class="card-title">
        <span>✎</span> 创作模板
        <span class="badge accent">{{ noteTpls.length }}</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="restoreNoteTpls">恢复内置</button>
        <button class="btn ghost sm" @click="startNewNoteTpl">新增模板</button>
      </div>
      <div class="dim" style="font-size: 11.5px; margin-bottom: 10px">
        原创区「空模板」那排按钮读的就是这里。正文是 Markdown 骨架，可用
        <code>&#123;&#123;date}}</code> / <code>&#123;&#123;time}}</code> /
        <code>&#123;&#123;title}}</code> 占位符（应用模板时替换成真实值）；
        <strong>别写 YAML 头</strong>（系统会自动生成，写两遍 Obsidian 打不开）。
      </div>

      <div v-if="ntEditing" class="nt-editor">
        <div class="row">
          <input
            v-model="ntName"
            type="text"
            placeholder="模板名（显示在原创区按钮上）"
            style="flex: 1"
          />
          <input
            v-model="ntTags"
            type="text"
            placeholder="建议标签，逗号分隔"
            style="width: 220px"
          />
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

    <!-- ==================== AI 与密钥（原设置页 AI 组） ==================== -->
    <SettingsView v-else-if="section === 'ai'" embedded group="ai" />

    <!-- ==================== 采集与存储（原设置页 storage 组 + 平台能力） ==================== -->
    <div v-else-if="section === 'storage'" class="manage-stack">
      <SettingsView embedded group="storage" />
      <div class="card">
        <div class="card-title"><span>📡</span> 平台适配器能力</div>
        <div class="list-item" v-for="a in adapters" :key="a.name">
          <div class="grow">
            <div class="title">{{ a.label }}</div>
            <div class="sub">
              <span v-if="a.supports_subtitle">字幕 ✓</span>
              <span v-if="a.supports_audio"> · 音频 ✓</span>
            </div>
          </div>
          <span class="badge" :class="supportLevels[a.support_level]?.cls">
            {{ supportLevels[a.support_level]?.label || a.support_level }}
          </span>
        </div>
        <div v-if="!adapters.length" class="dim">没有可用的适配器</div>
      </div>
    </div>

    <!-- ==================== 维护与系统 ====================
         自检诊断 / 清理缓存 / 备份（原管理区）+ 网络 / 自启 / 服务（原设置页 system 组） -->
    <div v-else class="grid-2" style="grid-template-columns: 1fr 1fr; align-items: start">
      <div class="card">
        <div class="card-title">
          <span>🩺</span> 自检诊断
          <div class="spacer" style="flex: 1"></div>
          <button class="btn ghost sm" @click="loadDiagnosis">重新检测</button>
        </div>
        <div v-if="!diagnosis" class="dim">检测中…</div>
        <div v-for="(c, i) in diagnosis?.checks || []" :key="i" class="list-item">
          <span class="badge" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? 'OK' : '异常' }}</span>
          <div class="grow">
            <div class="title">{{ c.name }}</div>
            <div class="sub mono">{{ c.detail }}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>🧹</span> 维护</div>
        <div class="list-item">
          <div class="grow">
            <div class="title">清理缓存</div>
            <div class="sub">清掉音频与临时文件；数据库与 md 不受影响</div>
          </div>
          <button class="btn" :disabled="busy" @click="cleanCache">清理</button>
        </div>
        <div class="list-item">
          <div class="grow">
            <div class="title">导出备份</div>
            <div class="sub">把数据库复制一份到 data/backups</div>
          </div>
          <button class="btn" :disabled="busy" @click="doBackup">备份</button>
        </div>
        <div class="alert info" style="margin-top: 10px">
          md 是唯一真相源：即使数据库全丢，也能用「重建索引」从你的库恢复。
        </div>
      </div>

      <!-- 系统（原设置页 system 组：网络 / 开机自启 / 服务），跨两列 -->
      <div style="grid-column: 1/-1">
        <SettingsView embedded group="system" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
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

/* v35：管理区也填满窗口（用户：每个区最好都这样）。
   原来内容只占 36% 高度，下面一大片空白。 */
.manage-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 各 section 的内容区吃掉剩余高度，卡片内部自己滚 */
.manage-page > .grid-2,
.manage-page > .card,
.manage-page > div:not(.row) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 分区 + 标签并排（v37：原来两个 tab，现在一页两栏）。
   两张卡都**撑满高度**（用户的一贯要求：每个区正好填满窗口），
   内容多了在卡片内部滚，而不是整页滚。 */
.vocab-grid {
  grid-template-columns: minmax(0, 1fr) 380px;
  align-items: stretch;
}
.vocab-grid > .card {
  min-height: 0;
  overflow-y: auto;
}

/* 竖向堆叠（设置面板 + 只读卡片的组合） */
.manage-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  overflow-y: auto;
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

/* v35：管理区也填满窗口（用户：每个区最好都这样）。
   原来内容只占 36% 高度，下面一大片空白。 */
.manage-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 各 section 的内容区吃掉剩余高度，卡片内部自己滚 */
.manage-page > .grid-2,
.manage-page > .card,
.manage-page > div:not(.row) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 分区 + 标签并排（v37：原来两个 tab，现在一页两栏）。
   两张卡都**撑满高度**（用户的一贯要求：每个区正好填满窗口），
   内容多了在卡片内部滚，而不是整页滚。 */
.vocab-grid {
  grid-template-columns: minmax(0, 1fr) 380px;
  align-items: stretch;
}
.vocab-grid > .card {
  min-height: 0;
  overflow-y: auto;
}

/* 竖向堆叠（设置面板 + 只读卡片的组合） */
.manage-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  overflow-y: auto;
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
