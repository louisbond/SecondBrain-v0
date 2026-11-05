<script setup>
/**
 * 设置 · 系统组（v42 从 SettingsView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 */
import { useSettings } from '../../composables/useSettings'

const { settings, autostart, toggleLan, toggleAutostart, setNeverSleep, shutdown } = useSettings()
</script>

<template>
  <div class="settings-group">
    <!-- 网络 -->
    <div class="card">
      <div class="card-title"><span>🌐</span> 网络</div>
      <div class="row">
        <label class="row" style="margin: 0; cursor: pointer">
          <input
            type="checkbox"
            :checked="settings.web.allow_lan"
            style="width: auto"
            @change="toggleLan"
          />
          允许局域网访问（默认关闭；开启时强制 token 校验）
        </label>
        <div class="spacer" style="flex: 1"></div>
        <span class="badge">{{ settings.web.host }}:{{ settings.web.port }}</span>
      </div>
      <div class="alert warn" style="margin-top: 12px">
        笔记本休眠或关机 = 服务下线。批量任务运行时请接通电源并把电源策略设为「永不睡眠」。
      </div>
    </div>

    <!-- 开机自启 & 电源 -->
    <div class="card">
      <div class="card-title"><span>⏻</span> 开机自启与电源</div>
      <div class="row">
        <span class="muted">开机自启：</span>
        <span class="badge" :class="autostart?.autostart?.installed ? 'ok' : ''">
          {{ autostart?.autostart?.installed ? '已注册' : '未注册' }}
        </span>
        <div class="spacer" style="flex: 1"></div>
        <button class="btn" @click="toggleAutostart">
          {{ autostart?.autostart?.installed ? '注销开机自启' : '注册开机自启' }}
        </button>
      </div>
      <div class="dim" style="font-size: 12px; margin-top: 8px">
        使用 Windows 任务计划程序（登录时启动）。需要管理员权限。
      </div>

      <div class="alert warn" style="margin-top: 14px">
        {{
          autostart?.power?.message ||
          '笔记本休眠或关机 = 服务下线，ASR 队列会暂停（唤醒后自动续跑）。'
        }}
      </div>
      <div class="row wrap" style="margin-top: 8px">
        <button class="btn sm" @click="setNeverSleep">设置为接通电源时不睡眠</button>
        <span class="dim" style="font-size: 11.5px">
          {{ (autostart?.power?.howto || []).join('；') }}
        </span>
      </div>
    </div>

    <!-- 危险操作 -->
    <div class="card">
      <div class="card-title"><span>⏹</span> 服务</div>
      <div class="row">
        <button class="btn danger" @click="shutdown">关闭服务</button>
        <span class="dim mono"> db: {{ settings.paths.db_path }} </span>
      </div>
    </div>
  </div>
</template>
