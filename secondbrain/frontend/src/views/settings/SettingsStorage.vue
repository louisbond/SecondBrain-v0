<script setup>
/**
 * 设置 · 存储组（v42 从 SettingsView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { useSettings } from '../../composables/useSettings'

const { settings, newVaultPath, busy, changeVault, scan, reindex, rebuildEmbeddings } =
  useSettings()
</script>

<template>
  <div class="settings-group">
    <!-- Vault -->
    <div class="card">
      <div class="card-title"><span>📁</span> Vault 目录</div>
      <div class="mono dim" style="margin-bottom: 10px">
        {{ settings.vault.path }}
      </div>
      <div class="row">
        <input v-model="newVaultPath" type="text" placeholder="vault 根目录绝对路径" />
        <button class="btn" @click="changeVault">切换</button>
      </div>
      <div class="grid-4" style="margin-top: 12px">
        <div class="stat">
          <div class="value" style="font-size: 13px">01_Inbox</div>
          <div class="label">A 层原文</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size: 13px">02_Processed</div>
          <div class="label">B 层提炼</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size: 13px">03_Notes</div>
          <div class="label">C 层创作</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size: 13px">_reports</div>
          <div class="label">报表</div>
        </div>
      </div>
    </div>

    <!-- 索引 -->
    <div class="card">
      <div class="card-title"><span>🗂</span> 索引维护</div>
      <div class="row wrap">
        <button class="btn" :disabled="busy" @click="scan">扫描存量 vault</button>
        <button class="btn" :disabled="busy" @click="reindex">重建索引</button>
        <button class="btn" :disabled="busy" @click="rebuildEmbeddings">重建向量索引</button>
      </div>
      <div class="dim" style="font-size: 12px; margin-top: 8px">
        md 文件是唯一真相源，索引随时可以重建。目标：5000 条重建 &lt; 60s。
      </div>
    </div>

    <!-- ASR -->
    <div class="card">
      <div class="card-title"><span>🎙</span> 语音转写</div>
      <div class="row wrap">
        <span class="badge">{{ settings.asr.engine }}</span>
        <span class="badge">{{ settings.asr.model_size }}</span>
        <span class="badge">{{ settings.asr.compute }}</span>
        <span class="badge" :class="settings.asr.available ? 'ok' : 'warn'">
          {{ settings.asr.available ? '可用' : '未安装' }}
        </span>
        <span class="badge" :class="settings.asr.ytdlp_exists ? 'ok' : 'warn'">
          yt-dlp {{ settings.asr.ytdlp_exists ? '就绪' : '缺失' }}
        </span>
      </div>
      <div class="dim mono" style="margin-top: 8px">{{ settings.asr.cache_dir }}</div>
    </div>
  </div>
</template>
