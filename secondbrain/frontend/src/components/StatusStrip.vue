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
  poll: { type: Number, default: 0 },
  /**
   * v37：**外部直接给分组**（加工区用）。
   * 传了就不按 state 分组，也不显示「全部 →」（加工区没有状态页可跳）。
   * 形状：[{ key, label, icon, cls, items: [{id, title, ...}] }]
   */
  groups: { type: Array, default: null },
  /** 标题，默认「任务状态」*/
  title: { type: String, default: '任务状态' },
  /** 是否显示右上「全部 →」*/
  showAll: { type: Boolean, default: true },
  /**
   * 最小高度（px）。用途：**跨页面窗口等高**。
   * 采集区有 4 行、加工区只有 2 行，不设这个两条带子高度会差 50px 左右，
   * 切页面时内容会跳。传同一个值就齐了。
   */
  minHeight: { type: Number, default: 0 },
  /**
   * 紧凑形态（v38，加工队列用）。
   *
   * 加工区只有 2 行，按采集区那套 195px 铺开会显得"两条消息飘在一大片空里"。
   * dense 收掉上下留白、行间距，高度降到 ~100px。
   */
  dense: { type: Boolean, default: false }
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

const groups = computed(() => {
  // 外部给了分组（加工区：待加工 / 待手核）→ 只做截断，不改语义
  if (props.groups) {
    return props.groups.map((g) => {
      const list = g.items || []
      return {
        key: g.key,
        label: g.label,
        icon: g.icon || '',
        cls: g.cls || '',
        count: list.length,
        shown: list.slice(0, props.perGroup)
      }
    })
  }
  // 默认：按 state 分四组（采集区）
  return GROUPS.map((g) => {
    const list = allItems.value.filter((i) => i.state === g.key)
    return {
      ...g,
      count: list.length,
      shown: list.slice(0, props.perGroup)
    }
  })
})

const total = computed(() => {
  if (props.groups) {
    return props.groups.reduce((n, g) => n + (g.items || []).length, 0)
  }
  return allStats.value.total ?? allItems.value.length
})

async function load() {
  try {
    const data = await workbenchApi.status(50)
    ownItems.value = data.items || []
    ownStats.value = data.stats || {}
  } catch {
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
  <div
    class="status-strip"
    :class="{ 'strip-even': !!groups, 'strip-dense': dense }"
    :style="minHeight ? { minHeight: minHeight + 'px' } : null"
  >
    <div class="strip-head">
      <span class="strip-title"><span>≡</span> {{ title }}</span>
      <span class="dim mono" style="font-size: 11px">共 {{ total }} 条</span>
      <div class="spacer" style="flex: 1"></div>
      <button v-if="showAll" class="btn ghost sm" @click="goAll">全部 →</button>
    </div>

    <div class="strip-body">
      <!-- v33：四个状态**并行横排**（各占一列，不换行）。
           点状态徽标 → 进该状态的状态页（带筛选）。
           点具体任务 → 选中它（采集区联动）。 -->
      <div v-for="g in groups" :key="g.key" class="strip-group" :class="g.cls">
        <button
          class="strip-badge"
          :class="g.cls"
          :title="showAll ? `查看全部「${g.label}」任务` : g.label"
          :disabled="!showAll"
          @click="showAll && goFiltered(g.key)"
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
            >+{{ g.count - g.shown.length }}</span
          >
        </div>
      </div>

      <span v-if="!total" class="dim" style="font-size: 11.5px"> 还没有任务 </span>
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
  /* v38：**不再强制等高**。
     v37 为了"窗口大小相同"给两条带子都加了 min-height:195px，
     结果加工区只有 2 行，硬撑到 195 会在两行之间留一大片空 —— 用户看着别扭。
     现在高度由内容决定：采集区 4 行自然 ~195，加工区 2 行自然 ~120。 */
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
  /* v35：四个状态**同一列纵向排列**（用户：四个状态在同一列）。
     每行内部是「徽标 + 任务 chip」横排。 */
  display: flex;
  flex-direction: column;
  gap: 7px;
  align-items: stretch;
}
.strip-group {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  /* 行内横排，不换行：放不下就裁掉（有 +N 兜底） */
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
  /* 四行对齐：徽标固定宽度，chip 起点才整齐 */
  min-width: 84px;
  text-align: center;
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
/* 加工区「待手核」用 warn 色（和采集区的 成功/失败/进行中 区分开） */
.strip-badge.warn {
  color: var(--warn, #e0a33a);
  border-color: var(--warn, #e0a33a);
}
/* 加工区「待手核」用 warn 色（和采集区的 成功/失败/进行中 区分开） */
.strip-badge.warn {
  color: var(--warn, #e0a33a);
  border-color: var(--warn, #e0a33a);
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
/* 不可点的徽标（加工区没有状态页可跳）：不要装成按钮 */
.strip-badge:disabled {
  cursor: default;
}
.strip-badge:disabled:hover {
  border-color: var(--line);
  color: var(--text-dim);
}
.strip-badge.ok:disabled:hover {
  border-color: var(--ok, #35c48e);
  color: var(--ok, #35c48e);
}
.strip-badge.danger:disabled:hover {
  border-color: var(--danger, #e5484d);
  color: var(--danger, #e5484d);
}
.strip-badge.accent:disabled:hover {
  border-color: var(--accent-border);
  color: var(--accent);
}

/* v37：外部分组模式（加工区 2 行）——行均分高度，使整条与采集区等高 */
.strip-even {
  display: flex;
  flex-direction: column;
}
.strip-even .strip-body {
  flex: 1;
  justify-content: flex-start;
}

/* v38：紧凑形态（加工队列）——高度约 100px，差不多是采集区那条的一半。
   两行紧挨着排，不再上下分居（分居会让 2 行撑出 100px 的空白）。 */
.strip-dense {
  padding: 8px 14px 10px;
  min-height: 0;
}
.strip-dense .strip-head {
  margin-bottom: 5px;
}
.strip-dense .strip-title {
  font-size: 13px;
}
.strip-dense .strip-body {
  gap: 4px;
}
</style>
