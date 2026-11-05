<script setup>
/**
 * 设置（v37 管理区 + 设置合并；v41 拆成页签子组件）。
 *
 * 拆分前 952 行：6 个页签的状态、接口调用、模板全挤在一个 setup 里，
 * 而且 `<style>` 里整段样式被重复贴了两遍。
 *
 * 现在这里只做三件事：画页签、切页签、缓存页签。
 * 每个页签是 `views/manage/` 下的自治组件：自己持有状态、自己拉数据。
 *
 * 为什么用 KeepAlive：
 *   · 原来父组件一次性拉 7 个接口，切 tab 不重新加载 → 编辑草稿不丢。
 *     KeepAlive 复刻了这个行为，还让首屏只拉当前 tab 的数据。
 *   · 不加 onActivated 刷新：刷新会清掉正在编辑的模板草稿。
 *     settings 的 watch(refreshSignal) 会在外部刷新信号到来时更新。
 */
import { computed, ref } from 'vue'
import ManageVocab from './manage/ManageVocab.vue'
import ManageParseTemplates from './manage/ManageParseTemplates.vue'
import ManageNoteTemplates from './manage/ManageNoteTemplates.vue'
import ManageAi from './manage/ManageAi.vue'
import ManageStorage from './manage/ManageStorage.vue'
import ManageMaintenance from './manage/ManageMaintenance.vue'

const tabs = [
  { key: 'vocab', label: '分区与标签' },
  { key: 'templates', label: '解析模板' },
  { key: 'note-templates', label: '创作模板' },
  { key: 'ai', label: 'AI 与密钥' },
  { key: 'storage', label: '采集与存储' },
  { key: 'ops', label: '维护与系统' }
]

const panels = {
  vocab: ManageVocab,
  templates: ManageParseTemplates,
  'note-templates': ManageNoteTemplates,
  ai: ManageAi,
  storage: ManageStorage,
  ops: ManageMaintenance
}

const section = ref('vocab')
const activePanel = computed(() => panels[section.value])
</script>

<template>
  <div class="manage-page">
    <div class="row" style="margin-bottom: 14px; flex: 0 0 auto">
      <div class="tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="tab"
          :class="{ active: section === t.key }"
          @click="section = t.key"
        >
          {{ t.label }}
        </button>
      </div>
      <div class="spacer" style="flex: 1"></div>
      <span class="dim" style="font-size: 11.5px">管理与设置已合并 · 同一页切换</span>
    </div>

    <div class="manage-body">
      <KeepAlive>
        <component :is="activePanel" />
      </KeepAlive>
    </div>
  </div>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.manage-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 页签内容区吃掉剩余高度；子组件根节点 .manage-section 自带 flex:1 + 内部滚动 */
.manage-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
