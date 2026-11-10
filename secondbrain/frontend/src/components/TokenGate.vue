<script setup>
/**
 * 局域网访问口令入口（v0.7.6）。
 *
 * 背景：手机端经常不是从「带 ?token= 的完整链接」进来的 —— 手输 IP、
 * 微信/QQ 内置浏览器吞掉 query、清理过存储、换过标签页，都会让 /api 全部 401。
 * 旧行为是每个卡片各甩一条红字「token 无效或缺失」，用户完全不知道该怎么办。
 *
 * 现在由 client.js 在 401 时广播 sb:auth-required，这里弹一个统一入口：
 * 粘贴完整链接或口令 → 写 localStorage + cookie → 刷新，一次解决。
 */
import { ref, onMounted, onUnmounted } from 'vue'
import { setToken } from '../api/client'

const visible = ref(false)
const input = ref('')
const error = ref('')
const origin = window.location.origin

function onAuthRequired() {
  visible.value = true
}

/** 既接受完整链接，也接受光秃秃的口令；粘贴错东西要给出可操作的提示。 */
function extractToken(text) {
  const raw = (text || '').trim()
  if (!raw) return ''
  try {
    const url = new URL(raw)
    return url.searchParams.get('token') || ''
  } catch {
    // 不是 URL：当成口令本体（允许有空格，自动去掉）
    return raw.replace(/\s+/g, '')
  }
}

function submit() {
  const token = extractToken(input.value)
  if (!token) {
    error.value = '没读到口令。请粘贴电脑上的完整链接（含 ?token=...），或只粘贴口令。'
    return
  }
  setToken(token)
  window.location.reload()
}

onMounted(() => window.addEventListener('sb:auth-required', onAuthRequired))
onUnmounted(() => window.removeEventListener('sb:auth-required', onAuthRequired))
</script>

<template>
  <div v-if="visible" class="gate-mask">
    <div class="gate-card">
      <div class="gate-icon">🔒</div>
      <h2>需要访问口令</h2>

      <div class="gate-how">
        <div class="gate-how-title">在电脑上拿到链接</div>
        <div>设置 → 维护与系统 → 网络 → 「复制带 token 的手机链接」</div>
      </div>

      <input
        v-model="input"
        type="text"
        class="gate-input"
        placeholder="粘贴完整链接或口令"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        @keyup.enter="submit"
      />
      <div v-if="error" class="gate-error">{{ error }}</div>

      <button class="btn gate-submit" @click="submit">保存并重新加载</button>

      <div class="gate-origin">当前地址：{{ origin }}</div>
    </div>
  </div>
</template>

<style scoped>
.gate-mask {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(8, 10, 14, 0.86);
  backdrop-filter: blur(4px);
}

.gate-card {
  width: 100%;
  max-width: 420px;
  max-height: 92vh;
  overflow-y: auto;
  padding: 22px;
  border-radius: 14px;
  border: 1px solid var(--border-light, #363c4d);
  background: var(--bg-panel, #161920);
  box-shadow: var(--shadow, 0 8px 28px rgba(0, 0, 0, 0.45));
}

.gate-icon {
  font-size: 28px;
}

h2 {
  margin: 8px 0 8px;
  font-size: 18px;
}

.gate-lead {
  margin: 0 0 14px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-muted, #9aa2b4);
}

.gate-how {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted, #9aa2b4);
}

.gate-how-title {
  font-weight: 600;
  color: var(--text, #e6e8ee);
  margin-bottom: 2px;
}

.gate-input {
  width: 100%;
  margin-top: 14px;
  padding: 11px 12px;
  border-radius: 8px;
  border: 1px solid var(--border, #2a2f3d);
  background: var(--bg-input, #11141a);
  color: var(--text, #e6e8ee);
  font-size: 16px; /* iOS 聚焦不自动放大 */
  outline: none;
}

.gate-input:focus {
  border-color: var(--accent-border, rgba(108, 140, 255, 0.35));
}

.gate-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--danger, #f87171);
  line-height: 1.6;
}

.gate-submit {
  width: 100%;
  margin-top: 12px;
  min-height: 40px;
}

.gate-origin {
  margin-top: 12px;
  font-size: 11px;
  text-align: center;
  color: var(--text-dim, #6b7387);
  word-break: break-all;
}
</style>
