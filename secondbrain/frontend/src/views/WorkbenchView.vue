<script setup>
/**
 * 采集区（v42 拆成主壳 + 两个子组件 + 注入上下文）。
 *
 * 这里只画「状态条 + 一张合并卡」，卡片里两段由子组件填。
 * 注意 .mb-split 留在主壳：它用负 margin 顶到卡片边缘，
 * 是两段之间的分隔，不属于任何一边。
 */
import { provideWorkbench } from '../composables/useWorkbench'
import StatusStrip from '../components/StatusStrip.vue'
import CollectSection from './workbench/CollectSection.vue'
import ParseSection from './workbench/ParseSection.vue'

const { states, stats, current, pickTask } = provideWorkbench()
</script>

<template>
  <div class="wb-page">
    <div class="wb-col">
      <StatusStrip
        :items="states"
        :stats="stats"
        :per-group="8"
        class="strip-top"
        @select="pickTask"
      />

      <!-- 采集与解析**同一张卡**（v36）：一条外边框 + 中间虚线分隔 -->
      <div class="wb-scroll">
        <div class="card wb-merge" :class="{ 'wb-merge--empty': !current }">
          <CollectSection />
          <div class="mb-split"></div>
          <ParseSection />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wb-page {
  height: 100%;
  min-height: 0;
}
.wb-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}

/* 状态条置顶后要更显眼（用户：面积调大一点） */
.strip-top {
  flex: 0 0 auto;
  padding-bottom: 4px;
}

/* 合并卡所在的可滚区域 */
.wb-scroll {
  flex: 1;
  min-height: 120px;
  /* v36：改成 flex 列 + 卡片吃满剩余高度 —— 这样「状态条 + 合并卡」
     正好一屏，不用滚一下才看到配图行（用户：合并后不要太占空间）。
     用 flex 而不是写死 max-height：窗口高度变了也不会有残留滚动条。 */
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding-right: 6px;
}

/* ===== 合并卡（v36）：采集段 + 解析段共用一张卡 =====
   省掉第二张卡的边框 + 内边距 + 标题行，约省 80~100px 竖向空间。 */
.wb-merge {
  padding: 10px 14px 12px;
  margin-bottom: 0;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
/* 没选任务：卡片只占内容高，不撑满屏幕（否则一张大空卡） */
.wb-merge--empty {
  flex: 0 0 auto;
  overflow: visible;
}

/* 采集段与解析段之间的分隔线（不新增卡片） */
.mb-split {
  margin: 12px -14px 10px;
  border-top: 1px dashed var(--line);
}
</style>
