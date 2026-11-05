<script setup>
/**
 * 核准卡片（v41 从 ProcessView.vue 拆出）。
 *
 * 状态与动作来自 `composables/useReview.js` 的注入上下文，
 * 本组件只负责渲染与绑定，不自己发请求、不自己持有表单状态。
 */
import { useReview } from '../../composables/useReview'
import ParseSection from './ParseSection.vue'
import TaxonomySection from './TaxonomySection.vue'

const {
  form,
  saving,
  reanalyzing,
  isRawReady,
  currentStageHint,
  aiModelLabel,
  summaryTypeLabel,
  reanalyze,
  skip,
  confirm
} = useReview()
</script>

<template>
  <div class="card">
    <!-- v32：头部重排（用户：右上角的状态显示逻辑不清晰）。
                 原来一行里挤着：卡片标题 + 步骤说明 + 「要点式」badge
                 + 「OPENAI_COMPAT」badge + 两个按钮 —— 技术枚举和操作
                 混在一起，用户看不出哪个是"状态"、哪个是"能点的"。
                 现在分三层：
                   ① 标题行：只放标题 + 状态徽标 + 动作按钮
                   ② 步骤行：流程提示（灰字，不抢视觉）
                   ③ 来源行：AI 来源与摘要样式（可读中文 + tooltip） -->
    <div class="card-title">
      <span>🤖</span> 核准
      <span class="badge" :class="currentStageHint.cls">
        {{ currentStageHint.text }}
      </span>
      <div class="spacer" style="flex: 1"></div>
      <button
        class="btn ghost sm"
        :disabled="reanalyzing"
        :title="
          isRawReady
            ? '跑一次 AI 提取要点（生成分区/标签建议）'
            : '摘要或分区标签不满意时，重新让 AI 分析一次'
        "
        @click="reanalyze"
      >
        {{
          reanalyzing
            ? isRawReady
              ? '分析中…'
              : '重新分析中…'
            : isRawReady
              ? 'AI 提取要点'
              : '重新分析'
        }}
      </button>
      <button class="btn ghost sm" @click="skip">跳过本条</button>
    </div>

    <div class="step-hint">① 解析 → ② 加手稿 → ③ 确认分区与标签 → 入库</div>

    <div v-if="aiModelLabel" class="source-hint">
      AI 来源：{{ aiModelLabel }}
      <span class="sep">·</span>
      摘要样式：{{ summaryTypeLabel }}
    </div>

    <input
      v-model="form.title"
      type="text"
      style="margin: 8px 0; font-size: 15px; font-weight: 600"
      placeholder="标题（默认取 AI 建议）"
    />

    <div v-if="form.ai_summary" class="ai-summary" style="margin-bottom: 10px">
      {{ form.ai_summary }}
    </div>
    <div v-else class="dim" style="margin-bottom: 10px">
      （本次没有 AI 摘要——可在下方用模板重新生成，或直接手写。）
    </div>

    <ParseSection />

    <!-- ② 个人手稿（对解析不满意时写你的理解） -->
    <div class="section">
      <label>② 个人手稿（这段是你的，图谱里的主节点）</label>
      <textarea
        v-model="form.personal_summary"
        rows="3"
        placeholder="写下你的理解、疑问、行动点"
      ></textarea>
    </div>

    <TaxonomySection />

    <!-- ⑤ 确认入库：分区与标签都定了才入库 -->
    <div class="row" style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line)">
      <span class="dim" style="font-size: 11.5px">
        分区与标签确认后入库，手稿会成为图谱主节点
      </span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn primary" :disabled="saving" @click="confirm">
        {{ saving ? '入库中…' : '确认入库' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 步骤提示：灰字细排，不抢标题的视觉重量 */
.step-hint {
  font-size: 11.5px;
  color: var(--text-dim);
  margin: -2px 0 6px;
}
/* AI 来源：比步骤提示更弱，用户想查的时候能找到就行 */
.source-hint {
  font-size: 11px;
  color: var(--text-dim);
  opacity: 0.75;
  margin-bottom: 10px;
}
.source-hint .sep {
  margin: 0 5px;
  opacity: 0.6;
}
.badge.accent {
  color: var(--accent);
  border-color: var(--accent-border);
}
.badge.warn {
  color: var(--warn, #e5a23d);
  border-color: var(--warn, #e5a23d);
}

.section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
</style>
