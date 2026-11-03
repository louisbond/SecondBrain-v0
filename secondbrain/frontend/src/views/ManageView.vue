<script setup>
/**
 * 管理区：设置各项数据（原功能区 + 整合区的数据管理职责）。
 *
 * 分区树治理 / 标签治理 / 解析模板 / Prompt 模板 /
 * 检索参数 / 缓存清理 / 备份导出 / 自检诊断 / 平台能力
 */
import { ref, onMounted, inject } from 'vue'
import {
  tagsApi,
  settingsApi,
  workbenchApi,
  partitionsApi
} from '../api/client'

const toast = inject('toast')

const section = ref('partitions')

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
  } catch (e) {
    /* 忽略 */
  }
}

async function loadPrompt() {
  try {
    const data = await settingsApi.getPrompt()
    promptTemplate.value = data.template || ''
  } catch (e) {
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
  } catch (e) {
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
  const target = prompt(
    '移动到哪个父分区？留空表示移到顶层',
    node.parent_id ? '' : ''
  )
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
    loadAdapters()
  ])
})
</script>

<template>
  <div class="manage-page">
    <div class="row" style="margin-bottom:14px;flex:0 0 auto">
      <div class="tabs">
        <button class="tab" :class="{ active: section === 'partitions' }" @click="section = 'partitions'">
          分区树
        </button>
        <button class="tab" :class="{ active: section === 'tags' }" @click="section = 'tags'">
          标签
        </button>
        <button class="tab" :class="{ active: section === 'templates' }" @click="section = 'templates'">
          解析模板
        </button>
        <button class="tab" :class="{ active: section === 'prompt' }" @click="section = 'prompt'">
          Prompt
        </button>
        <button class="tab" :class="{ active: section === 'maintenance' }" @click="section = 'maintenance'">
          维护与诊断
        </button>
        <button class="tab" :class="{ active: section === 'adapters' }" @click="section = 'adapters'">
          平台能力
        </button>
      </div>
    </div>

    <!-- ============ 分区树 ============ -->
    <div v-if="section === 'partitions'" class="grid-2" style="grid-template-columns:1fr 320px;align-items:start">
      <div class="card">
        <div class="card-title">
          <span>▤</span> 分区树
          <span class="badge accent">{{ partFlat.length }}</span>
          <div class="spacer" style="flex:1"></div>
          <button class="btn ghost sm" @click="seedPartitions">从目录树导入</button>
        </div>

        <div class="row" style="margin-bottom:10px">
          <input
            v-model="newPartition"
            type="text"
            placeholder="新分区名，回车添加"
            style="flex:1"
            @keydown.enter.prevent="addPartition"
          />
          <select v-model="newPartParent" style="width:auto">
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
              <span class="dim mono" style="margin-left:auto;font-size:11px">
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
                style="padding-left:16px"
              >
                <span class="tree-label">· {{ child.name }}</span>
                <span class="dim mono" style="margin-left:auto;font-size:11px">
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
        <div class="card-title"><span>≡</span> 全部分区</div>
        <div v-for="p in partFlat" :key="p.id" class="list-item">
          <div class="grow">
            <div class="title">{{ p.path }}</div>
            <div class="sub">第 {{ p.level }} 层 · {{ p.count }} 篇</div>
          </div>
        </div>
        <div v-if="!partFlat.length" class="dim">（空）</div>
      </div>
    </div>

    <!-- ============ 标签 ============ -->
    <div v-else-if="section === 'tags'" class="card">
      <div class="card-title">
        <span>#</span> 标签词表
        <span class="badge accent">{{ tagList.length }}</span>
        <div class="spacer" style="flex:1"></div>
        <button class="btn ghost sm" @click="merging = !merging">合并工具</button>
      </div>

      <div class="row" style="margin-bottom:10px">
        <input
          v-model="newTag"
          type="text"
          placeholder="新标签，回车添加"
          style="flex:1"
          @keydown.enter.prevent="addTag"
        />
        <button class="btn" @click="addTag">添加</button>
      </div>

      <div v-if="merging" class="alert info" style="margin-bottom:10px">
        <div class="row wrap">
          <select v-model="mergeSrc" style="width:auto">
            <option value="">源标签</option>
            <option v-for="t in tagList" :key="t.id" :value="t.name">{{ t.name }}</option>
          </select>
          <span>→</span>
          <input
            v-model="mergeDst"
            type="text"
            placeholder="目标标签名（不存在会自动创建）"
            style="flex:1"
          />
          <button class="btn" @click="doMerge">合并</button>
        </div>
      </div>

      <div class="row wrap">
        <span v-for="t in tagList" :key="t.id" class="check-chip">
          {{ t.name }}
          <span class="dim mono" style="margin-left:4px">{{ t.count }}</span>
          <button class="btn ghost sm" style="padding:0 4px" @click="deactivateTag(t)">×</button>
        </span>
      </div>
      <div v-if="!tagList.length" class="dim">还没有标签</div>
    </div>

    <!-- ============ 解析模板 ============ -->
    <div v-else-if="section === 'templates'" class="card">
      <div class="card-title">
        <span>☰</span> 解析模板
        <span class="badge accent">{{ templates.length }}</span>
        <div class="spacer" style="flex:1"></div>
        <button class="btn ghost sm" @click="startNewTpl">新增模板</button>
      </div>

      <div v-if="tplEditing" style="margin-bottom:12px">
        <input v-model="tplName" type="text" placeholder="模板名" style="margin-bottom:8px" />
        <input
          v-model="tplDesc"
          type="text"
          placeholder="一句话描述（供 AI 匹配时参考）"
          style="margin-bottom:8px"
        />
        <textarea v-model="tplBody" rows="5" placeholder="模板正文：告诉 AI 按什么结构输出"></textarea>
        <div class="row" style="margin-top:8px">
          <button class="btn primary" @click="saveTpl">保存</button>
          <button class="btn ghost" @click="tplEditing = null">取消</button>
        </div>
      </div>

      <div v-for="t in templates" :key="t.id" class="list-item">
        <div class="grow">
          <div class="title">{{ t.name }}</div>
          <div class="sub">{{ t.description || '（无描述）' }}</div>
        </div>
        <span class="badge">{{ t.source === 'ai' ? 'AI' : t.source === 'system' ? '内置' : '手动' }}</span>
        <button class="btn ghost sm" @click="startEditTpl(t)">编辑</button>
        <button class="btn ghost sm" @click="removeTpl(t)">删除</button>
      </div>
    </div>

    <!-- ============ Prompt ============ -->
    <div v-else-if="section === 'prompt'" class="card">
      <div class="card-title">
        <span>✎</span> 摘要 Prompt 模板
        <div class="spacer" style="flex:1"></div>
        <button class="btn primary" @click="savePrompt">
          {{ promptSaved ? '已保存 ✓' : '保存' }}
        </button>
      </div>
      <div class="dim" style="margin-bottom:8px;font-size:11.5px">
        这里改的是「AI 摘要」用的提示词；「解析模板」在上一栏。
      </div>
      <textarea v-model="promptTemplate" rows="20" class="mono"></textarea>
    </div>

    <!-- ============ 平台能力（从设置区搬来） ============ -->
    <div v-else-if="section === 'adapters'" class="card">
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

    <!-- ============ 维护与诊断 ============ -->
    <div v-else class="grid-2" style="grid-template-columns:1fr 1fr;align-items:start">
      <div class="card">
        <div class="card-title">
          <span>🩺</span> 自检诊断
          <div class="spacer" style="flex:1"></div>
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
        <div class="alert info" style="margin-top:10px">
          md 是唯一真相源：即使数据库全丢，也能用「重建索引」从你的库恢复。
        </div>
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
</style>
