<script setup>
/**
 * 设置面板（v42 拆成主壳 + 三组子组件 + 注入上下文）。
 *
 * 既可被 /settings 单独使用，也可被管理区按组嵌入：
 *   <SettingsView embedded group="ai" />
 * 嵌入时由外层 tab 决定分组，不画自己的 tab 条。
 */
import { ref, computed } from 'vue'
import { provideSettings } from '../composables/useSettings'
import SettingsAi from './settings/SettingsAi.vue'
import SettingsCollect from './settings/SettingsCollect.vue'
import SettingsStorage from './settings/SettingsStorage.vue'
import SettingsSystem from './settings/SettingsSystem.vue'

const props = defineProps({
  embedded: { type: Boolean, default: false },
  group: { type: String, default: '' }
})

// 本地状态不能叫 group（props 已有），否则 vue/no-dupe-keys
const localGroup = ref('ai')
const activeGroup = computed(() => props.group || localGroup.value)

const { settings } = provideSettings()
</script>

<template>
  <div class="settings-page">
    <div v-if="!settings" class="empty">
      <div class="icon"><span class="spin">◌</span></div>
      <div>加载中……</div>
    </div>

    <template v-else>
      <div v-if="!embedded" class="tabs" style="margin-bottom: 14px">
        <button class="tab" :class="{ active: localGroup === 'ai' }" @click="localGroup = 'ai'">
          AI
        </button>
        <button
          class="tab"
          :class="{ active: localGroup === 'collect' }"
          @click="localGroup = 'collect'"
        >
          采集
        </button>
        <button
          class="tab"
          :class="{ active: localGroup === 'storage' }"
          @click="localGroup = 'storage'"
        >
          存储
        </button>
        <button
          class="tab"
          :class="{ active: localGroup === 'system' }"
          @click="localGroup = 'system'"
        >
          系统
        </button>
      </div>

      <SettingsAi v-if="activeGroup === 'ai'" />
      <SettingsCollect v-else-if="activeGroup === 'collect'" />
      <SettingsStorage v-else-if="activeGroup === 'storage'" />
      <SettingsSystem v-else-if="activeGroup === 'system'" />
    </template>
  </div>
</template>

<style scoped>
/* v35：设置页也填满窗口 */
.settings-page {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}
</style>
