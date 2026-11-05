<script setup>
/**
 * 解析段（含配图）（v42 从 WorkbenchView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { useWorkbench } from '../../composables/useWorkbench'

const {
  current,
  loadingContent,
  retrying,
  canRetry,
  stateClass,
  bodyLines,
  bodyCharCount,
  retryCurrent,
  shots,
  shotPosition,
  shotCaption,
  shotUrl,
  shotBusy,
  onPickFile,
  insertShot,
  removeShot
} = useWorkbench()
</script>

<template>
  <div class="parse-section" :class="{ 'parse-section--empty': !current }">
    <div v-if="!current" class="mb-empty dim">
      ▤ 解析 — 选一条任务开始（点上面的任务，或去
      <a href="/status">状态页</a>）
    </div>

    <template v-else>
      <div class="mb-head">
        <span class="mb-icon">▤</span>
        <span class="mb-title">解析</span>
        <span class="badge" :class="stateClass(current.state)">
          {{ current.stage }}
        </span>
        <div class="spacer" style="flex: 1"></div>
        <span v-if="loadingContent" class="dim mb-hint">加载中…</span>
        <button
          v-if="current.state === 'fail' || canRetry"
          class="btn ghost sm"
          :disabled="retrying"
          @click="retryCurrent"
        >
          {{ retrying ? '重试中…' : '重试' }}
        </button>
      </div>

      <div class="mb-cur">
        <span class="mb-cur-title">{{ current.title }}</span>
        <span class="dim mono mb-cur-meta">
          {{ current.source_url || '速记' }} · 原文 {{ current.raw_len }} 字
        </span>
      </div>

      <div v-if="current.error_detail" class="alert warn mb-err">
        {{ current.error_detail }}
      </div>

      <div class="mb-body-label">
        <span>{{ current.asr ? '📄 语音转写' : '📄 原文' }}</span>
        <span class="badge">{{ bodyLines.length }} 行</span>
        <span class="badge">{{ bodyCharCount }} 字</span>
        <div class="spacer" style="flex: 1"></div>
        <a class="mb-goto" href="/process">按模板生成 → 加工区</a>
      </div>

      <!-- 转写正文：**带行号**（v36 用户要求） -->
      <div class="code-box">
        <div v-for="(ln, i) in bodyLines" :key="i" class="code-line">
          <span class="code-ln">{{ i + 1 }}</span>
          <span class="code-lt">{{ ln }}</span>
        </div>
      </div>

      <!-- 配图工具栏（v35 并进同一张卡；v36 保留一行） -->
      <div class="shot-bar">
        <span class="shot-label">🖼 配图 {{ shots.length }}</span>
        <label class="row shot-pos">
          第
          <input v-model.number="shotPosition" type="number" min="0" />
          段
        </label>
        <input v-model="shotCaption" type="text" placeholder="图注" class="shot-in" />
        <input v-model="shotUrl" type="text" placeholder="图片地址 / 选文件" class="shot-in" />
        <input type="file" accept="image/*" class="shot-file" @change="onPickFile" />
        <button class="btn sm" :disabled="shotBusy" @click="insertShot">
          {{ shotBusy ? '插入中…' : '插入' }}
        </button>
      </div>
      <div v-if="shots.length" class="shot-chips">
        <span
          v-for="(s, i) in shots"
          :key="i"
          class="badge"
          style="cursor: pointer"
          @click="removeShot(i)"
        >
          {{ s }} ×
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.mb-empty {
  font-size: 12.5px;
  padding: 14px 2px 6px;
}

/* 解析段的当前任务 */
.mb-cur {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.mb-cur-title {
  font-size: 14.5px;
  font-weight: 600;
}
.mb-cur-meta {
  font-size: 11.5px;
}
.mb-err {
  margin-bottom: 8px;
  font-size: 12px;
}
.mb-body-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
  margin: 10px 0 6px;
}
.mb-goto {
  font-size: 11.5px;
  font-weight: 400;
}

/* ===== 转写正文 + 行号（v36） =====
   行号固定宽右对齐，正文 flex:1 自动换行。
   长内容**卡内滚动**（max-height 38vh），不把整页推到几屏高。 */
.code-box {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-soft, rgba(255, 255, 255, 0.02));
  /* v36：**吃掉卡片剩余高度**（不是写死 max-height）。
     上面给 .wb-merge 设了 flex 列，这里 flex:1 就能自动适配窗口高度：
     高一点的屏幕看到更多行，矮一点的屏幕少几行但整页不滚。
     min-height 保证再矮也至少能看 6 行。 */
  flex: 1 1 auto;
  min-height: 120px;
  overflow: auto;
  padding: 6px 0;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12.5px;
  line-height: 1.7;
}
.code-line {
  display: flex;
  gap: 10px;
  padding: 0 10px 0 0;
}
.code-line:hover {
  background: var(--bg-hover);
}
.code-ln {
  flex: 0 0 38px;
  text-align: right;
  color: var(--text-dim);
  opacity: 0.55;
  user-select: none;
  font-variant-numeric: tabular-nums;
}
.code-lt {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text);
}
/* 空行也要占一行高度 */
.code-lt:empty::before {
  content: '\00a0';
}

/* 配图工具栏：一行放下 */
.shot-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
  flex: 0 0 auto;
}
.shot-label {
  font-size: 12px;
  color: var(--text-dim);
  flex: 0 0 auto;
}
.shot-pos {
  margin: 0;
  flex: 0 0 auto;
  font-size: 12px;
}
.shot-pos input {
  width: 46px;
}
.shot-in {
  flex: 1 1 130px;
  min-width: 0;
}
.shot-file {
  flex: 0 1 150px;
  min-width: 0;
  font-size: 11px;
}
.shot-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

/* 逐层 flex：.wb-merge(列) → 本组件(列, flex:1) → .code-box(flex:1)。
   这样转写框仍吃掉卡片剩余高度，窗口变高/变矮都自适应。 */
.parse-section {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 没选任务时不拉伸，避免一张大空卡 */
.parse-section--empty {
  flex: 0 0 auto;
}
</style>
