<script setup>
/**
 * 常规设置（v0.10.9）：原「采集与存储」的有用部分 + 原「维护与系统」合并。
 *
 * 去掉的：第三方解析 API + 测试解析（用户要求，公共接口已基本失效）。
 * 保留的：平台适配能力 / 登录态 Cookie / Vault 与索引 / 语音转写 / 网络 / 自启与电源 / 服务。
 */
import { ref, onMounted, inject } from 'vue'
import { settingsApi } from '../../api/client'
import { provideSettings } from '../../composables/useSettings'
import SettingsAi from '../settings/SettingsAi.vue'
import SettingsStorage from '../settings/SettingsStorage.vue'
import SettingsSystem from '../settings/SettingsSystem.vue'
import VaultPanel from './VaultPanel.vue'

// SettingsStorage / SettingsSystem 内部用 useSettings()，必须有上层 provide
const { settings } = provideSettings()

const toast = inject('toast')

// -- 适配器实测：拿一条真实链接跑一次，验证「部分可用」到底能不能用 ------
const probeUrl = ref('')
const probing = ref(false)
const probeResult = ref(null)

async function doProbe() {
  const u = probeUrl.value.trim()
  if (!u) {
    toast?.('先粘贴一条分享链接', 'warn')
    return
  }
  probing.value = true
  probeResult.value = null
  try {
    probeResult.value = await settingsApi.probeAdapter(u)
  } catch (e) {
    probeResult.value = { ok: false, error: String(e.message || e).slice(0, 160) }
  } finally {
    probing.value = false
  }
}

const adapters = ref([])
const supportLevels = {
  full: { label: '完整可用', cls: 'ok' },
  partial: { label: '部分可用', cls: 'warn' },
  unstable: { label: '不稳定', cls: 'danger' },
  manual: { label: '仅手动兜底', cls: '' }
}

async function loadAdapters() {
  try {
    const data = await settingsApi.get()
    adapters.value = data.adapters || []
  } catch {
    adapters.value = []
  }
}

onMounted(loadAdapters)
</script>

<template>
  <div class="general-stack">
    <!-- 平台适配能力 -->
    <div v-if="adapters.length" class="card">
      <div class="card-title">
        <span>📡</span><span class="title-text">平台适配能力</span>
        <span class="title-note">本地直采 · 不外发链接</span>
      </div>
      <div class="adapter-grid">
        <div v-for="a in adapters" :key="a.name" class="adapter-cell">
          <span class="level-dot" :class="supportLevels[a.support_level]?.cls"></span>
          <div class="grow">
            <div class="adapter-name">{{ a.label }}</div>
            <div class="adapter-meta">
              <span :class="{ on: a.supports_subtitle }"
                >字幕 {{ a.supports_subtitle ? '✓' : '—' }}</span
              >
              <span :class="{ on: a.supports_audio }"
                >音频 {{ a.supports_audio ? '✓' : '—' }}</span
              >
            </div>
          </div>
          <span class="badge" :class="supportLevels[a.support_level]?.cls">{{
            supportLevels[a.support_level]?.label || a.support_level
          }}</span>
        </div>
      </div>

      <!-- 实测：声明值不等于此刻能抓，拿一条真链接跑一次 -->
      <div class="probe-row">
        <input
          v-model="probeUrl"
          type="text"
          placeholder="粘贴一条 B站/抖音/快手/小红书/网页链接，实测能否抓到"
        />
        <button class="btn" :disabled="probing" @click="doProbe">
          {{ probing ? '实测中…' : '实测' }}
        </button>
      </div>
      <div v-if="probeResult" class="probe-result" :class="{ bad: !probeResult.ok }">
        <template v-if="probeResult.ok">
          ✓ <b>{{ probeResult.platform }}</b> 抓取成功 ·
          {{ probeResult.chars }} 字
          <span v-if="probeResult.asr"> · 有字幕/转写</span>
          <div class="dim" style="margin-top: 3px">
            {{ probeResult.title }} — {{ probeResult.preview }}
          </div>
          <div class="probe-facts">
            <span class="probe-fact" :class="probeResult.cookie_used ? 'on' : 'off'">
              Cookie {{ probeResult.cookie_used ? '已带' : '未带' }}
            </span>
            <span class="probe-fact" :class="probeResult.media_count ? 'on' : 'off'">
              视频直链 {{ probeResult.media_count || 0 }} 条
              {{ probeResult.media_count ? '（会转写口播）' : '（拿不到字幕）' }}
            </span>
            <span class="probe-fact" :class="probeResult.complete ? 'on' : 'warn'">
              {{ probeResult.complete ? '正文完整' : '只有标题/简介' }}
            </span>
          </div>
        </template>
        <template v-else>
          ✕ <b>{{ probeResult.platform || '未知平台' }}</b> 抓取失败：{{ probeResult.error }}
          <div class="probe-facts">
            <span class="probe-fact" :class="probeResult.cookie_used ? 'on' : 'off'">
              Cookie {{ probeResult.cookie_used ? '已带' : '未带' }}
            </span>
          </div>
        </template>
      </div>
    </div>

    <SettingsAi />

    <!-- 存量笔记索引（scan / 批量补 AI），原来只有后端没有界面 -->
    <VaultPanel />

    <!-- settings 是异步加载的：没到之前不能渲染下面两个组件，
         它们会直接读 settings.vault / settings.web，null 会当场抛错 -->
    <template v-if="settings">
      <SettingsStorage />
      <SettingsSystem />
    </template>
    <div v-else class="card dim" style="font-size: 12.5px">正在加载系统配置…</div>
  </div>
</template>

<style scoped>
.general-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.title-text {
  font-weight: 600;
}

.title-note {
  margin-left: 8px;
  font-size: 11.5px;
  font-weight: 400;
  color: var(--text-dim);
}

.adapter-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.adapter-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-elevated);
}

.level-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
  background: var(--text-dim);
}

.level-dot.ok {
  background: var(--ok);
}

.level-dot.warn {
  background: var(--warn);
}

.level-dot.danger {
  background: var(--danger);
}

.adapter-name {
  font-size: 12.5px;
  font-weight: 600;
}

.adapter-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--text-dim);
}

.adapter-meta .on {
  color: var(--ok);
}

.probe-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.probe-row input {
  flex: 1;
  min-width: 0;
}

.probe-result {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border: 1px solid var(--ok);
  color: var(--text);
}

.probe-result.bad {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border-color: var(--danger);
}

/* 实测结论：Cookie / 视频直链 / 正文完整性 —— 让用户自己看出
   「配了 Cookie 到底有没有用」和「能不能拿到字幕」 */
.probe-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.probe-fact {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
  color: var(--text-dim);
  background: var(--bg-elevated);
}

.probe-fact.on {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok) 45%, transparent);
  background: color-mix(in srgb, var(--ok) 10%, transparent);
}

.probe-fact.warn {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 45%, transparent);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}

.probe-fact.off {
  color: var(--text-dim);
  opacity: 0.75;
}

@media (max-width: 720px) {
  .adapter-grid {
    grid-template-columns: 1fr;
  }
}
</style>
