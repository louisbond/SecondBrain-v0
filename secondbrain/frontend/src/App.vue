<script setup>
/**
 * 应用外壳：侧边导航 + 顶栏 + 内容区。
 *
 * 顶栏在 LAN 开启时显示实际端口提示（§3.1：被占自动顺延并在 UI 顶部提示）。
 */
import { ref, onMounted, onUnmounted, computed, provide, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { healthApi, collectApi, reviewApi, recapApi } from './api/client'

const route = useRoute()
const router = useRouter()
const health = ref(null)
/** v39：健康检查里失败的关键项（侧边栏提示用）。 */
const failedChecks = computed(() =>
  (health.value?.checks || []).filter((c) => !c.ok).map((c) => c.name)
)
const stats = ref({})
const pendingCount = ref(0)
const toastHost = ref(null)
// v33：侧边栏「每周回顾」用的本周数据（用户：把每周回顾覆盖到本机状态那里）
const week = ref({})
const weeklyBusy = ref(false)
// 全局刷新信号：窗口重新获得焦点 / 切换路由时自增，视图 watch 它重拉数据。
// 没有它就会出现"状态不同步"——顶栏计数在更新，页面里的列表还是旧的。
const refreshSignal = ref(0)

const navItems = [
  // v33：导航第一项恢复「首页」——用户澄清「每周回顾」是要覆盖到
  // 侧边栏的「本机状态」那块，不是替换首页。
  { to: '/', icon: '⌂', label: '首页' },
  { to: '/workbench', icon: '⇪', label: '采集区' },
  { to: '/process', icon: '✓', label: '加工区' },
  { to: '/original', icon: '✎', label: '原创区' },
  // v37：设置并入管理区；v38 用户要求导航就叫「设置」（两页已是一页）
  { to: '/manage', icon: '⚙', label: '设置' }
]

const portNotice = computed(() => {
  if (!health.value) return ''
  if (health.value.allow_lan) {
    return `LAN 模式已开启 · ${health.value.host}:${health.value.port}`
  }
  return `本机 ${health.value.host}:${health.value.port}`
})

const searchModeWarning = computed(() => health.value?.search_mode_warning)

/** 侧边栏跳转（点计数直达对应页面）。第二参数可带状态筛选。 */
function go(path, filter) {
  const target = filter ? { path, query: { filter } } : path
  if (route.path !== path || (filter && route.query.filter !== filter)) {
    router.push(target)
  }
}

async function refresh() {
  try {
    health.value = await healthApi.get()
  } catch {
    health.value = null
  }
  try {
    stats.value = await collectApi.stats()
  } catch {
    stats.value = {}
  }
  try {
    const pending = await reviewApi.pending()
    pendingCount.value = (pending.items || []).length
  } catch {
    pendingCount.value = 0
  }
  try {
    // 侧边栏周回顾：只要计数（card 是轻量接口，不写文件、不调 AI）
    const c = await recapApi.card()
    week.value = c || {}
  } catch {
    week.value = {}
  }
}

/** 侧边栏「生成本周周报」：生成后写 vault/_reports/，并提示去看。 */
async function makeWeekly() {
  if (weeklyBusy.value) return
  weeklyBusy.value = true
  try {
    const r = await recapApi.weekly(0)
    week.value = {
      ...week.value,
      collected_week: r.collected,
      pending_review: week.value.pending_review
    }
    toast(`周报已生成：${r.week || ''}`, 'success')
  } catch (e) {
    toast('生成周报失败：' + (e.message || e), 'error')
  } finally {
    weeklyBusy.value = false
  }
}

/** 全局 toast 注入 */
// toast 去重：同一消息短时间内不重复弹
// （窗口反复 focus 时各视图会重复报错，不去重就变成弹窗刷屏）
const toastRecent = new Map()

function toast(message, type = 'info', timeout = 3600) {
  const key = `${type}:${message}`
  const now = Date.now()
  const last = toastRecent.get(key) || 0
  if (now - last < 3000) return
  toastRecent.set(key, now)
  // 清理过期的记录，避免无限增长
  if (toastRecent.size > 50) {
    for (const [k, t] of toastRecent) {
      if (now - t > 30000) toastRecent.delete(k)
    }
  }

  const id = Math.random().toString(36).slice(2)
  const item = { id, message, type }
  toastHost.value = toastHost.value || []
  toastHost.value.push(item)
  setTimeout(() => {
    if (toastHost.value) {
      toastHost.value = toastHost.value.filter((t) => t.id !== id)
    }
  }, timeout)
}

provide('toast', toast)
provide('refreshGlobal', refresh)
provide('refreshSignal', refreshSignal)

let timer = null
let focusHandler = null

// 切换页面 → 目标视图重新拉数据（保证看到的是最新状态）
watch(
  () => route.path,
  () => {
    refreshSignal.value += 1
  }
)

// 注意：这里**不能**用 beforeunload 来通知后端退出——
// 用户按 F5 刷新页面也会触发 beforeunload，会把服务误关掉。
// 桌面模式（pywebview）的关窗检测由后端的 window.events.closed 负责：
//   窗口关闭 → pywebview 事件 → stop_backend + os._exit(0) → 缓存数据保留在磁盘。

onMounted(() => {
  // v39：把 toast 挂到 window，供 main.js 的全局错误兜底复用（样式统一 + 去重）
  window.__sbToast = toast
  refresh()
  timer = setInterval(refresh, 20000)
  // 窗口重新获得焦点 → 通知所有视图重拉数据
  // 窗口获得焦点 → 刷新数据。
  // **必须节流**：pywebview 里 focus 可能被反复触发，
  // 不节流就会让所有视图反复重载（→ 弹窗刷屏 + 网络请求刷爆）。
  let lastFocus = 0
  focusHandler = () => {
    const now = Date.now()
    if (now - lastFocus < 2000) return
    lastFocus = now
    refreshSignal.value += 1
    refresh()
  }
  window.addEventListener('focus', focusHandler)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (focusHandler) window.removeEventListener('focus', focusHandler)
  if (window.__sbToast === toast) window.__sbToast = null
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <h1>SecondBrain</h1>
        <!-- v39：版本号**从后端取**，不再前端硬编码。
             以前这里写死 v0.5.0，后端都迭代到 v38 了还显示 0.5.0 ——
             用户看到的版本和真实版本对不上。取不到时回退到 '—'（不显示假版本）。

             后端健康状态不 ok 时标红：v39 之前 /api/health 恒返回 ok:true，
             数据库坏了界面也看不出来。 -->
        <div class="version">
          <span
            class="health-dot"
            :class="health?.ok === false ? 'bad' : health ? 'ok' : ''"
            :title="
              health?.ok === false ? '后端异常：' + (failedChecks.join('、') || '未知') : '后端正常'
            "
          ></span>
          <template v-if="health?.version">v{{ health.version }}</template>
          <template v-else>·</template>
          · 本机
        </div>
      </div>
      <nav class="sidebar-nav">
        <RouterLink v-for="item in navItems" :key="item.to" :to="item.to" class="nav-item">
          <span class="nav-icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
          <span v-if="item.to === '/process' && pendingCount > 0" class="nav-badge">{{
            pendingCount
          }}</span>
        </RouterLink>
      </nav>

      <!--
        v33：侧边栏这块从「本机状态」改成「每周回顾」
        （用户：把每周回顾覆盖到本机状态那里）。
        显示本周采集/入库/创作 + 待核准，底部一键生成周报。
        首页那个周回顾卡片已按用户要求去掉。
      -->
      <div class="sidebar-status">
        <div class="sidebar-status-title">每周回顾</div>

        <button class="sidebar-stat" @click="go('/workbench')">
          <span class="k">本周采集</span>
          <span class="v">{{ week.collected_week ?? stats.week ?? 0 }}</span>
        </button>
        <button class="sidebar-stat" @click="go('/process')">
          <span class="k">库中待核准</span>
          <span class="v" :class="{ accent: (week.pending_review || stats.pending_review) > 0 }">
            {{ week.pending_review ?? stats.pending_review ?? 0 }}
          </span>
        </button>
        <button class="sidebar-stat" @click="go('/original')">
          <span class="k">本周创作</span>
          <span class="v">{{ week.created_notes_week ?? 0 }}</span>
        </button>
        <button class="sidebar-stat" @click="go('/status', 'fail')">
          <span class="k">失败任务</span>
          <span class="v" :class="{ danger: (stats.by_status?.ai_failed || 0) > 0 }">
            {{ stats.by_status?.ai_failed || 0 }}
          </span>
        </button>

        <button class="sidebar-weekly-btn" :disabled="weeklyBusy" @click="makeWeekly">
          {{ weeklyBusy ? '生成中…' : '生成本周周报' }}
        </button>
      </div>

      <div class="sidebar-foot">
        <div class="dim" style="font-size: 11px; line-height: 1.6">
          共 {{ stats.total || 0 }} 条素材<br />
          本周采集 {{ stats.week || 0 }} 条
        </div>
      </div>
    </aside>

    <div class="main-area">
      <header class="topbar">
        <h2>{{ route.meta?.title || 'SecondBrain' }}</h2>
        <div class="spacer"></div>
        <span v-if="stats.pending_review" class="badge accent">
          待核准 {{ stats.pending_review }}
        </span>
        <span v-if="stats.need_manuscript" class="badge warn">
          待补手稿 {{ stats.need_manuscript }}
        </span>
        <span class="dim mono">{{ portNotice }}</span>
      </header>

      <main class="content">
        <div v-if="searchModeWarning" class="alert warn">⚠ {{ searchModeWarning }}</div>
        <RouterView />
      </main>
    </div>

    <div class="toast-host">
      <div v-for="t in toastHost || []" :key="t.id" class="toast" :class="t.type">
        {{ t.message }}
      </div>
    </div>
  </div>
</template>
