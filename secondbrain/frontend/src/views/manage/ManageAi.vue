<script setup>
/** 设置 → AI 与密钥：原设置页 ai 组，包一层只为统一 .manage-section 布局。 */
import SettingsView from '../SettingsView.vue'
</script>

<template>
  <div class="manage-section">
    <SettingsView embedded group="ai" />
  </div>
</template>

<style scoped>
.manage-section {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
</style>
