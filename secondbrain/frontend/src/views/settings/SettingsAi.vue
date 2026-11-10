<script setup>
/**
 * 设置 · AI 组（v42 从 SettingsView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { ref, onMounted } from 'vue'
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

// -- 提取链提示词模板 ------------------------------------------------------
// 创作链（按模板生成）的提示词在「管理 → 解析模板」里逐模板编辑，不在这里。
const promptText = ref('')
const promptDefault = ref('')
const promptCustom = ref(false)
const promptPh = ref([])
const promptBusy = ref(false)
const promptMsg = ref('')
const showDefault = ref(false)

async function loadPrompts() {
  try {
    const d = await settingsApi.getPrompts()
    const e = d.extract || {}
    promptText.value = e.text || ''
    promptDefault.value = e.default || ''
    promptCustom.value = !!e.custom
    promptPh.value = e.placeholders || []
  } catch (err) {
    promptMsg.value = '读取失败：' + String(err.message || err).slice(0, 80)
  }
}

async function savePrompt() {
  promptBusy.value = true
  promptMsg.value = ''
  try {
    await settingsApi.savePrompts({ text: promptText.value })
    promptCustom.value = true
    promptMsg.value = '已保存，下一次 AI 提取立即生效（无需重启）'
  } catch (err) {
    promptMsg.value = '保存失败：' + String(err.message || err).slice(0, 120)
  } finally {
    promptBusy.value = false
  }
}

async function resetPrompt() {
  if (!confirm('恢复为内置默认提示词？当前自定义内容会丢失。')) return
  promptBusy.value = true
  promptMsg.value = ''
  try {
    await settingsApi.resetPrompts()
    await loadPrompts()
    promptMsg.value = '已恢复默认'
  } catch (err) {
    promptMsg.value = '恢复失败：' + String(err.message || err).slice(0, 120)
  } finally {
    promptBusy.value = false
  }
}

onMounted(loadPrompts)
</script>

<template>
  <div class="settings-group">
    <!-- OpenAI 兼容端点（火山方舟） -->
    <div class="card">
      <div class="card-title"><span>🔑</span> API 密钥（OpenAI 兼容端点）</div>
      <div class="dim" style="font-size: 12px; margin-bottom: 10px">
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
        <button class="btn" @click="saveArkKey(true)">保存并让两条链都用它</button>
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

.prompt-box {
  width: 100%;
  min-height: 190px;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid var(--border, #33353d);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}

.prompt-default {
  margin-top: 10px;
  max-height: 220px;
  overflow: auto;
  padding: 10px;
  border-radius: 8px;
  border: 1px dashed var(--border, #33353d);
  background: var(--bg-input, #1b1c22);
  color: var(--text-dim, #9a9daa);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
}
</style>
