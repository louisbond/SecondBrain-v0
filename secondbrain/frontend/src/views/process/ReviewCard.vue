<script setup>
/**
 * 核准卡片（v41 从 ProcessView.vue 拆出）。
 *
 * 状态与动作来自 `composables/useReview.js` 的注入上下文，
 * 本组件只负责渲染与绑定，不自己发请求、不自己持有表单状态。
 */
import { inject, ref } from 'vue'
import { useReview } from '../../composables/useReview'
import ParseSection from './ParseSection.vue'
import SourceText from './SourceText.vue'
import TaxonomySection from './TaxonomySection.vue'

const toast = inject('toast')

const {
  form,
  current,
  selectItem,
  saving,
  reanalyzing,
  isRawReady,
  currentStageHint,
  aiModelLabel,
  summaryTypeLabel,
  reanalyze,
  skip,
  confirm,
  saveTitleNow,
  removeCurrent
} = useReview()

// -- 标题：默认只读，点一下才变输入框，改完必须点「确定」-------------------
const editingTitle = ref(false)
const titleDraft = ref('')

function startTitleEdit() {
  titleDraft.value = form.title || ''
  editingTitle.value = true
}

function cancelTitleEdit() {
  editingTitle.value = false
}

async function confirmTitle() {
  const t = titleDraft.value.trim()
  if (!t) return cancelTitleEdit()
  form.title = t
  await saveTitleNow() // 立即写库 + 重命名 A 层 md
  editingTitle.value = false
}
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
      <button
        class="btn ghost sm"
        style="color: var(--danger, #e5484d)"
        title="删除这条素材（含原文文件），不可恢复"
        @click="removeCurrent"
      >
        🗑 删除
      </button>
    </div>

    <div v-if="aiModelLabel" class="source-hint">
      AI 来源：{{ aiModelLabel }}
      <span class="sep">·</span>
      摘要样式：{{ summaryTypeLabel }}
    </div>

    <div class="title-row">
      <template v-if="!editingTitle">
        <span
          class="title-text"
          title="点击改标题，改完会同步重命名 A 层文件"
          @click="startTitleEdit"
          >{{ form.title || '未命名' }}</span
        >
      </template>
      <template v-else>
        <input
          v-model="titleDraft"
          class="title-input"
          @keyup.enter="confirmTitle"
          @keyup.esc="cancelTitleEdit"
        />
        <button class="btn sm" @click="confirmTitle">确定</button>
        <button class="btn ghost sm" @click="cancelTitleEdit">取消</button>
      </template>
    </div>

    <!-- 原文：放在 AI 总结**之前** —— 先确认抓到了什么，再决定要不要生成摘要。
         太短的原文会直接标黄/标红，避免"素材是空的"这种问题拖到 AI 失败才暴露。 -->
    <SourceText />

    <!-- 摘要不在这里重复整段渲染：① 解析框里就是同一份可编辑内容，
         这里只报「有没有、多长」，避免两处各显示一份、改了一处另一处不动 -->
    <div class="ai-summary-line">
      <template v-if="form.ai_summary">
        <span class="badge ok">AI 摘要 {{ form.ai_summary.length }} 字</span>
      </template>
      <span v-else class="dim">（无 AI 摘要）</span>
    </div>

    <!-- 素材工具：把「听原声核对 / 补传视频 / 导出正文」放在正文之前，
         因为这三件事决定下面看到的内容是否可信 -->
    <ParseSection />

    <!-- 入库入口放在解析结果**正下方**（用户要求）：
         解析完就能直接入库，不用先翻到页面最底；
         手稿、分区、标签没定也可以先入库，之后再补。 -->
    <div class="row" style="margin: 10px 0 4px">
      <div class="spacer" style="flex: 1"></div>
      <button class="btn primary" :disabled="saving" @click="confirm">
        {{ saving ? '入库中…' : '✓ 总结入库' }}
      </button>
    </div>

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

    <!-- ⑤ 确认入库（底部还有一个入口：解析结果下方也能点，两处等价） -->
    <div class="row" style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line)">
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost" :disabled="saving" @click="confirm">
        {{ saving ? '入库中…' : '✓ 总结入库' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.ai-summary-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-size: 12px;
}

/* 素材工具条：三颗小按钮，不抢正文视觉 */
/* 标题：默认只读文本，点一下才变输入框（避免一直杵着个大框很突兀） */
.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
  min-height: 30px;
}

.title-text {
  font-size: 15px;
  font-weight: 600;
  cursor: text;
  padding: 3px 6px;
  border-radius: 6px;
  border: 1px dashed transparent;
}

.title-text:hover {
  border-color: var(--line, #33353d);
  background: var(--bg-elevated, #22242b);
}

.title-input {
  flex: 1;
  min-width: 260px;
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid var(--ai, #7c6cf0);
  background: var(--bg-input, #1b1c22);
  color: var(--text, #e6e7ea);
  font-size: 15px;
  font-weight: 600;
}
/* AI 来源：弱化显示，用户想查的时候能找到就行 */
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
