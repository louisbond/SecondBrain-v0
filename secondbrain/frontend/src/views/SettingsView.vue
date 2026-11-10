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
  <div class="settings-page" :class="{ embedded }">
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

/* 嵌入管理区时不要再自己滚一次。
   v0.7.4 之前这里是 height:100% + overflow-y:auto，套在
   .manage-section（同样 overflow-y:auto）里就成了双滚动条：
   内层先滚到底、外层才动，滚轮手感像卡住。整页只留外层一个滚动容器。

   flex: 0 0 auto 是必须的：管理区是 flex column，默认 flex-shrink:1 会把
   这个包裹层压到 200 多像素高，而里面的卡片有 600 多像素 —— 内容溢出后
   直接压在下一张卡上，看起来像卡片"叠"在一起。 */
.settings-page.embedded {
  flex: 0 0 auto;
  height: auto;
  min-height: auto;
  overflow: visible;
}
</style>
