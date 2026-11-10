<script setup>
/**
 * 音频播放器 + 带时间码的转写分段。
 *
 * ## 为什么需要它
 *
 * 文本是降维产物：语气、停顿、重音、情绪全部丢失。用户要核对
 * 「这句话是不是这个意思」「他当时是不是在讽刺」，只能听原声。
 *
 * 但光有音频没用 —— 10 分钟的音频，想找第 7 分钟那句话得手动拖
 * 进度条反复试。所以每段台词前面必须有时间戳，点一下跳到那一秒。
 *
 * ## 数据来源
 *
 * - 音频：`GET /api/items/{id}/audio`（FileResponse，支持 Range，
 *   所以能边下边播、能拖进度条）
 * - 分段：`GET /api/items/{id}/segments`（带 start/end，ASR 时落盘）
 *
 * 两个接口都走 cookie 鉴权（`<audio src>` 带不了 header），
 * 所以局域网模式下要先有 sb_token cookie。
 */
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'

// 同源部署，直接用相对路径。token 走 sb_token cookie（<audio src> 带不了 header）。

const props = defineProps({
  itemId: { type: [Number, String], required: true },
  hasAudio: { type: Boolean, default: false },
  durationMs: { type: Number, default: 0 }
})

const audioEl = ref(null)
const segments = ref([])
const loading = ref(false)
const error = ref('')
const currentTime = ref(0)
const playing = ref(false)
const activeIndex = ref(-1)

const audioUrl = computed(() => `/api/items/${props.itemId}/audio`)

onMounted(load)
onUnmounted(() => {
  // 离开页面时停掉，避免两个播放器同时响
  if (audioEl.value) audioEl.value.pause()
})

watch(
  () => props.itemId,
  () => load()
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const resp = await fetch(`/api/items/${props.itemId}/segments`, {
      credentials: 'same-origin'
    })
    if (!resp.ok) {
      if (resp.status === 404) {
        segments.value = []
        return
      }
      throw new Error(`HTTP ${resp.status}`)
    }
    const data = await resp.json()
    segments.value = data.segments || []
  } catch (e) {
    error.value = '读取分段失败：' + (e.message || e)
    segments.value = []
  } finally {
    loading.value = false
  }
}

/** 秒 → mm:ss（超过 1 小时补 hh:）。 */
function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

function seek(seg, index) {
  const el = audioEl.value
  if (!el) return
  el.currentTime = Math.max(0, Number(seg.start) || 0)
  activeIndex.value = index
  if (el.paused) {
    el.play().catch(() => {
      /* 用户没交互过时浏览器会拦自动播放，忽略即可 */
    })
  }
}

/**
 * 跟随播放位置高亮当前段。
 *
 * 用二分查找而不是每帧遍历：一条 10 分钟视频有几百段，
 * timeupdate 每秒触发 4~10 次，线性扫会有明显卡顿。
 */
function onTimeUpdate() {
  const el = audioEl.value
  if (!el) return
  currentTime.value = el.currentTime
  const list = segments.value
  if (!list.length) return
  let lo = 0
  let hi = list.length - 1
  let found = -1
  const t = el.currentTime
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const seg = list[mid]
    if (t < Number(seg.start)) hi = mid - 1
    else if (t >= Number(seg.end)) lo = mid + 1
    else {
      found = mid
      break
    }
  }
  activeIndex.value = found
}

/**
 * 导出：SRT 走浏览器下载。
 *
 * 为什么前端直接拼链接而不是走后端返回 blob：SRT 是十几 KB 的文本，
 * 让浏览器自己发起下载最简单，也能享受 cookie 鉴权。
 */
function exportAs(fmt) {
  // 直接开新窗口会触发 cookie 鉴权；用隐藏 <a> 更安静
  const a = document.createElement('a')
  a.href = `/api/items/${props.itemId}/export?fmt=${fmt}`
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}
</script>

<template>
  <div class="audio-transcript">
    <div v-if="!hasAudio" class="hint dim">
      这条素材还没有归档音频。
    </div>

    <template v-else>
      <audio
        ref="audioEl"
        :src="audioUrl"
        controls
        preload="metadata"
        class="player"
        @timeupdate="onTimeUpdate"
        @play="playing = true"
        @pause="playing = false"
      ></audio>

      <div class="row wrap bar">
        <span class="dim mono" style="font-size: 12px">
          {{ clock(currentTime) }} / {{ clock(durationMs / 1000) }} · {{ segments.length }} 段
        </span>
        <span class="grow"></span>
        <button class="btn ghost sm" :disabled="!segments.length" @click="exportAs('srt')">
          导出 SRT
        </button>
        <button class="btn ghost sm" @click="exportAs('txt')">导出 TXT</button>
      </div>

      <div v-if="error" class="danger-text" style="font-size: 12px">{{ error }}</div>

      <div v-if="loading" class="dim" style="font-size: 12px">读取分段…</div>

      <div v-else-if="segments.length" class="segments">
        <button
          v-for="(seg, index) in segments"
          :key="index"
          class="seg"
          :class="{ active: index === activeIndex }"
          @click="seek(seg, index)"
        >
          <span class="ts mono">{{ clock(seg.start) }}</span>
          <span class="txt">{{ seg.text }}</span>
        </button>
      </div>

      <div v-else class="dim" style="font-size: 12px">没有分段数据。</div>
    </template>
  </div>
</template>

<style scoped>
.audio-transcript {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.player {
  width: 100%;
  height: 36px;
}
.bar {
  gap: 8px;
  align-items: center;
}
.segments {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid var(--line, #2a2f38);
  border-radius: 8px;
  padding: 4px;
}
.seg {
  display: flex;
  gap: 10px;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  border-radius: 6px;
  padding: 6px 8px;
  cursor: pointer;
  color: inherit;
  font: inherit;
  line-height: 1.55;
}
.seg:hover {
  background: rgba(127, 127, 127, 0.1);
}
.seg.active {
  background: rgba(80, 140, 255, 0.16);
}
.ts {
  flex: 0 0 auto;
  font-size: 11px;
  opacity: 0.65;
  padding-top: 2px;
}
.txt {
  flex: 1 1 auto;
  font-size: 13px;
}
.hint {
  font-size: 12px;
  line-height: 1.7;
}
</style>
