<script setup>
/**
 * 任务状态页：采集/加工的所有任务在这条流水线上的位置与结果。
 *
 * 从采集区、加工区跳转过来。每条任务显示：阶段、进度、耗时、
 * 失败原因（可读文本）+ 重试按钮。
 */
import { ref, computed, onMounted, onUnmounted, inject, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { workbenchApi, itemsApi, noteRoute } from '../api/client'

const toast = inject('toast')
const router = useRouter()
const route = useRoute()

const states = ref([])
const stats = ref({})
const loading = ref(false)

// v33：从采集区的状态条点某个状态跳过来时带 ?filter=done，
// 落地就该是那一类，不用再手动点一次筛选。
const VALID_FILTERS = ['all', 'run', 'wait', 'done', 'fail']
const filter = ref(
  VALID_FILTERS.includes(String(route.query.filter || '')) ? String(route.query.filter) : 'all'
)

// 停留在本页时再点别的状态（例如从侧边栏）也要跟着变
watch(
  () => route.query.filter,
  (v) => {
    const f = String(v || '')
    if (VALID_FILTERS.includes(f)) filter.value = f
  }
)

const filtered = computed(() => {
  if (filter.value === 'all') return states.value
  return states.value.filter((i) => i.state === filter.value)
})

let timer = null

const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'run', label: '进行中' },
  { id: 'wait', label: '等待' },
  { id: 'done', label: '已完成' },
  { id: 'fail', label: '失败' }
]

const stateClass = (s) => ({ run: 'accent', done: 'ok', fail: 'danger', wait: '' })[s] || ''

async function load() {
  loading.value = true
  try {
    const data = await workbenchApi.status(100)
    states.value = data.items || []
    stats.value = data.stats || {}
  } catch (e) {
    toast('读取任务状态失败：' + e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function retry(id) {
  try {
    await itemsApi.retry(id)
    toast('已重新提交', 'success')
    await load()
  } catch (e) {
    toast('重试失败：' + e.message, 'error')
  }
}

/** 打开这条任务的 A/B 层原文。
 *
 * 用路由跳转而不是 window.location.href——后者会整页重载，
 * 丢掉 SPA 状态，还会让页面重新挂载一遍所有视图。 */
function openItem(item) {
  if (!item.inbox_path && !item.processed_path) return
  router.push(noteRoute(item.processed_path || item.inbox_path))
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onUnmounted(() => {
  clearInterval(timer)
})
</script>

<template>
  <div class="status-page">
    <!-- 统计条 -->
    <div class="card">
      <div class="stat-row">
        <div class="stat">
          <div class="num">{{ stats.total || 0 }}</div>
          <div class="dim">全部</div>
        </div>
        <div class="stat">
          <div class="num accent">{{ stats.running || 0 }}</div>
          <div class="dim">进行中</div>
        </div>
        <div class="stat">
          <div class="num">{{ stats.wait || 0 }}</div>
          <div class="dim">等待</div>
        </div>
        <div class="stat">
          <div class="num ok">{{ stats.done || 0 }}</div>
          <div class="dim">已完成</div>
        </div>
        <div class="stat">
          <div class="num danger">{{ stats.fail || 0 }}</div>
          <div class="dim">失败</div>
        </div>
        <div class="spacer" style="flex: 1"></div>
        <div class="dim mono" style="align-self: center">
          <!-- v31：进度只统计**未完成**的条目（后端已改）。
               全都完成了显示「已全部完成」，比一个孤零零的 100% 清楚。 -->
          {{
            stats.pending
              ? `进行中进度 ${Math.round((stats.avg_progress || 0) * 100)}%`
              : stats.total
                ? '已全部完成'
                : '暂无任务'
          }}
        </div>
        <button class="btn ghost sm" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- 筛选 + 列表 -->
    <div class="card">
      <div class="tabs" style="margin-bottom: 10px">
        <button
          v-for="f in FILTERS"
          :key="f.id"
          class="tab"
          :class="{ active: filter === f.id }"
          @click="filter = f.id"
        >
          {{ f.label }}
        </button>
      </div>

      <div v-for="item in filtered" :key="item.id" class="task-item" @click="openItem(item)">
        <div class="grow">
          <div class="title">
            {{ item.title || '未命名' }}
            <span class="badge" :class="stateClass(item.state)" style="margin-left: 6px">
              {{ item.stage || item.status }}
            </span>
          </div>
          <div v-if="item.error_detail" class="sub danger-text">
            {{ item.error_detail }}
          </div>
          <div class="sub dim mono" style="font-size: 11px">
            {{ item.platform_label || item.platform }} ·
            {{ (item.source_url || '').slice(0, 60) }}
          </div>
          <div class="progress-track" style="margin-top: 6px">
            <div
              class="progress-bar"
              :class="{ fail: item.state === 'fail' }"
              :style="{ width: Math.round((item.progress || 0) * 100) + '%' }"
            ></div>
          </div>
        </div>
        <button
          v-if="item.state === 'fail'"
          class="btn ghost sm"
          style="align-self: center"
          @click.stop="retry(item.id)"
        >
          重试
        </button>
      </div>

      <div v-if="!filtered.length" class="empty" style="padding: 24px 8px">
        <div class="icon">≡</div>
        <div>{{ filter === 'all' ? '还没有任务' : '这一类暂时没有' }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.stat-row {
  display: flex;
  gap: 26px;
  align-items: stretch;
}
.stat .num {
  font-size: 22px;
  font-weight: 650;
}
.stat .num.accent {
  color: var(--accent);
}
.stat .num.ok {
  color: var(--ok, #35c48e);
}
.stat .num.danger {
  color: var(--danger, #e5484d);
}
.task-item {
  display: flex;
  gap: 10px;
  padding: 9px 6px;
  border-top: 1px solid var(--line);
  cursor: pointer;
}
.task-item:first-of-type {
  border-top: none;
}
.task-item:hover {
  background: var(--bg-hover);
  border-radius: 8px;
}
.danger-text {
  color: var(--danger, #e5484d);
}
.progress-track {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-hover);
  overflow: hidden;
  max-width: 320px;
}
.progress-bar {
  height: 100%;
  background: var(--accent);
  border-radius: 2px;
  transition: width 0.4s;
}
.progress-bar.fail {
  background: var(--danger, #e5484d);
}
</style>
