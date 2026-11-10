<script setup>
/**
 * 加工区的「原文」展示。
 *
 * 用户诉求（2026-11-09）："未加工的文件最好还有个办法看到原文，就能尽量避免错误发生"。
 *
 * 以前加工区只有 AI 摘要框，用户看不到抓到了什么 ——
 * 抖音只抓到一句文案、ASR 没转出字幕这类问题，
 * 界面上完全看不出来（条目还显示"已采集""原文就绪"）。
 * 现在把原文摆出来，并**按长度给出健康度提示**：
 * 抓到的不可能是正文 → 立刻标红提醒，不用等到 AI 总结失败才发现。
 */
import { computed, ref } from 'vue'
import { useReview } from '../../composables/useReview'

const { current } = useReview()

const expanded = ref(true)

// 与后端 pipeline 的 `_TRANSCRIPT_MIN_CHARS` 一致：
// 短于这个字数基本只是标题/简介，不是真正文
const MIN_REAL_CHARS = 200

const rawText = computed(() => String(current.value?.item?.raw_text || ''))
const rawLen = computed(() => rawText.value.trim().length)

/** 健康度：够长=可能抓到了正文；太短=八成只抓到一句文案。 */
const health = computed(() => {
  const n = rawLen.value
  if (!n) {
    return { cls: 'danger', text: '没有原文', hint: '这条素材没有任何正文，可能是抓取失败' }
  }
  if (n < MIN_REAL_CHARS) {
    return {
      cls: 'warn',
      text: `只有 ${n} 字`,
      hint: '原文过短，多半只抓到标题/简介，没有真正的正文'
    }
  }
  return { cls: 'ok', text: `${n} 字`, hint: '' }
})

const lines = computed(() => rawText.value.split('\n'))

/** 采集来源：ASR 转写 / 网页正文 / 手动粘贴。 */
const sourceLabel = computed(() => {
  const it = current.value?.item || {}
  if (it.asr) return '语音转写'
  const ex = String(it.extractor || '')
  if (ex.includes('asr')) return '语音转写'
  if (ex.includes('manual')) return '手动粘贴'
  if (ex.includes('ssr') || ex.includes('og')) return '页面抓取'
  return ex || '原文'
})
</script>

<template>
  <div class="section source-sec">
    <div class="source-head">
      <label class="source-label">原文</label>
      <span class="badge" :class="health.cls">{{ health.text }}</span>
      <span class="dim source-kind">{{ sourceLabel }}</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" @click="expanded = !expanded">
        {{ expanded ? '收起' : '展开' }}
      </button>
    </div>

    <div v-if="health.hint" class="source-warn" :class="health.cls">⚠ {{ health.hint }}</div>

    <div v-if="expanded" class="code-box">
      <div v-if="!rawLen" class="dim" style="padding: 6px 10px">（没有原文内容）</div>
      <div v-for="(ln, i) in lines" v-else :key="i" class="code-line">
        <span class="code-ln">{{ i + 1 }}</span>
        <span class="code-lt">{{ ln }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.source-sec {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.source-label {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 550;
}

.source-kind {
  font-size: 11.5px;
}

/* 健康度提示：太短/为空时给出醒目警示 */
.source-warn {
  font-size: 11.5px;
  padding: 6px 10px;
  margin-bottom: 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  color: var(--text-dim);
}
.source-warn.warn {
  color: var(--warn, #fbbf24);
  border-color: var(--warn, #fbbf24);
  background: rgba(251, 191, 36, 0.08);
}
.source-warn.bad,
.source-warn.danger {
  color: var(--danger, #f87171);
  border-color: var(--danger, #f87171);
  background: rgba(248, 113, 113, 0.08);
}

/* 原文正文：带行号，卡内滚动（与采集区解析正文同款） */
.code-box {
  border: 1px solid var(--line, #33353d);
  border-radius: 8px;
  background: var(--bg-soft, rgba(255, 255, 255, 0.02));
  max-height: 32vh;
  min-height: 90px;
  overflow: auto;
  padding: 6px 0;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  line-height: 1.7;
}

.code-line {
  display: flex;
  gap: 10px;
  padding: 0 10px;
  white-space: pre-wrap;
  word-break: break-word;
}

.code-ln {
  flex: none;
  width: 30px;
  text-align: right;
  color: var(--text-dim);
  opacity: 0.6;
  user-select: none;
}

.code-lt {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--text);
}
</style>
