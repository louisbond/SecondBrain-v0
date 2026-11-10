<script setup>
/**
 * 设置 → 维护与系统（v41 从 ManageView.vue 拆出；v0.7.5 整页重排）。
 *
 * 版式（用户指定）：
 *   · 自检诊断的「内部」项**横排**成一行小卡片 —— 一眼扫完本机状态
 *   · 「外部网络自检」与「维护」**竖排**，逐条看详情（网络类信息长，横排会挤）
 *   · 系统设置放最下面，整页从上到下一条线
 *
 * 只留 `.manage-section` 一个滚动容器：这里也是 flex column，
 * 子项必须 flex:0 0 auto，否则会被压缩、内容叠到下一张卡上（采集与存储页踩过）。
 */
import { ref, computed, onMounted, inject } from 'vue'
import { workbenchApi } from '../../api/client'
import SettingsView from '../SettingsView.vue'

const toast = inject('toast')

// 诊断 / 维护
const diagnosis = ref(null)
const checking = ref(false)
const busy = ref(false)

// 老版本后端没有 group 字段，一律当内部项，避免页面空白
const internalChecks = computed(() =>
  (diagnosis.value?.checks || []).filter((c) => (c.group || '内部') === '内部')
)
const externalChecks = computed(() =>
  (diagnosis.value?.checks || []).filter((c) => c.group === '外部')
)

async function loadDiagnosis() {
  checking.value = true
  try {
    diagnosis.value = await workbenchApi.diagnose()
  } catch (e) {
    diagnosis.value = {
      ok: false,
      checks: [{ name: '诊断失败', ok: false, detail: e.message, group: '内部' }]
    }
  } finally {
    checking.value = false
  }
}

// ---- 维护 ----
async function cleanCache() {
  if (!confirm('清理缓存（音频与临时文件）？数据库与 md 不受影响。')) return
  busy.value = true
  try {
    const res = await workbenchApi.cleanCache()
    toast('缓存已清理', 'success')
    void res
  } catch (e) {
    toast('清理失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function doBackup() {
  busy.value = true
  try {
    const res = await workbenchApi.backup()
    toast(`备份完成：${res.path}`, 'success', 6000)
  } catch (e) {
    toast('备份失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

onMounted(loadDiagnosis)
</script>

<template>
  <div class="maintenance-stack manage-section">
    <!-- 内部自检：横排 -->
    <div class="card">
      <div class="card-title">
        <span>🩺</span> 自检诊断 · 内部
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" :disabled="checking" @click="loadDiagnosis">
          {{ checking ? '检测中…' : '重新检测' }}
        </button>
      </div>
      <div v-if="!diagnosis" class="dim">检测中…</div>
      <div v-else-if="!internalChecks.length" class="dim">没有内部检查项</div>
      <div v-else class="internal-grid">
        <div
          v-for="(c, i) in internalChecks"
          :key="i"
          class="check-tile"
          :class="c.ok ? 'ok' : 'bad'"
        >
          <span class="badge" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? 'OK' : '异常' }}</span>
          <div class="check-name">{{ c.name }}</div>
          <div class="check-detail mono" :title="c.detail">{{ c.detail || '—' }}</div>
        </div>
      </div>
    </div>

    <!-- 外部网络自检：竖排 -->
    <div class="card">
      <div class="card-title">
        <span>🌐</span> 外部网络自检
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" :disabled="checking" @click="loadDiagnosis">
          {{ checking ? '检测中…' : '重新检测' }}
        </button>
      </div>
      <div v-if="!diagnosis" class="dim">检测中…</div>
      <div v-else-if="!externalChecks.length" class="dim">
        后端未提供外部网络检查项（请确认程序是最新版本）
      </div>
      <div v-else>
        <div v-for="(c, i) in externalChecks" :key="i" class="list-item">
          <span class="badge" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? 'OK' : '异常' }}</span>
          <div class="grow">
            <div class="title">{{ c.name }}</div>
            <div class="sub wrap">{{ c.detail }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 维护：竖排 -->
    <div class="card">
      <div class="card-title"><span>🧹</span> 维护</div>
      <div class="list-item">
        <div class="grow">
          <div class="title">清理缓存</div>
          <div class="sub">清掉音频与临时文件；数据库与 md 不受影响</div>
        </div>
        <button class="btn" :disabled="busy" @click="cleanCache">清理</button>
      </div>
      <div class="list-item">
        <div class="grow">
          <div class="title">导出备份</div>
          <div class="sub">把数据库复制一份到 data/backups</div>
        </div>
        <button class="btn" :disabled="busy" @click="doBackup">备份</button>
      </div>
      <div class="alert info" style="margin-top: 10px">
        md 是唯一真相源：即使数据库全丢，也能用「重建索引」从你的库恢复。
      </div>
    </div>

    <!-- 系统（网络 / 开机自启 / 服务）放最下 -->
    <SettingsView embedded group="system" />
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.maintenance-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 滚动容器里的子项不压缩，否则内容会叠到下一张卡上 */
.maintenance-stack > * {
  flex: 0 0 auto;
}

.maintenance-stack > .card {
  margin-bottom: 0;
}

.internal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}

.check-tile {
  min-width: 0;
  padding: 9px 10px;
  border-radius: 8px;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
}

.check-tile.bad {
  border-color: rgba(248, 113, 113, 0.4);
}

.check-name {
  margin-top: 6px;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.check-detail {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-dim, #6b7387);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
}
</style>
