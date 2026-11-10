<script setup>
/**
 * 提示词模板（合并页，v0.10.4）。
 *
 * 原来散在两个地方：设置 → AI 里的「提取提示词」+ 设置 → 解析模板 / 创作模板。
 * 按用户要求合并成一页，分四个区：
 *   采集      → 采集模板（prompts/collect.txt）
 *   解析      → 解析模板（parse_templates，模板化生成的结构）
 *   个人创作   → 创作模板（note_templates，原创区空模板）
 *   面向接口   → 发给模型的原始 prompt（prompts/extract.txt）
 */
import { ref } from 'vue'
import ManageParseTemplates from './ManageParseTemplates.vue'
import ManageNoteTemplates from './ManageNoteTemplates.vue'
import PromptEditor from './PromptEditor.vue'

const sub = ref('collect')
const subs = [
  { key: 'collect', label: '采集' },
  { key: 'parse', label: '解析' },
  { key: 'create', label: '个人创作' },
  { key: 'api', label: '面向接口（AI提示词）' }
]
</script>

<template>
  <div class="prompts-page">
    <div class="sub-tabs">
      <button
        v-for="s in subs"
        :key="s.key"
        class="sub-tab"
        :class="{ active: sub === s.key }"
        @click="sub = s.key"
      >
        {{ s.label }}
      </button>
    </div>

    <!-- 采集 -->
    <PromptEditor v-if="sub === 'collect'" scope="collect" title="采集模板" />

    <!-- 解析（模板化生成） -->
    <ManageParseTemplates v-else-if="sub === 'parse'" />

    <!-- 个人创作（原创区空模板） -->
    <ManageNoteTemplates v-else-if="sub === 'create'" />

    <!-- 面向接口：发给模型的原始 prompt -->
    <PromptEditor v-else scope="extract" title="面向接口（AI 提示词）" />
  </div>
</template>

<style scoped>
.prompts-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.sub-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.sub-tab {
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid var(--line, #33353d);
  background: transparent;
  color: var(--text-dim, #9a9daa);
  font-size: 12.5px;
  cursor: pointer;
}

.sub-tab.active {
  border-color: var(--ai, #7c6cf0);
  color: var(--text, #e6e7ea);
  background: color-mix(in srgb, var(--ai, #7c6cf0) 18%, transparent);
}
</style>
