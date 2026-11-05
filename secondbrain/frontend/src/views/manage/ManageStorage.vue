<script setup>
/**
 * 设置 → 采集与存储（v41 从 ManageView.vue 拆出）。
 *
 * 拆分前 ManageView.vue 有 952 行：6 个页签的状态、API 调用、模板全挤在
 * 一个 setup 里，改「创作模板」要在 900 行里找对应变量。
 *
 * 现在每个页签是一个自治组件：自己持有状态、自己 onMounted 拉数据。
 * 父组件只管 tab 切换 + KeepAlive 缓存。
 * 共享的只有 inject('toast')（App.vue 提供），不需要 props 穿透。
 *
 * 注意：本文件的模板/逻辑是从原文件**逐行切片**搬来的，未改业务逻辑。
 */
import { ref, onMounted } from 'vue'
import { settingsApi } from '../../api/client'
import SettingsView from '../SettingsView.vue'

// 平台适配器能力（从设置区搬来的只读展示）
const adapters = ref([])
const supportLevels = {
  full: { label: '完整可用', cls: 'ok' },
  partial: { label: '部分可用', cls: 'warn' },
  unstable: { label: '不稳定', cls: 'danger' },
  manual: { label: '仅手动兜底', cls: '' }
}

/** 平台适配器能力（只读清单，来自 /api/settings 的 adapters 字段）。 */
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
  <div class="manage-stack manage-section">
    <SettingsView embedded group="storage" />
    <div class="card">
      <div class="card-title"><span>📡</span> 平台适配器能力</div>
      <div class="list-item" v-for="a in adapters" :key="a.name">
        <div class="grow">
          <div class="title">{{ a.label }}</div>
          <div class="sub">
            <span v-if="a.supports_subtitle">字幕 ✓</span>
            <span v-if="a.supports_audio"> · 音频 ✓</span>
          </div>
        </div>
        <span class="badge" :class="supportLevels[a.support_level]?.cls">
          {{ supportLevels[a.support_level]?.label || a.support_level }}
        </span>
      </div>
      <div v-if="!adapters.length" class="dim">没有可用的适配器</div>
    </div>
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.manage-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
