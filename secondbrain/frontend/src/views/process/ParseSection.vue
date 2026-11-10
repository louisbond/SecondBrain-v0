<script setup>
/**
 * ① 模板化解析（v41 从 ProcessView.vue 拆出）。
 *
 * 状态与动作来自 `composables/useReview.js` 的注入上下文，
 * 本组件只负责渲染与绑定，不自己发请求、不自己持有表单状态。
 */
import { computed } from 'vue'
import { useReview } from '../../composables/useReview'
import ModelPicker from '../../components/ModelPicker.vue'

const {
  form,
  templateId,
  templates,
  minChars,
  charPresets,
  parsingBusy,
  parseResult,
  parseText,
  runParse
} = useReview()

// 解析框和 form.ai_summary 必须是**同一份数据**：
// 之前只写 parseText，入库时 confirm() 送的是 form.ai_summary（旧值），
// 于是「生成 2000 字 → 入库后笔记还是旧摘要」，用户看到的就是"不同步"。
const contentText = computed({
  get: () => parseText.value,
  set: (v) => {
    parseText.value = v
    form.value.ai_summary = v
  }
})
</script>

<template>
  <!-- ① 解析：模板 + 目标字数 + 生成键（同一行） -->
  <div class="section">
    <label>① AI 总结（选模板与字数档位，让 AI 生成）</label>
    <div class="row wrap parse-row">
      <!-- 创作模型**位居最左**（用户 2026-11-09，与采集区的提取模型一致） -->
      <ModelPicker kind="create" label="创作模型" />
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
          parsingBusy ? '生成中…' : parseResult && !parseResult.ok ? '重新生成' : 'AI 总结'
        }}
      </button>
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
      <span v-if="parseResult.adjusted">（素材较短，目标已下调为 {{ parseResult.min_chars }} 字）</span>
    </div>
    <textarea
      v-if="parseText"
      v-model="contentText"
      rows="8"
      placeholder="解析结果（可直接编辑，改完入库保存的就是这一版）"
      style="margin-top: 8px"
    ></textarea>
  </div>
</template>

<style scoped>
/* 解析行：模型 + 模板 + 字数 + 生成键同一行 */
.parse-row {
  gap: 8px;
  align-items: center;
  /* 模型控件在最左且不换行；后面的下拉/按钮空间不够时才整体折行。 */
  flex-wrap: wrap;
}
/* 下拉与按钮同高（全局 select padding 偏大，这里压平） */
.parse-row select {
  height: 32px;
  padding: 4px 9px;
  font-size: 13px;
}
/* 生成键与下拉同高 32px，三者基线齐平 */
.parse-row .btn {
  height: 32px;
}

.section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
</style>
