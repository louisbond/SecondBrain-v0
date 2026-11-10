<script setup>
/**
 * Vault 索引维护（v0.11）：把后端已有的 scan / backfill 能力接上界面。
 *
 * 之前这 6 个端点全都没有 UI —— 用户把旧笔记放进 vault 后，索引和图谱里看不到，
 * 只能靠命令行。现在：扫描建索引、预估 token、批量补 AI、看进度、重跑失败。
 */
import { ref, onMounted, onUnmounted, inject } from 'vue'
import { vaultApi } from '../../api/client'
import { useSettings } from '../../composables/useSettings'

const toast = inject('toast')
// 重建索引 / 重建向量索引 原本在「设置·存储」里，和这块是同一件事 —— 合并到一张卡
const { busy: idxBusy, reindex, rebuildEmbeddings } = useSettings()

const busy = ref('')
const scanResult = ref(null)
const estimate = ref(null)
const progress = ref(null)
let timer = null

function fmtTime(s) {
  if (!s) return '—'
  return String(s).replace('T', ' ').slice(0, 19)
}

async function refresh() {
  try {
    progress.value = await vaultApi.progress()
  } catch {
    /* 后端可能正忙 */
  }
}

async function doScan() {
  busy.value = 'scan'
  try {
    const r = await vaultApi.scan()
    scanResult.value = r
    toast?.(
      `扫描完成：索引 ${r?.indexed ?? r?.total_files ?? '?'} 篇，未分层 ${r?.unlayered_count ?? 0} 篇`,
      'success'
    )
    await refresh()
  } catch (e) {
    toast?.('扫描失败：' + (e.message || e), 'error')
  } finally {
    busy.value = ''
  }
}

async function doEstimate() {
  busy.value = 'estimate'
  try {
    estimate.value = await vaultApi.estimate()
  } catch (e) {
    toast?.('预估失败：' + (e.message || e), 'error')
  } finally {
    busy.value = ''
  }
}

async function doStart() {
  const est = estimate.value
  const msg = est
    ? `将处理 ${est.count} 篇、约 ${est.estimated_tokens} tokens、${est.batches} 批。开始？`
    : '开始批量补 AI？'
  if (!confirm(msg)) return
  busy.value = 'start'
  try {
    await vaultApi.start(false)
    toast?.('已启动，进度见下方', 'success')
    await refresh()
  } catch (e) {
    toast?.('启动失败：' + (e.message || e), 'error')
  } finally {
    busy.value = ''
  }
}

async function doRetry() {
  busy.value = 'retry'
  try {
    await vaultApi.retry()
    toast?.('已重跑失败项', 'success')
    await refresh()
  } catch (e) {
    toast?.('重跑失败：' + (e.message || e), 'error')
  } finally {
    busy.value = ''
  }
}

async function doReset() {
  if (!confirm('清空补 AI 进度？已完成的不受影响，只是让它可以重新从头统计。')) return
  busy.value = 'reset'
  try {
    await vaultApi.reset()
    await refresh()
    toast?.('进度已清空', 'success')
  } catch (e) {
    toast?.('清空失败：' + (e.message || e), 'error')
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 3000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="card">
    <div class="card-title">
      <span>🗂</span>索引维护
      <div class="spacer" style="flex: 1"></div>
      <span v-if="progress?.running" class="badge accent">补 AI 进行中</span>
      <span v-else class="badge">空闲</span>
    </div>

    <div class="row wrap" style="margin-bottom: 8px">
      <button class="btn" :disabled="!!busy" @click="doScan">
        {{ busy === 'scan' ? '扫描中…' : '扫描 Vault（补索引）' }}
      </button>
      <button class="btn ghost" :disabled="!!busy || !!idxBusy" @click="reindex">
        重建索引
      </button>
      <button class="btn ghost" :disabled="!!busy || !!idxBusy" @click="rebuildEmbeddings">
        重建向量索引
      </button>
    </div>

    <div class="row wrap" style="margin-bottom: 10px">
      <button class="btn ghost" :disabled="!!busy" @click="doEstimate">
        {{ busy === 'estimate' ? '估算中…' : '预估 Token' }}
      </button>
      <button class="btn ghost" :disabled="!!busy || progress?.running" @click="doStart">
        开始补 AI
      </button>
      <button
        class="btn ghost sm"
        :disabled="!!busy || !(progress?.failed > 0)"
        @click="doRetry"
      >
        重跑失败 {{ progress?.failed || 0 }}
      </button>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn ghost sm" :disabled="!!busy || progress?.running" @click="doReset">
        清空进度
      </button>
    </div>

    <!-- 预估结果 -->
    <div v-if="estimate" class="alert info" style="font-size: 12px">
      待补 <b>{{ estimate.count }}</b> 篇 ·
      原文 {{ estimate.total_chars }} 字 ·
      预计 <b>{{ estimate.estimated_tokens }}</b> tokens ·
      {{ estimate.batches }} 批
      <div class="dim" style="margin-top: 4px">{{ estimate.note }}</div>
    </div>

    <!-- 扫描结果 -->
    <div v-if="scanResult" class="alert info" style="font-size: 12px; margin-top: 8px">
      扫描完成：
      <span v-if="scanResult.indexed != null">索引 {{ scanResult.indexed }} 篇 · </span>
      <span v-if="scanResult.unlayered_count">未分层 {{ scanResult.unlayered_count }} 篇 · </span>
      <span v-if="scanResult.added != null">新增 {{ scanResult.added }} · </span>
      <span v-if="scanResult.updated != null">更新 {{ scanResult.updated }}</span>
    </div>

    <!-- 进度 -->
    <div v-if="progress && (progress.running || progress.total > 0)" style="margin-top: 10px">
      <div class="row" style="font-size: 12px; margin-bottom: 4px">
        <span>
          已处理 {{ progress.processed }} / {{ progress.total }}
          <span class="dim">（成功 {{ progress.succeeded }} · 失败 {{ progress.failed }}）</span>
        </span>
        <div class="spacer" style="flex: 1"></div>
        <span class="dim">{{ Math.round(progress.percent || 0) }}%</span>
      </div>
      <div class="track">
        <div class="fill" :style="{ width: (progress.percent || 0) + '%' }"></div>
      </div>
      <div class="dim" style="font-size: 11.5px; margin-top: 4px">
        批次 {{ progress.current_batch }} / {{ progress.total_batches }} ·
        开始 {{ fmtTime(progress.started_at) }}
        <span v-if="progress.finished_at"> · 结束 {{ fmtTime(progress.finished_at) }}</span>
      </div>
    </div>

    <!-- 说明集中放最后：每个按钮「什么时候该点」 -->
    <div class="card-note">
      <b>什么时候用哪个：</b><br />
      · <b>扫描 Vault（补索引）</b> —— 你手动往 vault 里放了一批 md（Obsidian 拷进来、别处恢复），
      搜索和图谱里却看不到。它只补新增/变化的文件，不会动内容。<br />
      · <b>重建索引</b> —— 索引和实际文件对不上（删了文件还搜得到、层级改了不生效），
      或程序升级后表结构变了。会清空索引表再按 md 重扫，<b>不碰 md 文件</b>。<br />
      · <b>重建向量索引</b> —— 相似笔记和图谱连线靠语义向量算的。换了 embedding 模型、
      觉得推荐明显不准时再点；CPU 上每篇都要过一次模型，全库重建比较慢。<br />
      · <b>开始补 AI</b> —— 给「没有 AI 摘要」的旧笔记批量跑一次提取，
      走的是提取链模型（本地免费 / API 按量计费），跑之前可先点「预估 Token」看开销。
    </div>
  </div>
</template>

<style scoped>
.track {
  height: 6px;
  border-radius: 3px;
  background: var(--bg-hover);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--ai));
  transition: width 0.4s ease;
}

.card-note {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--line, #33353d);
  font-size: 11.5px;
  line-height: 1.8;
  color: var(--text-dim, #9a9daa);
}
</style>
