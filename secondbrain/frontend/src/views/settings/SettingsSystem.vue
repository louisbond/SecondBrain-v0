<script setup>
/**
 * 设置 · 系统组（v42 从 SettingsView.vue 拆出）。
 *
 * 状态与动作来自注入上下文，本组件只负责渲染与绑定。
 *
 * v0.7.5：网络卡片改成「能直接照着做」。之前只显示 127.0.0.1:7688，
 * 用户拿手机输这个地址必然失败 —— 回环地址在手机上指向手机自己。
 * 现在直接给出局域网 IP、可复制的带 token 链接，以及防火墙/重启提示。
 */
import { inject } from 'vue'
import { useSettings } from '../../composables/useSettings'

const { settings, autostart, toggleLan, toggleAutostart, setNeverSleep, shutdown } = useSettings()
const toast = inject('toast')

/** 剪贴板：LAN 下是 http:// 非安全上下文，navigator.clipboard 不可用，要有兜底。 */
async function copyText(text, label) {
  if (!text) return
  let ok
  try {
    await navigator.clipboard.writeText(text)
    ok = true
  } catch {
    const el = document.createElement('textarea')
    el.value = text
    el.style.position = 'fixed'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    el.remove()
  }
  toast?.(ok ? `已复制${label || ''}` : `复制失败，请手动选中：${text}`, ok ? 'success' : 'error')
}
</script>

<template>
  <div class="settings-group">
    <!-- 网络 -->
    <div class="card">
      <div class="card-title"><span>🌐</span> 网络</div>
      <div class="row wrap">
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
        <span class="badge">本机 127.0.0.1:{{ settings.web.port }}</span>
      </div>

      <div class="lan-box">
        <div class="field-label">手机 / 其他设备访问地址（同一个 Wi-Fi）</div>
        <div class="row wrap">
          <code class="mono url">{{ settings.web.lan_url || '未检测到局域网 IP' }}</code>
          <button
            class="btn sm"
            :disabled="!settings.web.lan_url"
            @click="copyText(settings.web.lan_url, '地址')"
          >
            复制
          </button>
        </div>

        <div class="hint danger-hint">
          ⚠ <b>不要在手机上输入 127.0.0.1</b>：它只指向设备自己，在手机上永远连不到电脑。
          必须用上面这个 192.168.x.x / 10.x.x.x 的地址。
        </div>

        <div
          v-if="settings.web.allow_lan && settings.web.share_url"
          class="row wrap"
          style="margin-top: 8px"
        >
          <button class="btn sm" @click="copyText(settings.web.share_url, '手机链接')">
            复制带 token 的手机链接（推荐）
          </button>
        </div>
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

    <!-- 程序服务：这里说的「服务」是 SecondBrain 自己的后台进程，不是数据库 -->
    <div class="card">
      <div class="card-title"><span>⏹</span> 程序服务</div>
      <div class="row wrap">
        <button class="btn danger" @click="shutdown">关闭程序服务</button>
      </div>

      <div class="card-note">
        <b>数据库文件（SQLite）</b>
        <div class="mono" style="margin-top: 4px; word-break: break-all">
          {{ settings.paths.db_path }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lan-box {
  margin-top: 12px;
  padding: 12px;
  border-radius: 8px;
  background: var(--bg-elevated, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}

.field-label {
  font-size: 12px;
  color: var(--text-2, #a0a6b8);
  margin-bottom: 6px;
}

.url {
  flex: 1 1 220px;
  min-width: 0;
  font-size: 13px;
  color: var(--accent, #6c8cff);
  word-break: break-all;
  user-select: all;
}

.hint {
  margin-top: 8px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-dim, #6b7387);
}

.danger-hint {
  color: var(--warn, #fbbf24);
}

.ok-hint {
  color: var(--ok, #34d399);
}

.card-note {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--line, #33353d);
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--text-dim, #9a9daa);
}
</style>
