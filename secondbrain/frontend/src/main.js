// 启动前先应用主题与密度（避免首屏闪一下深色再切浅色）
;(function bootPrefs() {
  try {
    const t = localStorage.getItem('sb:theme') || 'dark'
    const root = document.documentElement
    if (t === 'auto') {
      root.dataset.theme = window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light'
    } else {
      root.dataset.theme = t
    }
    root.dataset.density = localStorage.getItem('sb:density') || 'comfortable'
  } catch {
    /* 隐私模式下 localStorage 可能抛错，忽略 */
  }
})()

import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './assets/main.css'

/**
 * 全局错误兜底（v39）。
 *
 * 以前 Vue 的运行时错误、没被 catch 的 Promise 拒绝**完全静默**：
 * 界面停在半截状态，只有控制台有痕迹 —— 用界面的人根本不知道出了事，
 * 只能强刷页面。
 *
 * 现在统一拦下来给一条可见提示：
 *   · Vue errorHandler：组件渲染/事件里的异常
 *   · unhandledrejection：漏 catch 的异步失败
 *   · error：组件外的普通 JS 异常 + 资源加载失败
 *
 * 提示优先走 App.vue 提供的 toast（Vue 渲染、样式统一），
 * 挂载前用兜底 DOM 提示。
 */
function fallbackToast(message, type = 'error', timeout = 8000) {
  // App.vue 挂载后会把它的 toast 挂到 window 上
  if (typeof window.__sbToast === 'function') {
    window.__sbToast(message, type, timeout)
    return
  }
  try {
    let host = document.querySelector('.toast-host')
    if (!host) {
      host = document.createElement('div')
      host.className = 'toast-host'
      document.body.appendChild(host)
    }
    const el = document.createElement('div')
    el.className = `toast ${type}`
    el.textContent = message
    host.appendChild(el)
    setTimeout(() => el.remove(), timeout)
  } catch {
    /* 连 DOM 都动不了就算了 */
  }
}

const app = createApp(App)

app.config.errorHandler = (err, _instance, info) => {
  console.error('[SecondBrain] Vue 运行时错误:', err, info)
  const msg = err && err.message ? err.message : String(err)
  fallbackToast(`界面出错了：${msg}`, 'error')
}

window.addEventListener('unhandledrejection', (event) => {
  console.error('[SecondBrain] 未处理的异步失败:', event.reason)
  const reason = event.reason
  const msg = reason && reason.message ? reason.message : String(reason)
  fallbackToast(`操作失败：${msg}`, 'error')
})

window.addEventListener('error', (event) => {
  console.error('[SecondBrain] 脚本错误:', event.error || event.message)
  // Vue 组件内的异常走 app.config.errorHandler（Vue 不会把它再抛到 window，
  // 所以不会重复提示）。这里处理的是**组件外**的普通 JS 异常：
  if (event.error) {
    const msg = event.error.message || String(event.error)
    fallbackToast(`脚本出错了：${msg}`, 'error')
    return
  }
  // 资源加载失败（img/script 404 之类）：这类错误没有 error 对象，
  // 静默失败最容易让人摸不着头脑，至少要提示一下。
  // EventTarget 不一定有 tagName；instanceof Element 同时完成类型收窄
  if (event.target instanceof Element) {
    fallbackToast(`资源加载失败：${event.target.tagName}`, 'warn')
  }
})

app.use(router)
app.mount('#app')
