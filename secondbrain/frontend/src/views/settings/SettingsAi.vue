<script setup>
/**
 * 设置 · AI 组（v42 从 SettingsView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { settingsApi } from '../../api/client'
import { useSettings } from '../../composables/useSettings'

const {
  settings,
  providers,
  arkState,
  arkKey,
  arkBaseUrl,
  arkModel,
  load,
  applyPreset,
  saveArkKey,
  clearArkKey,
  switchProvider,
  resetProvider
} = useSettings()

// AI 组专属的端点预设（原来在 SettingsView 的 setup 里）
const ENDPOINT_PRESETS = [
  {
    name: '豆包（火山方舟）',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    model: 'doubao-1-5-thinking-pro-250415'
  },
  {
    name: 'DeepSeek 官方',
    base_url: 'https://api.deepseek.com/v1',
    model: 'deepseek-reasoner'
  },
  {
    name: 'DeepSeek（硅基流动）',
    base_url: 'https://api.siliconflow.cn/v1',
    model: 'deepseek-ai/DeepSeek-R1'
  }
]
</script>

<template>
  <div class="settings-group">
    <!-- Provider -->
    <div class="card">
      <div class="card-title"><span>🤖</span> AI Provider</div>
      <div class="row wrap" style="margin-bottom: 10px">
        <span class="muted">当前：</span>
        <span class="badge accent">{{ settings.ai.provider }}</span>
        <span v-if="providers?.fallback_active" class="badge warn">
          已自动降级到 {{ providers.current }}
        </span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="resetProvider">恢复主链</button>
      </div>

      <!-- Provider 列表：内部滚动。
             链的数量不固定（ollama / openai_compat …），不设上限的话
             页面高度会随链数增长，破坏「每页内容高度 ≤ 视口」。
             v34：workbuddy 那条链已移除。 -->
      <div class="provider-list">
        <div
          v-for="p in providers?.providers || []"
          :key="p.name"
          class="list-item"
          style="cursor: pointer; align-items: flex-start"
          @click="switchProvider(p.name)"
        >
          <div class="grow">
            <div class="title">
              {{ p.name }}
              <span v-if="p.active" class="badge accent">当前</span>
            </div>
            <div class="sub">{{ p.model }}</div>
            <div class="sub" :style="{ color: p.ok ? 'var(--ok)' : 'var(--danger)' }">
              {{ p.ok ? '✓ 可用' : '✗ 不可用' }} · {{ p.latency_ms }}ms
            </div>
            <div v-if="p.detail" class="sub wrap" style="font-size: 11px">
              {{ p.detail }}
            </div>
          </div>
        </div>
      </div>

      <div class="row" style="margin-top: 12px">
        <label class="row" style="margin: 0; cursor: pointer">
          <input
            type="checkbox"
            :checked="settings.ai.auto_fallback"
            style="width: auto"
            @change="settingsApi.setAutoFallback($event.target.checked).then(load)"
          />
          自动降级（连续失败 {{ settings.ai.fallback_threshold }} 次或 429 时切 Ollama）
        </label>
      </div>

      <div class="row" style="margin-top: 8px">
        <label class="row" style="margin: 0; cursor: pointer">
          <input
            type="checkbox"
            :checked="settings.ai.auto_start_ollama"
            style="width: auto"
            @change="settingsApi.setAutoStartOllama($event.target.checked).then(load)"
          />
          自动拉起本机 Ollama（切到 Ollama 或启动时发现未运行，就代为启动）
        </label>
      </div>

      <div class="alert info" style="margin-top: 12px">
        embedding：{{ settings.ai.embedding_model }}
        <span :class="settings.ai.embedding_real ? 'badge ok' : 'badge warn'">
          {{ settings.ai.embedding_real ? '真实模型' : '降级哈希后端' }}
        </span>
      </div>
    </div>

    <!-- OpenAI 兼容端点（火山方舟） -->
    <div class="card">
      <div class="card-title"><span>🔑</span> API 密钥（OpenAI 兼容端点）</div>
      <div class="dim" style="font-size: 12px; margin-bottom: 10px">
        适用于火山方舟等 OpenAI 兼容服务。密钥只写入
        <span class="mono">config/secrets.json</span>，不落 config.toml。
        <span v-if="arkState?.configured" class="badge ok" style="margin-left: 6px">
          已配置 {{ arkState.masked }}
        </span>
        <span v-else class="badge warn" style="margin-left: 6px">未配置</span>
      </div>
      <!-- 预设端点：一键填 base_url + 模型名 -->
      <div class="row wrap" style="margin-bottom: 8px">
        <span class="dim" style="font-size: 11.5px">常用预设：</span>
        <button
          v-for="p in ENDPOINT_PRESETS"
          :key="p.name"
          class="btn ghost sm"
          @click="applyPreset(p)"
        >
          {{ p.name }}
        </button>
      </div>
      <div class="row wrap">
        <input
          v-model="arkBaseUrl"
          type="text"
          placeholder="base_url，如 https://ark.cn-beijing.volces.com/api/v3"
          style="flex: 1; min-width: 260px"
        />
      </div>
      <div class="row wrap" style="margin-top: 8px">
        <input
          v-model="arkModel"
          type="text"
          placeholder="模型 ID，如 doubao-seed-2-1-pro-260915"
          style="flex: 1; min-width: 220px"
        />
        <input
          v-model="arkKey"
          type="password"
          placeholder="API Key（留空则保留已保存的）"
          style="flex: 1; min-width: 220px"
        />
      </div>
      <div class="row" style="margin-top: 10px">
        <button class="btn ghost" @click="saveArkKey(false)">仅保存</button>
        <button class="btn" @click="saveArkKey(true)">保存并切换到该链</button>
        <div class="spacer" style="flex: 1"></div>
        <button v-if="arkState?.configured" class="btn ghost sm" @click="clearArkKey">
          清除密钥
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Provider 列表：内部滚动，链数再多也不撑破页面高度 */
.provider-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
  max-height: 176px;
  overflow-y: auto;
}
</style>
