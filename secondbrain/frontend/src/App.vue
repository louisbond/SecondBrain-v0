<script setup>
/**
 * 应用外壳：侧边导航 + 顶栏 + 内容区。
 *
 * 顶栏在 LAN 开启时显示实际端口提示（§3.1：被占自动顺延并在 UI 顶部提示）。
 */
import { ref, onMounted, onUnmounted, computed, provide, watch } from 'vue'
import { useRoute } from 'vue-router'
import { healthApi, collectApi, reviewApi } from './api/client'

const route = useRoute()
const health = ref(null)
const stats = ref({})
const pendingCount = ref(0)
const toastHost = ref(null)
// 全局刷新信号：窗口重新获得焦点 / 切换路由时自增，视图 watch 它重拉数据。
// 没有它就会出现"状态不同步"——顶栏计数在更新，页面里的列表还是旧的。
const refreshSignal = ref(0)

const navItems = [
  { to: '/', icon: '⌂', label: '首页' },
  { to: '/workbench', icon: '⇪', label: '采集区' },
  { to: '/process', icon: '✓', label: '加工区' },
  { to: '/original', icon: '✎', label: '原创区' },
  { to: '/manage', icon: '▦', label: '管理区' },
  { to: '/settings', icon: '⚙', label: '设置' }
]

const portNotice = computed(() => {
  if (!health.value) return ''
  if (health.value.allow_lan) {
    return `LAN 模式已开启 · ${health.value.host}:${health.value.port}`
  }
  return `本机 ${health.value.host}:${health.value.port}`
})

const searchModeWarning = computed(() => health.value?.search_mode_warning)

async function refresh() {
  try {
    health.value = await healthApi.get()
  } catch (e) {
    health.value = null
  }
  try {
    stats.value = await collectApi.stats()
  } catch (e) {
    stats.value = {}
  }
  try {
    const pending = await reviewApi.pending()
    pendingCount.value = (pending.items || []).length
  } catch (e) {
    pendingCount.value = 0
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
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <h1>SecondBrain</h1>
        <div class="version">v0.5.0 · 本机</div>
      </div>
      <nav class="sidebar-nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
        >
          <span class="nav-icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
          <span
            v-if="item.to === '/process' && pendingCount > 0"
            class="nav-badge"
          >{{ pendingCount }}</span>
        </RouterLink>
      </nav>
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
        <div v-if="searchModeWarning" class="alert warn">
          ⚠ {{ searchModeWarning }}
        </div>
        <RouterView />
      </main>
    </div>

    <div class="toast-host">
      <div
        v-for="t in toastHost || []"
        :key="t.id"
        class="toast"
        :class="t.type"
      >{{ t.message }}</div>
    </div>
  </div>
</template>
