<script setup>
/**
 * 加工区 · 核准页（v41 拆成主壳 + 子组件）。
 *
 * 这里只做三件事：画加工队列条、选条目、决定空态还是核准卡片。
 * 全部表单状态与业务逻辑在 `composables/useReview.js`，
 * 核准界面在 `views/process/` 下三个组件。
 */
import StatusStrip from '../components/StatusStrip.vue'
import ReviewCard from './process/ReviewCard.vue'
import { provideReview } from '../composables/useReview'

const { queueGroups, current, pickQueue } = provideReview()
</script>

<template>
  <div>
    <div class="proc-col">
      <!--
        加工队列（v37）：复用采集区同款 StatusStrip，2 行紧凑形态（v38）。
        同一个组件 → 同一套 padding / 字号 / 圆角，切页面不跳。
      -->
      <StatusStrip
        :groups="queueGroups"
        title="加工队列"
        :show-all="false"
        :per-group="8"
        dense
        class="strip-top"
        @select="pickQueue"
      />

      <!-- 核准区：内容自己滚，页面框架不滚 -->
      <div class="scroll-col">
        <div v-if="!current" class="card empty">
          <div class="icon">▤</div>
          <div>左边选一条素材开始<strong>核准</strong></div>
          <div class="dim" style="margin-top: 6px; font-size: 12px; line-height: 1.7">
            核准：确认<strong>分区</strong>与标签 →
            选模板做<strong>模板化解析</strong>（字数档位可调）→
            生成结果可直接当创作底稿（字数为可调档位）
          </div>
        </div>
        <ReviewCard v-else />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 加工区：纵向排（待加工工作区在上、核准区在下） */
.proc-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}
/* 队列条不参与拉伸（下面核准区吃剩余高度） */
.strip-top {
  flex: 0 0 auto;
  padding-bottom: 4px;
}

/* 内容区自己滚：页面框架（顶栏/导航）不动，右侧长内容独立滚动 */
.scroll-col {
  max-height: calc(100vh - 300px);
  min-height: 320px;
  overflow-y: auto;
  padding-right: 6px;
}
</style>
