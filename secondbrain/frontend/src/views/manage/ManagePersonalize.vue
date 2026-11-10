<script setup>
/**
 * 个性化（v0.10.9）：主题与界面偏好。
 *
 * 主题实现：html[data-theme="light"] 覆盖 main.css 里的 CSS 变量，
 * 不需要给每个组件写浅色样式 —— 全部走 var(--xxx)。
 * 选择存在 localStorage（sb:theme），下次启动立即生效（main.js 里先应用再挂载）。
 */
import { ref, onMounted } from 'vue'
import { inject } from 'vue'

const toast = inject('toast')

const THEMES = [
  { key: 'dark', label: '深色', desc: '桌面常驻，长时间看不刺眼' },
  { key: 'light', label: '浅色', desc: '白天/投影环境更清楚' },
  { key: 'auto', label: '跟随系统', desc: '随 Windows 的深浅模式自动切换' }
]

const DENSITIES = [
  { key: 'comfortable', label: '舒适', desc: '默认间距，适合大屏' },
  { key: 'compact', label: '紧凑', desc: '行高与留白收紧，一屏看更多' }
]

const theme = ref('dark')
const density = ref('comfortable')

function applyTheme(v) {
  const root = document.documentElement
  if (v === 'auto') {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
    root.dataset.theme = dark ? 'dark' : 'light'
  } else {
    root.dataset.theme = v
  }
}

function applyDensity(v) {
  document.documentElement.dataset.density = v
}

function setTheme(v) {
  theme.value = v
  localStorage.setItem('sb:theme', v)
  applyTheme(v)
  toast?.(`已切换为「${THEMES.find((t) => t.key === v)?.label}」`, 'success')
}

function setDensity(v) {
  density.value = v
  localStorage.setItem('sb:density', v)
  applyDensity(v)
  toast?.(`界面密度：${DENSITIES.find((d) => d.key === v)?.label}`, 'success')
}

onMounted(() => {
  theme.value = localStorage.getItem('sb:theme') || 'dark'
  density.value = localStorage.getItem('sb:density') || 'comfortable'
  applyTheme(theme.value)
  applyDensity(density.value)
})
</script>

<template>
  <div class="personalize-stack">
    <div class="card">
      <div class="card-title"><span>🎨</span> 主题</div>
      <div class="opt-grid">
        <button
          v-for="t in THEMES"
          :key="t.key"
          class="opt-card"
          :class="{ active: theme === t.key }"
          @click="setTheme(t.key)"
        >
          <div class="opt-name">{{ t.label }}</div>
          <div class="opt-desc">{{ t.desc }}</div>
        </button>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>📐</span> 界面密度</div>
      <div class="opt-grid">
        <button
          v-for="d in DENSITIES"
          :key="d.key"
          class="opt-card"
          :class="{ active: density === d.key }"
          @click="setDensity(d.key)"
        >
          <div class="opt-name">{{ d.label }}</div>
          <div class="opt-desc">{{ d.desc }}</div>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.personalize-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.opt-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.opt-card {
  text-align: left;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: var(--bg-elevated);
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.opt-card:hover {
  border-color: var(--accent-border);
}

.opt-card.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.opt-name {
  font-size: 13.5px;
  font-weight: 600;
  margin-bottom: 4px;
}

.opt-desc {
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.5;
}

@media (max-width: 720px) {
  .opt-grid {
    grid-template-columns: 1fr;
  }
}
</style>
