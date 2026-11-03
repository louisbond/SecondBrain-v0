<script setup>
/**
 * 应用外壳：侧边导航 + 顶栏 + 内容区。
 *
 * 顶栏在 LAN 开启时显示实际端口提示（§3.1：被占自动顺延并在 UI 顶部提示）。
 */
import { ref, onMounted, onUnmounted, computed, provide, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { healthApi, collectApi, reviewApi } from './api/client'

const route = useRoute()
const router = useRouter()
const health = ref(null)
const stats = ref({})
const pendingCount = ref(0)
const toastHost = ref(null)
// 全局刷新信号：窗口重新获得焦点 / 切换路由时自增，视图 watch 它重拉数据。
// 没有它就会出现"状态不同步"——顶栏计数在更新，页面里的列表还是旧的。
const refreshSignal = ref(0)

const navItems = [
  // v32：一级导航第一项从「首页」改成「每周回顾」。
  // 用户：「左下角（一级导航）改成每周回顾算了，首页那个去掉」。
  // 路由仍是 /（HomeView 已改造：周回顾为主，搜索/图谱保留在下方）。
  { to: '/', icon: '◷', label: '每周回顾' },
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

/** 侧边栏状态面板的跳转（点计数直达对应页面）。 */
function go(path) {
  if (route.path !== path) router.push(path)
}

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

      <!--
        v31：侧边栏状态面板。
        原来侧边栏只有品牌区 + 6 个菜单，下面是整片空白（208px × 全高）。
        把空白填成**有用的常驻信息**：待核准 / 待补手稿 / 今日采集 / 失败数，
        点一下直接跳到对应页面 —— 不用先切页面再找。
      -->
      <div class="sidebar-status">
        <div class="sidebar-status-title">本机状态</div>

        <button
          class="sidebar-stat"
          @click="go('/process')"
        >
          <span class="k">待核准</span>
          <span class="v" :class="{ accent: stats.pending_review > 0 }">
            {{ stats.pending_review || 0 }}
          </span>
        </button>
        <button
          class="sidebar-stat"
          @click="go('/')"
        >
          <span class="k">待补手稿</span>
          <span class="v" :class="{ warn: stats.need_manuscript > 0 }">
            {{ stats.need_manuscript || 0 }}
          </span>
        </button>
        <button class="sidebar-stat" @click="go('/workbench')">
          <span class="k">今日采集</span>
          <span class="v">{{ stats.today || 0 }}</span>
        </button>
        <button
          class="sidebar-stat"
          @click="go('/status')"
        >
          <span class="k">解析失败</span>
          <span class="v" :class="{ danger: (stats.by_status?.ai_failed || 0) > 0 }">
            {{ stats.by_status?.ai_failed || 0 }}
          </span>
        </button>
      </div>

      <div class="sidebar-foot">
        <div class="dim" style="font-size:11px;line-height:1.6">
          共 {{ stats.total || 0 }} 条素材<br />
          本周 {{ stats.week || 0 }} 条
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
