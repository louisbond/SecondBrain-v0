<script setup>
/**
 * 提示词编辑器（通用）：一条链路一份可编辑提示词。
 *
 * scope：
 *   · collect → 采集模板（prompts/collect.txt）
 *   · extract → 面向接口（prompts/extract.txt，发给模型的原始 prompt）
 *
 * 保存后下一次调用即生效（后端每次现读文件），不用重启。
 */
import { ref, onMounted } from 'vue'
import { settingsApi } from '../../api/client'

const props = defineProps({
  scope: { type: String, required: true },
  title: { type: String, default: '' }
})

const text = ref('')
const defaultText = ref('')
const custom = ref(false)
const placeholders = ref([])
const busy = ref(false)
const msg = ref('')
const showDefault = ref(false)

async function load() {
  try {
    const d = await settingsApi.getPrompts()
    const e = (d && d[props.scope]) || {}
    text.value = e.text || ''
    defaultText.value = e.default || ''
    custom.value = !!e.custom
    placeholders.value = e.placeholders || []
  } catch (err) {
    msg.value = '读取失败：' + String(err.message || err).slice(0, 80)
  }
}

async function save() {
  busy.value = true
  msg.value = ''
  try {
    await settingsApi.savePrompts({ scope: props.scope, text: text.value })
    custom.value = true
    msg.value = '已保存，下一次调用立即生效'
  } catch (err) {
    msg.value = '保存失败：' + String(err.message || err).slice(0, 120)
  } finally {
    busy.value = false
  }
}

async function reset() {
  if (!confirm('恢复为内置默认提示词？当前自定义内容会丢失。')) return
  busy.value = true
  msg.value = ''
  try {
    await settingsApi.resetPrompts(props.scope)
    await load()
    msg.value = '已恢复默认'
  } catch (err) {
    msg.value = '恢复失败：' + String(err.message || err).slice(0, 120)
  } finally {
    busy.value = false
  }
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <div class="prompt-editor">
    <div class="pe-head">
      <b>{{ title }}</b>
      <span v-if="custom" class="badge accent">已自定义</span>
      <span v-else class="badge">默认</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" @click="showDefault = !showDefault">
        {{ showDefault ? '隐藏默认' : '查看内置默认' }}
      </button>
      <button class="btn ghost sm" :disabled="busy || !custom" @click="reset">
        恢复默认
      </button>
      <button class="btn" :disabled="busy" @click="save">保存</button>
    </div>
    <div v-if="placeholders.length" class="dim pe-hint">
      可用占位符：<span class="mono">{{ placeholders.join('  ') }}</span>
    </div>
    <textarea v-model="text" class="pe-box" spellcheck="false" placeholder="加载中…"></textarea>
    <pre v-if="showDefault" class="pe-default">{{ defaultText }}</pre>
    <div v-if="msg" class="dim pe-msg">{{ msg }}</div>
  </div>
</template>

<style scoped>
.prompt-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pe-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pe-hint {
  font-size: 11.5px;
}

.pe-box {
  width: 100%;
  min-height: 320px;
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--border, #33353d);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.65;
  resize: vertical;
}

.pe-default {
  margin: 0;
  max-height: 240px;
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

.pe-msg {
  font-size: 12px;
}
</style>
