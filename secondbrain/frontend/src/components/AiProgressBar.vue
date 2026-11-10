<script setup>
/**
 * 全局 AI 进度条（v0.9.6）。
 *
 * 三个诉求（用户提的）：
 *   1. 秒数要一秒接一秒地走 —— 本地秒表每秒 +1，后端每 2 秒校准一次防漂移；
 *   2. 要有估计时长 —— 后端按「字数 × 思考开关 × 4.5 tok/s」估 est_s；
 *   3. 要有进度 —— percent = 已用 / 预计（封顶 98%），配一条细进度条。
 *
 * 数据来自 /api/workbench/progress 的 `ai` 字段（后端 ai_activity 登记）。
 * 只要后端在跑 AI，无论切到哪个页面，这条都挂在右上角。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { workbenchApi } from '../api/client'

const ai = ref({ active: false })
/** 本地秒表：每秒自增，轮询回来用服务端值校准 */
const localS = ref(0)
let pollTimer = null
let tickTimer = null

function fmt(sec) {
  const s = Math.max(0, Math.round(sec || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${pad(m)}:${pad(ss)}`
}

const elapsedText = computed(() => fmt(localS.value))
const etaText = computed(() => {
  const est = ai.value.est_s
  if (!est) return ''
  const left = Math.max(0, est - localS.value)
  return left > 1 ? fmt(left) : '即将完成'
})
const percent = computed(() => {
  const est = ai.value.est_s
  if (!est || est <= 0) return null
  return Math.min(98, Math.round((localS.value / est) * 100))
})

async function poll() {
  try {
    const d = await workbenchApi.progress()
    const next = d && d.ai ? d.ai : { active: false }
    const wasActive = ai.value.active
    ai.value = next
    if (next.active) {
      // 校准本地秒表（服务端是权威值）
      localS.value = Number(next.elapsed_s) || localS.value
    } else if (wasActive) {
      localS.value = 0
    }
  } catch {
    // 轮询失败不该打扰用户：后端可能在重启，下一轮会自己恢复。
  }
}

onMounted(() => {
  poll()
  pollTimer = setInterval(poll, 2000)
  // 本地秒表：每秒 +1，保证数字连续跳动（轮询 2 秒一次会顿一格）
  tickTimer = setInterval(() => {
    if (ai.value.active) localS.value += 1
  }, 1000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (tickTimer) clearInterval(tickTimer)
})
</script>

<template>
  <Transition name="ai-slide">
    <div v-if="ai.active" class="ai-progress" role="status" aria-live="polite">
      <div class="ai-row">
        <span class="ring"></span>
        <span class="label">{{ ai.label || 'AI 处理中' }}</span>
        <span class="elapsed">{{ elapsedText }}</span>
        <span v-if="percent !== null" class="pct">{{ percent }}%</span>
        <span v-if="etaText" class="eta">预计还需 {{ etaText }}</span>
        <span v-else class="hint">本地模型较慢，请勿关闭窗口</span>
      </div>
      <div v-if="percent !== null" class="track">
        <div class="fill" :style="{ width: percent + '%' }"></div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.ai-progress {
  position: fixed;
  top: 14px;
  right: 18px;
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 14px 9px;
  border-radius: 14px;
  border: 1px solid var(--ai, #7c6cf0);
  background: var(--bg-panel, #1b1c22);
  box-shadow: 0 6px 20px rgb(0 0 0 / 35%);
  font-size: 12.5px;
  color: var(--text, #e6e7ea);
  min-width: 300px;
  max-width: 70vw;
}

.ai-row {
  display: flex;
  align-items: center;
  gap: 9px;
}

.ring {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--ai-soft, rgb(124 108 240 / 30%));
  border-top-color: var(--ai, #7c6cf0);
  animation: ai-spin 0.8s linear infinite;
  flex: none;
}

.label {
  font-weight: 600;
}

.elapsed {
  font-variant-numeric: tabular-nums;
  color: var(--ai, #7c6cf0);
  font-weight: 600;
}

.pct {
  font-variant-numeric: tabular-nums;
  color: var(--text-dim, #9a9daa);
}

.eta {
  color: var(--text-dim, #9a9daa);
  font-size: 11.5px;
}

.hint {
  color: var(--text-dim, #9a9daa);
  font-size: 11.5px;
}

.track {
  height: 3px;
  border-radius: 2px;
  background: var(--ai-soft, rgb(124 108 240 / 18%));
  overflow: hidden;
}

.fill {
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--ai, #7c6cf0), #9d8cff);
  transition: width 0.9s linear;
}

@keyframes ai-spin {
  to {
    transform: rotate(360deg);
  }
}

.ai-slide-enter-active,
.ai-slide-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.ai-slide-enter-from,
.ai-slide-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
