<script setup>
/**
 * 设置 · 存储组（v42 从 SettingsView.vue 拆出；v0.12 精简）。
 *
 * 索引维护已并到 VaultPanel 那张卡里（两者本来就是同一件事的不同动作），
 * 这里只留 Vault 目录与语音转写。
 */
import { useSettings } from '../../composables/useSettings'

const { settings, newVaultPath, changeVault } = useSettings()
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
      <div class="card-note">
        md 文件是唯一真相源：这四个目录里的内容就是你的全部数据，
        换电脑时拷走整个目录即可；索引随时能从这些文件重建。
      </div>
    </div>

  </div>
</template>

<style scoped>
.card-note {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--line, #33353d);
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--text-dim, #9a9daa);
}

.card-note code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  font-size: 11px;
}
</style>
