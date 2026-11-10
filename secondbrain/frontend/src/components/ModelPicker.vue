<script setup>
/**
 * 就地模型控件（v0.12）：一条 AI 链路 = provider + 模型 + 深度思考开关。
 *
 * **按链路独立**：
 *   · 提取链（采集区）→ 可单独选本机 ollama，免费、快，适合批量提取
 *   · 创作链（加工区）→ 可单独选 API，质量优先
 *   两条链各存各的 ai.provider_extract / ai.provider_create，互不影响。
 *
 * 控件里可以直接切「本地 / API」：切了之后模型列表跟着换成该 provider
 * 的型号（本地读 /api/tags，API 用预设 + 手输），存盘即生效、无需重启。
 */
import { ref, onMounted, computed } from 'vue'
import { settingsApi } from '../api/client'

const props = defineProps({
  /** extract = 素材提取链；create = AI 创作链 */
  kind: { type: String, required: true },
  /** 控件标题，如「提取模型」 */
  label: { type: String, default: '' }
})

const models = ref([])
const providers = ref({})
const currentModel = ref('')
const think = ref(false)
const provider = ref('')
const thinkSupported = ref(true)
const loaded = ref(false)
const saving = ref(false)
const savedAt = ref(0)
const err = ref('')

const isCreate = computed(() => props.kind === 'create')

async function load() {
  try {
    const d = await settingsApi.getAiModel()
    providers.value = d.providers || {}
    const ch = (d.chains || {})[props.kind] || {}
    provider.value = ch.provider || d.provider || 'ollama'
    models.value = ch.models || []
    currentModel.value = ch.model || ''
    think.value = !!ch.think
    thinkSupported.value = ch.think_supported !== false
    err.value = ch.error || (d.ok ? '' : d.error) || ''
    loaded.value = true
  } catch (e) {
    err.value = String(e.message || e).slice(0, 80)
  }
}

async function save() {
  saving.value = true
  try {
    await settingsApi.saveAiModel({
      kind: props.kind,
      provider: provider.value,
      model: currentModel.value,
      think: think.value
    })
    savedAt.value = Date.now()
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e).slice(0, 80)
  } finally {
    saving.value = false
  }
}

/** 切 provider：模型列表换成对方的，模型名重置为该 provider 的主型号。 */
function switchProvider(target) {
  if (!target || target === provider.value) return
  provider.value = target
  const info = providers.value[target] || {}
  models.value = info.models || []
  currentModel.value = info.default_model || ''
  thinkSupported.value = target === 'ollama'
  if (!thinkSupported.value) think.value = false
  save()
}

function onModelChange() {
  save()
}
function onThinkChange() {
  save()
}

onMounted(load)
</script>

<template>
  <div class="model-picker" :class="kind">
    <span class="mp-label">{{ label || (isCreate ? '创作模型' : '提取模型') }}</span>

    <!-- 链路类型：本地（ollama）/ API（OpenAI 兼容端点） -->
    <select
      class="mp-prov"
      :value="provider"
      :disabled="!loaded"
      title="这条链走本地还是 API（两条链可各选各的）"
      @change="switchProvider($event.target.value)"
    >
      <option value="ollama">本地</option>
      <option value="openai_compat">API</option>
    </select>

    <!-- datalist：既能下拉选预设，也能手输任意型号（API 链的型号经常变） -->
    <input
      v-model="currentModel"
      class="mp-select"
      :list="`mp-models-${kind}`"
      :disabled="!loaded"
      :placeholder="loaded ? '模型（空=该端默认）' : '加载中…'"
      title="选择或输入这条链路使用的模型；留空则用该端的主模型"
      @change="onModelChange"
    />
    <datalist :id="`mp-models-${kind}`">
      <option v-for="m in models" :key="m" :value="m" />
    </datalist>

    <label
      v-if="thinkSupported"
      class="mp-think"
      :title="isCreate ? '开 = 质量优先，耗时约 3 倍' : '关 = 快，适合批量提取'"
    >
      <input v-model="think" type="checkbox" @change="onThinkChange" />
      <span>深度思考</span>
    </label>
    <span
      v-else
      class="mp-think mp-disabled"
      title="API 模型的思考由模型名决定（带 thinking 的型号自动思考）"
    >
      <span>🔒 思考随型号</span>
    </span>

    <transition name="mp-fade">
      <span v-if="savedAt && Date.now() - savedAt < 4000" class="mp-saved">✓ 已生效</span>
    </transition>
    <span v-if="err" class="mp-err" :title="err">⚠</span>
  </div>
</template>

<style scoped>
.model-picker {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  /* 高度锁 32px，与同排的下拉框 / 按钮一致（用户：元素格式要整齐）。
     左右 padding 用 8px，纵向靠 align-items:center 居中，不再靠 padding 撑。 */
  height: 32px;
  padding: 0 8px;
  border: 1px solid var(--border, #33353d);
  border-radius: 8px;
  background: var(--bg-elevated, #22242b);
  font-size: 12px;
  /* **不换行**（用户 2026-11-09：模型控件不要双行）。
     原来是 flex-wrap: wrap，容器一窄就折成两行，视觉上很乱。
     现在整条永远单行。 */
  flex-wrap: nowrap;
  white-space: nowrap;
  /* 整条**不可收缩**：内部元素一旦被压窄，内容就会溢出盒子、
     盖到后面的控件上（曾把「采集音频」勾选框压在下面）。
     宁可让外层整行折行，也不要控件自身溢出。 */
  flex: 0 0 auto;
}

.mp-label {
  color: var(--text-dim, #9a9daa);
  white-space: nowrap;
}

.mp-prov,
.mp-select {
  /* 高度与外层 32px 协调：24px 内容 + 上下 1px 边框 = 26px，
     放在 32px 高的容器里垂直居中，与同排 select/button 基线齐平。 */
  height: 24px;
  padding: 0 6px;
  border-radius: 6px;
  border: 1px solid var(--border, #33353d);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-size: 12px;
  line-height: 1;
  /* **必须显式给宽度**。
     全局样式有 `input[type='text'], select { width: 100% }`，
     会把下拉框撑成 100% 宽 —— 在 flex 里就成了"无限宽"，
     把后面的型号输入框和思考开关全挤出/压叠到一行上。
     这里两个都用 auto，由内容决定宽度。 */
  width: auto;
}

.mp-prov {
  /* 固定不伸缩：宽度按内容（「本地」/「API」）自适应。 */
  flex: 0 0 auto;
}

.mp-select {
  /* 型号名长短差异很大（doubao-seed-2-1-pro-260915 vs qwen3.5-9b），
     不限宽的话长型号会把整行按钮撑开。固定宽度 + 原生省略最稳。
     固定 190px 且**不收缩**（flex:none）—— 之前给 flex-shrink，
     输入框被压窄后文字溢出到框外，把后面的控件盖住了。 */
  width: 190px;
  max-width: 190px;
  flex: 0 0 190px;
}

.mp-think {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  white-space: nowrap;
  color: var(--text, #e6e7ea);
}

.mp-think input {
  accent-color: var(--ai, #7c6cf0);
}

.mp-disabled {
  cursor: help;
  color: var(--text-dim, #9a9daa);
}

.mp-saved {
  color: var(--ok, #52c41a);
  white-space: nowrap;
}

.mp-err {
  color: var(--danger, #e5484d);
  cursor: help;
}

.mp-fade-enter-active,
.mp-fade-leave-active {
  transition: opacity 0.3s ease;
}

.mp-fade-enter-from,
.mp-fade-leave-to {
  opacity: 0;
}
</style>
