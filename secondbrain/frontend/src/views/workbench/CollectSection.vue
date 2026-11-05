<script setup>
/**
 * 采集段（v42 从 WorkbenchView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { useWorkbench } from '../../composables/useWorkbench'

const {
  rawText,
  parsed,
  parsing,
  submitting,
  quickNoteText,
  showQuickNote,
  items,
  dupCount,
  platformOf,
  parseAndSubmit,
  doQuickNote,
  removeDuplicates,
  removeItem
} = useWorkbench()
</script>

<template>
  <div class="collect-section">
    <div class="mb-head">
      <span class="mb-icon">⇪</span>
      <span class="mb-title">采集</span>
      <span class="dim mb-hint">粘贴即入库</span>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" @click="showQuickNote = !showQuickNote">速记</button>
    </div>

    <div v-if="showQuickNote" class="mb-quicknote">
      <textarea
        v-model="quickNoteText"
        rows="2"
        placeholder="随手记一段想法，直接入队列"
      ></textarea>
      <button class="btn sm" @click="doQuickNote">加入队列</button>
    </div>

    <textarea
      v-model="rawText"
      rows="2"
      placeholder="把分享文案整段粘进来（链接 / 标题 / 简介一起），自动识别平台"
    ></textarea>

    <div class="row mb-actions">
      <button
        class="btn primary"
        :disabled="parsing || submitting || !rawText.trim()"
        @click="parseAndSubmit"
      >
        {{ parsing ? '解析中…' : submitting ? '入库中…' : '解析并入库' }}
      </button>
      <span v-if="parsed" class="dim mb-count">
        识别到 {{ parsed.count }} 条{{ submitting ? '，正在入库…' : '' }}
      </span>
      <div class="spacer" style="flex: 1"></div>
      <button v-if="dupCount" class="btn ghost sm" @click="removeDuplicates">
        去掉重复 {{ dupCount }}
      </button>
    </div>

    <div v-if="parsed && items.length" class="mb-parsed">
      <div v-for="(it, i) in items" :key="i" class="list-item mb-parsed-row">
        <span class="platform-dot" :style="{ background: platformOf(it.platform).color }"></span>
        <div class="grow">
          <div class="title">{{ it.title || it.url }}</div>
          <div class="sub">{{ it.platform }} · {{ it.url }}</div>
        </div>
        <span v-if="it.existing" class="badge warn">已存在</span>
        <button class="btn ghost sm" @click.stop="removeItem(i)">×</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  min-height: 24px;
}
.mb-icon {
  font-size: 13px;
  color: var(--text-dim);
}
.mb-title {
  font-size: 13.5px;
  font-weight: 600;
}
.mb-hint {
  font-size: 11.5px;
  font-weight: 400;
}
.mb-actions {
  margin-top: 8px;
  align-items: center;
}
.mb-count {
  font-size: 11.5px;
  margin-left: 2px;
}
.mb-quicknote {
  margin-bottom: 8px;
}
.mb-quicknote .btn {
  margin-top: 6px;
}

/* 采集预览列表：限高内滚，避免把解析段挤下去 */
.mb-parsed {
  margin-top: 8px;
  max-height: 140px;
  overflow-y: auto;
}
.mb-parsed-row {
  padding: 4px 2px;
}

/* 原来由主壳的 `.wb-merge textarea` 提供。拆成子组件后 scoped 选择器
   跨不过组件边界（只有子组件**根元素**带父 scope id），所以移到这里。 */
textarea {
  min-height: 46px;
}
.collect-section {
  flex: 0 0 auto;
}
</style>
