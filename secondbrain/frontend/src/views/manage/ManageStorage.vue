<script setup>
/**
 * 设置 → 采集与存储（v41 从 ManageView.vue 拆出）。
 *
 * 拆分前 ManageView.vue 有 952 行：6 个页签的状态、API 调用、模板全挤在
 * 一个 setup 里，改「创作模板」要在 900 行里找对应变量。
 *
 * 现在每个页签是一个自治组件：自己持有状态、自己 onMounted 拉数据。
 * 父组件只管 tab 切换 + KeepAlive 缓存。
 * 共享的只有 inject('toast')（App.vue 提供），不需要 props 穿透。
 *
 * v0.7.4 版式修正：
 *   · 「平台适配能力」原本接在两张配置卡之后，被挤到页底，翻到底才知道
 *     自己能采什么。现在放到页首 —— 先看能力，再配兜底，最后配存储。
 *   · 改成两列栅格：5 个适配器从 5 行压到 3 行，页首不再是一大片。
 *   · 本页只有 `.manage-section` 一个滚动容器；嵌入的 SettingsView 在
 *     embedded 态不再自带 overflow，否则会出现内外两个滚动条。
 */
import { ref, onMounted } from 'vue'
import { settingsApi } from '../../api/client'
import SettingsView from '../SettingsView.vue'

// 平台适配器能力（从设置区搬来的只读展示）
const adapters = ref([])
const supportLevels = {
  full: { label: '完整可用', cls: 'ok' },
  partial: { label: '部分可用', cls: 'warn' },
  unstable: { label: '不稳定', cls: 'danger' },
  manual: { label: '仅手动兜底', cls: '' }
}

/** 平台适配器能力（只读清单，来自 /api/settings 的 adapters 字段）。 */
async function loadAdapters() {
  try {
    const data = await settingsApi.get()
    adapters.value = data.adapters || []
  } catch {
    adapters.value = []
  }
}

onMounted(loadAdapters)
</script>

<template>
  <div class="manage-stack manage-section">
    <div v-if="adapters.length" class="card">
      <div class="card-title">
        <span>📡</span><span class="title-text">平台适配能力</span>
        <span class="title-note">本地直采 · 不外发链接</span>
      </div>
      <div class="adapter-grid">
        <div class="adapter-cell" v-for="a in adapters" :key="a.name">
          <span class="level-dot" :class="supportLevels[a.support_level]?.cls"></span>
          <div class="grow">
            <div class="adapter-name">{{ a.label }}</div>
            <div class="adapter-meta">
              <span :class="{ on: a.supports_subtitle }"
                >字幕 {{ a.supports_subtitle ? '✓' : '—' }}</span
              >
              <span :class="{ on: a.supports_audio }">音频 {{ a.supports_audio ? '✓' : '—' }}</span>
            </div>
          </div>
          <span class="badge" :class="supportLevels[a.support_level]?.cls">
            {{ supportLevels[a.support_level]?.label || a.support_level }}
          </span>
        </div>
      </div>
      <div class="adapter-hint">
        诚实标注：短视频平台对未登录请求只返回标题和摘要，这是平台限制而非 bug；
        要完整文案可在下面开启第三方解析，或在后续版本用登录态 Cookie。
      </div>
    </div>

    <!-- v0.7.3：采集解析配置（第三方 API 兜底）并入本页，页签名就叫「采集与存储」 -->
    <SettingsView embedded group="collect" />
    <SettingsView embedded group="storage" />
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.manage-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 滚动容器里的子项一律不压缩：否则高度按容器均值分配，内容越高的卡片
   被压得越狠，剩下的内容就叠到下一张卡上 */
.manage-stack > * {
  flex: 0 0 auto;
}

.title-text {
  white-space: nowrap;
}

.title-note {
  margin-left: auto;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-dim, #6b7387);
  white-space: nowrap;
}

.adapter-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 8px;
}

.adapter-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  min-width: 0;
}

/* .grow 只在 .list-item 下有 flex:1 ——这里必须自己补，否则名字那一列不收缩，
   会把卡片顶出横向滚动条（min-width:0 才能让 text-overflow 生效） */
.adapter-cell .grow {
  flex: 1 1 auto;
  min-width: 0;
}

.level-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: var(--text-dim, #6b7387);
}

.level-dot.ok {
  background: var(--ok, #34d399);
}

.level-dot.warn {
  background: var(--warn, #fbbf24);
}

.level-dot.danger {
  background: var(--danger, #f87171);
}

.adapter-name {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.adapter-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--text-dim, #6b7387);
}

.adapter-meta .on {
  color: var(--text-muted, #9aa2b4);
}

.adapter-hint {
  margin-top: 8px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-dim, #6b7387);
}

@media (max-width: 720px) {
  .adapter-grid {
    grid-template-columns: 1fr;
  }

  /* 窄窗口下标题行先让位，别把卡片顶出横向滚动条 */
  .title-note {
    display: none;
  }
}
</style>
