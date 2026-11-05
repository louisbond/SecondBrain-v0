<script setup>
/**
 * ① 模板化解析（v41 从 ProcessView.vue 拆出）。
 *
 * 状态与动作来自 `composables/useReview.js` 的注入上下文，
 * 本组件只负责渲染与绑定，不自己发请求、不自己持有表单状态。
 */
import { useReview } from '../../composables/useReview'

const {
  templateId,
  templates,
  minChars,
  charPresets,
  parsingBusy,
  parseResult,
  parseText,
  runParse,
  sendToOriginal
} = useReview()
</script>

<template>
  <!-- ① 解析：模板 + 目标字数 + 生成键（同一行） -->
  <div class="section">
    <label>① 解析（选模板与字数档位，让 AI 生成）</label>
    <div class="row wrap parse-row">
      <select v-model="templateId" style="width: auto; min-width: 230px">
        <option :value="null">AI 自动匹配模板</option>
        <option v-for="t in templates" :key="t.id" :value="t.id">
          {{ t.name }}
        </option>
      </select>
      <select v-model.number="minChars" style="width: auto; min-width: 96px">
        <option v-for="c in charPresets" :key="c" :value="c">{{ c }} 字</option>
      </select>
      <button class="btn primary" :disabled="parsingBusy" @click="runParse">
        {{
          parsingBusy ? '生成中…' : parseResult && !parseResult.ok ? '重新生成' : 'AI 生成解析结果'
        }}
      </button>
      <button v-if="parseText" class="btn ghost" @click="sendToOriginal">送去原创区 →</button>
    </div>
    <div class="dim" style="font-size: 11.5px; margin-top: 4px">
      低于目标字数会自动重新生成（最多 3 次）
    </div>
    <div v-if="parsingBusy" class="dim" style="margin-top: 6px; font-size: 11.5px">
      正在生成（长内容需要 1–3 分钟）
    </div>
    <div v-if="parseResult && !parseResult.ok" class="alert warn" style="margin-top: 8px">
      {{ parseResult.error }}
    </div>
    <div v-if="parseResult && parseResult.ok" class="alert info" style="margin-top: 8px">
      已按模板「{{ parseResult.template?.name }}」生成 {{ parseResult.chars }} 字
      <span v-if="parseResult.matched_by === 'ai'">（AI 自动匹配）</span>
    </div>
    <textarea
      v-if="parseText"
      v-model="parseText"
      rows="8"
      placeholder="解析结果（可直接编辑，满意就送去原创区）"
      style="margin-top: 8px"
    ></textarea>
  </div>
</template>

<style scoped>
/* 解析行：模板 + 字数 + 生成键同一行 */
.parse-row {
  gap: 8px;
  align-items: center;
}
/* 下拉与按钮同高（全局 select padding 偏大，这里压平） */
.parse-row select {
  height: 32px;
  padding: 4px 9px;
  font-size: 13px;
}

.section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
</style>
