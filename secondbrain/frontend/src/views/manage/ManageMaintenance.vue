<script setup>
/**
 * 设置 → 维护与系统（v41 从 ManageView.vue 拆出）。
 *
 * 拆分前 ManageView.vue 有 952 行：6 个页签的状态、API 调用、模板全挤在
 * 一个 setup 里，改「创作模板」要在 900 行里找对应变量。
 *
 * 现在每个页签是一个自治组件：自己持有状态、自己 onMounted 拉数据。
 * 父组件只管 tab 切换 + KeepAlive 缓存。
 * 共享的只有 inject('toast')（App.vue 提供），不需要 props 穿透。
 *
 * 注意：本文件的模板/逻辑是从原文件**逐行切片**搬来的，未改业务逻辑。
 */
import { ref, onMounted, inject } from 'vue'
import { workbenchApi } from '../../api/client'
import SettingsView from '../SettingsView.vue'

const toast = inject('toast')

// 诊断 / 维护
const diagnosis = ref(null)
const busy = ref(false)

async function loadDiagnosis() {
  try {
    diagnosis.value = await workbenchApi.diagnose()
  } catch (e) {
    diagnosis.value = { ok: false, checks: [{ name: '诊断失败', ok: false, detail: e.message }] }
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
  <div class="grid-2 manage-section" style="grid-template-columns: 1fr 1fr; align-items: start">
    <div class="card">
      <div class="card-title">
        <span>🩺</span> 自检诊断
        <div class="spacer" style="flex: 1"></div>
        <button class="btn ghost sm" @click="loadDiagnosis">重新检测</button>
      </div>
      <div v-if="!diagnosis" class="dim">检测中…</div>
      <div v-for="(c, i) in diagnosis?.checks || []" :key="i" class="list-item">
        <span class="badge" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? 'OK' : '异常' }}</span>
        <div class="grow">
          <div class="title">{{ c.name }}</div>
          <div class="sub mono">{{ c.detail }}</div>
        </div>
      </div>
    </div>

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

    <!-- 系统（原设置页 system 组：网络 / 开机自启 / 服务），跨两列 -->
    <div style="grid-column: 1/-1">
      <SettingsView embedded group="system" />
    </div>
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
</style>
