<script setup>
/**
 * 阅读页：B/C 层笔记详情。
 *
 * §6.3 / §11 布局：
 *   标题 + 标签
 *   🤖 AI 摘要（引用块样式）        ← 上
 *   ✍️ 个人手稿                      ← 下（垂直排布，不并排）
 *   📄 回查原文链接（A 层）
 *   🔗 反链 / 出链 + echarts 反链视图
 */
import { ref, onMounted, watch, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { searchApi, notesApi, itemsApi, noteRoute } from '../api/client'

const toast = inject('toast')
const refreshSignal = inject('refreshSignal')
const route = useRoute()
const router = useRouter()

const note = ref(null)
const loading = ref(false)
const sourceContent = ref(null)
const showSource = ref(false)
const graphEl = ref(null)
const showGraph = ref(false)
let chart = null

const notePath = () => route.params.path

async function load() {
  loading.value = true
  try {
    note.value = await notesApi.get(notePath())
    sourceContent.value = null
    showSource.value = false
    showGraph.value = false
  } catch (e) {
    toast('加载笔记失败：' + e.message, 'error')
    note.value = null
  } finally {
    loading.value = false
  }
}

async function openSource() {
  showSource.value = !showSource.value
  if (showSource.value && !sourceContent.value) {
    const src = note.value?.frontmatter?.source_inbox
    if (!src) return
    const stem = src.replace(/^\[\[|\]\]$/g, '')
    try {
      const list = await itemsApi.list({ limit: 200 })
      const match = (list.items || []).find((i) =>
        (i.inbox_path || '').includes(stem.replace(/\.md$/, ''))
      )
      if (match) {
        sourceContent.value = await itemsApi.content(match.id)
      } else {
        sourceContent.value = { body: '未找到对应的 A 层条目', frontmatter: {} }
      }
    } catch (e) {
      toast('加载原文失败：' + e.message, 'error')
    }
  }
}

async function toggleGraph() {
  showGraph.value = !showGraph.value
  if (!showGraph.value) {
    if (chart) {
      chart.dispose()
      chart = null
    }
    return
  }
  await new Promise((r) => setTimeout(r, 60))
  try {
    const echarts = await import('echarts')
    const data = await searchApi.graph(notePath())
    if (!graphEl.value) return

    if (chart) chart.dispose()
    chart = echarts.init(graphEl.value)

    const nodes = [{ name: note.value.title || '当前笔记' }]
    const seen = new Set([nodes[0].name])

    data.backlinks.forEach((b) => {
      const n = b.title || b.path
      if (!seen.has(n)) {
        seen.add(n)
        nodes.push({ name: n })
      }
    })
    data.outlinks.forEach((o) => {
      const n = o.title || o.path
      if (!seen.has(n)) {
        seen.add(n)
        nodes.push({ name: n })
      }
    })

    chart.setOption({
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'tree',
          data: [
            {
              name: nodes[0].name,
              children: nodes.slice(1).map((n) => ({ name: n.name }))
            }
          ],
          layout: 'radial',
          symbol: 'circle',
          symbolSize: 12,
          roam: false,
          label: { show: true, position: 'right', fontSize: 11 },
          lineStyle: { color: '#4b5563', curveness: 0.5 }
        }
      ]
    })
  } catch (e) {
    toast('反链视图加载失败：' + e.message, 'error')
  }
}

async function archive() {
  if (!confirm('确认归档这条笔记？将移入 99_Archive/，引用会同步更新。')) return
  try {
    await notesApi.archive(notePath())
    toast('已归档', 'success')
    router.push('/')
  } catch (e) {
    toast('归档失败：' + e.message, 'error')
  }
}

// ---- 补写个人手稿（已入库但手稿为空的 B 层笔记）----
const editingManuscript = ref(false)
const manuscriptDraft = ref('')
const savingManuscript = ref(false)

function startEditManuscript() {
  manuscriptDraft.value = note.value?.manuscript || ''
  editingManuscript.value = true
}

async function saveManuscript() {
  if (!note.value) return
  savingManuscript.value = true
  try {
    await notesApi.fillManuscript(note.value.note_path, manuscriptDraft.value)
    toast('手稿已保存', 'success')
    editingManuscript.value = false
    await load()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  } finally {
    savingManuscript.value = false
  }
}

watch(() => route.params.path, load)
// 全局刷新信号（窗口聚焦 / 切页）→ 重新拉数据，避免状态不同步
watch(refreshSignal, () => {
  load()
})

onMounted(load)
</script>

<template>
  <div v-if="note" style="max-width: 860px; margin: 0 auto">
    <div class="card">
      <div class="row">
        <span class="badge" :class="note.level === 'note' ? 'accent' : ''">
          {{ note.level === 'note' ? '创作笔记' : '提炼笔记' }}
        </span>
        <span v-if="note.form" class="badge">
          {{ note.form === 'article' ? '成文' : '碎片' }}
        </span>
        <span v-if="note.digested" class="badge ok">已消化</span>
        <span v-else-if="note.level === 'processed'" class="badge warn">待补手稿</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="toggleGraph">反链视图</button>
        <button class="btn ghost sm" @click="archive">归档</button>
      </div>
      <h1 style="margin: 12px 0 8px; font-size: 22px">{{ note.title }}</h1>
      <div class="row wrap">
        <span v-for="t in note.tags" :key="t" class="tag">{{ t }}</span>
        <span
          v-for="p in note.partitions"
          :key="p"
          class="tag"
          style="border-color: var(--accent-border)"
        >
          {{ p }}
        </span>
        <span class="dim mono" style="margin-left: auto">
          {{ note.frontmatter?.created || '' }}
        </span>
      </div>
    </div>

    <!-- AI 摘要（上） -->
    <div v-if="note.summary" class="review-block ai">
      <div class="block-head">
        <span>🤖</span> AI 摘要
        <span v-if="note.frontmatter?.ai_model" class="badge">
          {{ note.frontmatter.ai_model }}
        </span>
      </div>
      <div class="block-body">
        <div class="ai-summary">{{ note.summary }}</div>
      </div>
    </div>

    <!-- 个人手稿（下）——B 层笔记这里是主内容；空的时候给出补写入口 -->
    <div v-if="note.manuscript || note.level === 'processed'" class="review-block human">
      <div class="block-head">
        <span>✍️</span> 个人手稿
        <span v-if="!note.manuscript" class="badge warn">待补</span>
        <div class="spacer" style="flex: 1"></div>
        <button v-if="!editingManuscript" class="btn ghost sm" @click="startEditManuscript">
          {{ note.manuscript ? '编辑手稿' : '补写手稿' }}
        </button>
      </div>
      <div v-if="editingManuscript" class="block-body">
        <textarea
          v-model="manuscriptDraft"
          rows="5"
          placeholder="写下你的理解、疑问、行动点"
        ></textarea>
        <div class="row" style="margin-top: 8px">
          <button class="btn primary" :disabled="savingManuscript" @click="saveManuscript">
            {{ savingManuscript ? '保存中…' : '保存手稿' }}
          </button>
          <button class="btn ghost" @click="editingManuscript = false">取消</button>
        </div>
      </div>
      <div v-else class="block-body">
        <div v-if="note.manuscript" class="ai-summary">{{ note.manuscript }}</div>
        <div v-else class="dim">
          还没有写手稿。手稿是图谱里的主节点，也是这条笔记真正属于你的部分——
        </div>
      </div>
    </div>

    <!-- 正文 -->
    <div v-if="note.body?.trim()" class="card">
      <div class="card-title">正文</div>
      <div style="white-space: pre-wrap; line-height: 1.8">{{ note.body }}</div>
    </div>

    <!-- 回查原文 -->
    <div v-if="note.frontmatter?.source_inbox" class="review-block plain">
      <div class="block-head">
        <span>📄</span> 回查原文
        <span class="dim mono">{{ note.frontmatter.source_inbox }}</span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="openSource">
          {{ showSource ? '收起' : '展开' }}
        </button>
      </div>
      <div v-if="showSource" class="block-body">
        <div v-if="sourceContent" class="mono" style="white-space: pre-wrap; line-height: 1.7">
          {{ sourceContent.body }}
        </div>
        <div v-else class="dim">加载中……</div>
      </div>
    </div>

    <!-- 反链 -->
    <div class="card">
      <div class="card-title">🔗 引用关系</div>
      <div class="grid-2">
        <div>
          <label>被引用（反链）</label>
          <div v-if="note.backlinks?.length">
            <RouterLink
              v-for="b in note.backlinks"
              :key="b.note_path"
              :to="noteRoute(b.note_path)"
              class="list-item"
              style="text-decoration: none; color: inherit"
            >
              <div class="grow">
                <div class="title">{{ b.title }}</div>
                <div class="sub">{{ b.kind === 'citation' ? '正文引用' : '确认关联' }}</div>
              </div>
            </RouterLink>
          </div>
          <div v-else class="dim">还没有笔记引用它</div>
        </div>
        <div>
          <label>引用了</label>
          <div v-if="note.outlinks?.length">
            <RouterLink
              v-for="o in note.outlinks"
              :key="o.note_path"
              :to="noteRoute(o.note_path)"
              class="list-item"
              style="text-decoration: none; color: inherit"
            >
              <div class="grow">
                <div class="title">{{ o.title }}</div>
                <div class="sub">{{ o.kind === 'citation' ? '正文引用' : '确认关联' }}</div>
              </div>
            </RouterLink>
          </div>
          <div v-else class="dim">没有引用其他笔记</div>
        </div>
      </div>

      <div v-if="showGraph" ref="graphEl" style="height: 320px; margin-top: 14px"></div>
    </div>
  </div>

  <div v-else-if="!loading" class="empty">
    <div class="icon">📄</div>
    <div>笔记不存在或已被删除</div>
  </div>
</template>
