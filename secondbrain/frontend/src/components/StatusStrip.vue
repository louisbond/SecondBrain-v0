<script setup>
/**
 * 状态横条：采集区（采集与解析之间）、加工区（待加工工作区下方）共用。
 *
 * 设计要点（用户定稿）：
 *   · **不是**一级导航，而是嵌在页面纵向流程里的「跳转口」
 *   · 条目**横向**排（一条带里读完，不用上下滚）
 *   · 分组顺序固定：完成 > 失败 > 进行中 > 等待
 *   · 点条目 = 选中该任务；点右上「全部 →」跳到完整状态页 /status
 *
 * 数据来源两种：
 *   · 父组件已有数据 → 传 items / stats（不重复请求）
 *   · 没有 → 组件自己拉（autoLoad）
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { workbenchApi } from '../api/client'

const props = defineProps({
  /** 父组件已有的数据（传了就用这个，不再请求） */
  items: { type: Array, default: null },
  stats: { type: Object, default: null },
  /** 每组最多显示几条条目 */
  perGroup: { type: Number, default: 3 },
  /** 自动轮询间隔（ms），0 = 不轮询 */
  poll: { type: Number, default: 0 }
})

const emit = defineEmits(['select'])

const router = useRouter()

const ownItems = ref([])
const ownStats = ref({})
let timer = null

// 分组顺序：完成 > 失败 > 进行中 > 等待
const GROUPS = [
  { key: 'done', label: '完成', cls: 'ok', icon: '✓' },
  { key: 'fail', label: '失败', cls: 'danger', icon: '✕' },
  { key: 'run', label: '进行中', cls: 'accent', icon: '⟳' },
  { key: 'wait', label: '等待', cls: '', icon: '⏳' }
]

const allItems = computed(() => props.items || ownItems.value)
const allStats = computed(() => props.stats || ownStats.value)

const groups = computed(() =>
  GROUPS.map((g) => {
    const list = allItems.value.filter((i) => i.state === g.key)
    return {
      ...g,
      count: list.length,
      shown: list.slice(0, props.perGroup)
    }
  })
)

const total = computed(() => allStats.value.total ?? allItems.value.length)

async function load() {
  try {
    const data = await workbenchApi.status(50)
    ownItems.value = data.items || []
    ownStats.value = data.stats || {}
  } catch (e) {
    /* 静默：状态条不该弹窗打扰 */
  }
}

function goAll() {
  router.push('/status')
}

/**
 * 点某个状态 → 进状态页并**带上该状态的筛选**（v33）。
 *
 * 用户：各状态能点击进各自页面。跳过去还要再点一次筛选很蠢，
 * 所以直接带上 ?filter=done / fail / run / wait。
 */
function goFiltered(key) {
  router.push({ path: '/status', query: { filter: key } })
}

function pick(item) {
  emit('select', item)
}

function short(text) {
  const t = String(text || '').trim()
  return t.length > 16 ? t.slice(0, 16) + '…' : t || '未命名'
}

onMounted(() => {
  if (!props.items) load()
  if (props.poll > 0) timer = setInterval(load, props.poll)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="status-strip">
    <div class="strip-head">
      <span class="strip-title"><span>≡</span> 任务状态</span>
      <span class="dim mono" style="font-size:11px">共 {{ total }} 条</span>
      <div class="spacer" style="flex:1"></div>
      <button class="btn ghost sm" @click="goAll">全部 →</button>
    </div>

    <div class="strip-body">
      <!-- v33：四个状态**并行横排**（各占一列，不换行）。
           点状态徽标 → 进该状态的状态页（带筛选）。
           点具体任务 → 选中它（采集区联动）。 -->
      <div
        v-for="g in groups"
        :key="g.key"
        class="strip-group"
        :class="g.cls"
      >
        <button
          class="strip-badge"
          :class="g.cls"
          :title="`查看全部「${g.label}」任务`"
          @click="goFiltered(g.key)"
        >
          {{ g.icon }} {{ g.label }} {{ g.count }}
        </button>
        <div class="strip-items">
          <span
            v-for="it in g.shown"
            :key="it.id"
            class="strip-item"
            :title="it.title || ''"
            @click="pick(it)"
          >
            {{ short(it.title) }}
          </span>
          <span
            v-if="g.count > g.shown.length"
            class="strip-more"
            :title="`还有 ${g.count - g.shown.length} 条`"
            @click="goFiltered(g.key)"
          >+{{ g.count - g.shown.length }}</span>
        </div>
      </div>

      <span v-if="!allItems.length" class="dim" style="font-size:11.5px">
        还没有任务
      </span>
    </div>
  </div>
</template>

<style scoped>
/* v32：置顶后放大（用户：任务状态置顶，面积调大一点）。
   原来是夹在采集与解析之间的一个细条，padding 8/12、字号 11.5，
   看着像页脚。现在它是采集区的第一眼信息，给足空间。 */
.status-strip {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-soft, rgba(255, 255, 255, 0.02));
  padding: 12px 16px 14px;
}
.strip-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.strip-title {
  font-size: 14px;
  font-weight: 650;
}
.strip-body {
  /* v33：四个状态**并行**（各占一格等宽），整行不换行。 */
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px 14px;
  align-items: start;
}
.strip-group {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  /* 组内也不换行：任务 chip 横向排，放不下就裁掉（有 +N 兜底） */
  flex-wrap: nowrap;
}
.strip-items {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  flex-wrap: nowrap;
}
.strip-badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--text-dim);
  white-space: nowrap;
  background: transparent;
  font-family: inherit;
  cursor: pointer;
  flex: 0 0 auto;
}
.strip-badge:hover {
  border-color: var(--accent);
  color: var(--text);
}
.strip-badge.ok {
  color: var(--ok, #35c48e);
  border-color: var(--ok, #35c48e);
}
.strip-badge.danger {
  color: var(--danger, #e5484d);
  border-color: var(--danger, #e5484d);
}
.strip-badge.accent {
  color: var(--accent);
  border-color: var(--accent-border);
}
.strip-item {
  font-size: 12.5px;
  padding: 4px 11px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-dim);
  cursor: pointer;
  /* 四组并行时每组只有 ~270px，chip 太宽会挤到隔壁组 */
  max-width: 118px;
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.strip-item:hover {
  color: var(--text);
  background: var(--accent-soft);
}
.strip-more {
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
  flex: 0 0 auto;
  white-space: nowrap;
}
</style>
