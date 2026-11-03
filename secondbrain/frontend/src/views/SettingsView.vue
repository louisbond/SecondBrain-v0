<script setup>
/**
 * 设置页：Provider、vault、LAN、索引重建、prompt 模板、适配器能力、关机。
 */
import { ref, onMounted, inject, watch } from 'vue'
import { settingsApi, setToken } from '../api/client'

const toast = inject('toast')
const refreshSignal = inject('refreshSignal')
// 分组 Tab（与管理区同款交互：同页切换）
const group = ref('ai')

const settings = ref(null)
const providers = ref(null)
const busy = ref(false)
const newVaultPath = ref('')
const autostart = ref(null)
const arkKey = ref('')
const arkBaseUrl = ref('')
const arkModel = ref('')
const arkState = ref(null)

// 常用 OpenAI 兼容端点预设（深度思考模型：加工区的 AI 总结靠它们）
const ENDPOINT_PRESETS = [
  {
    name: '豆包（火山方舟）',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    model: 'doubao-1-5-thinking-pro-250415'
  },
  {
    name: 'DeepSeek 官方',
    base_url: 'https://api.deepseek.com/v1',
    model: 'deepseek-reasoner'
  },
  {
    name: 'DeepSeek（硅基流动）',
    base_url: 'https://api.siliconflow.cn/v1',
    model: 'deepseek-ai/DeepSeek-R1'
  }
]

const supportLevels = {
  full: { label: '完整可用', cls: 'ok' },
  partial: { label: '部分可用', cls: 'warn' },
  unstable: { label: '不稳定', cls: 'danger' },
  manual: { label: '仅手动兜底', cls: '' }
}

async function loadSettingsOnly() {
  try {
    settings.value = await settingsApi.get()
    newVaultPath.value = settings.value.vault.path
  } catch (e) {
    /* 静默：全局刷新失败不该弹窗 */
  }
}

async function load() {
  try {
    settings.value = await settingsApi.get()
    newVaultPath.value = settings.value.vault.path
  } catch (e) {
    toast('加载设置失败：' + e.message, 'error')
  }
  try {
    providers.value = await settingsApi.providers()
  } catch (e) {
    /* 健康检查失败不阻塞 */
  }
  try {
    autostart.value = await settingsApi.autostart()
  } catch (e) {
    autostart.value = null
  }
  try {
    arkState.value = await settingsApi.getAiKey()
    arkBaseUrl.value = arkState.value.base_url || 'https://ark.cn-beijing.volces.com/api/v3'
    arkModel.value = arkState.value.model || ''
  } catch (e) {
    arkState.value = null
  }
}

function applyPreset(p) {
  arkBaseUrl.value = p.base_url
  arkModel.value = p.model
  toast(`已填入「${p.name}」端点与模型，填好密钥后保存即可`, 'success')
}

async function saveArkKey(activate) {
  if (!arkKey.value && !arkState.value?.configured) {
    toast('请先填写 API Key', 'warn')
    return
  }
  try {
    const r = await settingsApi.saveAiKey({
      api_key: arkKey.value,
      base_url: arkBaseUrl.value,
      model: arkModel.value,
      activate
    })
    arkState.value = r
    arkKey.value = ''
    toast(
      activate
        ? `已保存并切换到 openai_compat（密钥 ${r.masked}）`
        : `已保存（密钥 ${r.masked}）`,
      'success'
    )
    await load()
  } catch (e) {
    toast((e.detail || e.message || '保存失败'), 'error')
  }
}

async function toggleAutostart() {
  const installed = autostart.value?.autostart?.installed
  try {
    const r = installed
      ? await settingsApi.disableAutostart()
      : await settingsApi.enableAutostart()
    toast(r.message || (installed ? '已注销开机自启' : '已注册开机自启'), 'success')
    await load()
  } catch (e) {
    toast((e.detail || e.message || '操作失败'), 'error')
  }
}

async function setNeverSleep() {
  try {
    const r = await settingsApi.neverSleep()
    toast(r.message || '已设置', 'success')
  } catch (e) {
    toast((e.detail || e.message || '设置失败（可能需要管理员权限）'), 'warn')
  }
}

async function openLoginTerminal() {
  try {
    const r = await settingsApi.providerLogin()
    toast(r.message, 'info', 8000)
  } catch (e) {
    toast((e.detail || e.message || '打开终端失败'), 'error')
  }
}

async function clearArkKey() {
  if (!confirm('确认清除已保存的 API Key？')) return
  try {
    const r = await settingsApi.saveAiKey({ clear: true })
    arkState.value = r
    arkKey.value = ''
    toast('已清除密钥', 'success')
    await load()
  } catch (e) {
    toast((e.detail || e.message || '清除失败'), 'error')
  }
}

async function switchProvider(name) {
  try {
    await settingsApi.switchProvider(name)
    toast(`已切换到 ${name}`, 'success')
    await load()
  } catch (e) {
    toast('切换失败：' + e.message, 'error')
  }
}

async function resetProvider() {
  try {
    const r = await settingsApi.resetProvider()
    toast(`已恢复主链：${r.provider}`, 'success')
    await load()
  } catch (e) {
    toast('操作失败：' + e.message, 'error')
  }
}

async function toggleLan() {
  const enable = !settings.value.web.allow_lan
  try {
    const r = await settingsApi.setLan(enable)
    if (r.token) {
      setToken(r.token)
      toast('LAN 已开启，token 已保存到本地', 'success')
    } else {
      setToken('')
      toast('LAN 已关闭', 'success')
    }
    if (r.warning) toast(r.warning, 'warn')
    await load()
  } catch (e) {
    toast('操作失败：' + e.message, 'error')
  }
}

async function changeVault() {
  try {
    await settingsApi.setVault(newVaultPath.value)
    toast('vault 路径已更新', 'success')
    await load()
  } catch (e) {
    toast('更新失败：' + e.message, 'error')
  }
}

async function reindex() {
  busy.value = true
  try {
    const r = await settingsApi.reindex()
    toast(`索引重建完成：${r.indexed} 篇，用时 ${r.elapsed_ms}ms`, 'success')
  } catch (e) {
    toast('重建失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function scan() {
  busy.value = true
  try {
    const r = await settingsApi.scan()
    toast(
      `扫描完成：${r.indexed} 篇已分层，${r.unlayered_count} 篇待分层，用时 ${r.elapsed_ms}ms`,
      'success'
    )
    if (r.unlayered_count) {
      toast('存在未分层的老笔记，可在「存量分层」处处理', 'warn')
    }
  } catch (e) {
    toast('扫描失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function rebuildEmbeddings() {
  busy.value = true
  try {
    const r = await settingsApi.rebuildEmbeddings()
    toast(`向量重建完成：${r.count} 条（后端 ${r.backend}）`, 'success')
  } catch (e) {
    toast('重建失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}



async function shutdown() {
  if (!confirm('确认关闭 SecondBrain 服务？')) return
  try {
    await settingsApi.shutdown()
    toast('服务已关闭', 'success')
  } catch (e) {
    toast('关闭失败：' + e.message, 'error')
  }
}

// 全局刷新信号（窗口聚焦 / 切页）→ 重新拉数据，避免状态不同步
// 全局刷新信号 → 只重载设置内容，**不重载 providers**
// （providers 会打 provider 健康检查，网络请求重，重载就是刷屏）
watch(refreshSignal, () => { loadSettingsOnly() })

onMounted(load)
</script>

<template>
  <div>
    <div v-if="!settings" class="empty">
      <div class="icon"><span class="spin">◌</span></div>
      <div>加载中……</div>
    </div>

    <template v-else>
      <!-- 分组 Tab：与管理区同款（同页切换，不做长滚动） -->
      <div class="tabs" style="margin-bottom:14px">
        <button class="tab" :class="{ active: group === 'ai' }" @click="group = 'ai'">
          AI
        </button>
        <button class="tab" :class="{ active: group === 'storage' }" @click="group = 'storage'">
          存储
        </button>
        <button class="tab" :class="{ active: group === 'system' }" @click="group = 'system'">
          系统
        </button>
      </div>

      <!-- ===== AI 组 ===== -->
      <template v-if="group === 'ai'">
      <!-- Provider -->
      <div class="card">
        <div class="card-title"><span>🤖</span> AI Provider</div>
        <div class="row wrap" style="margin-bottom:10px">
          <span class="muted">当前：</span>
          <span class="badge accent">{{ settings.ai.provider }}</span>
          <span v-if="providers?.fallback_active" class="badge warn">
            已自动降级到 {{ providers.current }}
          </span>
          <div class="spacer" style="flex:1"></div>
          <button class="btn ghost sm" @click="resetProvider">恢复主链</button>
          <button
            v-if="providers?.cli?.available"
            class="btn sm"
            @click="openLoginTerminal"
          >
            打开终端完成 CLI 登录
          </button>
        </div>

        <div v-if="providers?.cli?.available" class="dim mono" style="font-size:11.5px;margin-bottom:10px">
          codebuddy CLI：{{ providers.cli.path }}
          <span v-if="providers.cli.is_node_script">（node 脚本，用 {{ providers.cli.node }} 执行）</span>
        </div>

        <div class="grid-2">
          <div
            v-for="p in providers?.providers || []"
            :key="p.name"
            class="list-item"
            style="cursor:pointer;align-items:flex-start"
            @click="switchProvider(p.name)"
          >
            <div class="grow">
              <div class="title">
                {{ p.name }}
                <span v-if="p.active" class="badge accent">当前</span>
              </div>
              <div class="sub">{{ p.model }}</div>
              <div class="sub" :style="{ color: p.ok ? 'var(--ok)' : 'var(--danger)' }">
                {{ p.ok ? '✓ 可用' : '✗ 不可用' }} · {{ p.latency_ms }}ms
              </div>
              <div v-if="p.detail" class="sub wrap" style="font-size:11px">
                {{ p.detail }}
              </div>
            </div>
          </div>
        </div>

        <div class="row" style="margin-top:12px">
          <label class="row" style="margin:0;cursor:pointer">
            <input
              type="checkbox"
              :checked="settings.ai.auto_fallback"
              style="width:auto"
              @change="settingsApi.setAutoFallback($event.target.checked).then(load)"
            />
            自动降级（连续失败 {{ settings.ai.fallback_threshold }} 次或 429 时切 Ollama）
          </label>
        </div>

        <div class="alert info" style="margin-top:12px">
          embedding：{{ settings.ai.embedding_model }}
          <span :class="settings.ai.embedding_real ? 'badge ok' : 'badge warn'">
            {{ settings.ai.embedding_real ? '真实模型' : '降级哈希后端' }}
          </span>
        </div>
      </div>

      <!-- OpenAI 兼容端点（火山方舟） -->
      <div class="card">
        <div class="card-title"><span>🔑</span> API 密钥（OpenAI 兼容端点）</div>
        <div class="dim" style="font-size:12px;margin-bottom:10px">
          适用于火山方舟等 OpenAI 兼容服务。密钥只写入
          <span class="mono">config/secrets.json</span>，不落 config.toml。
          <span v-if="arkState?.configured" class="badge ok" style="margin-left:6px">
            已配置 {{ arkState.masked }}
          </span>
          <span v-else class="badge warn" style="margin-left:6px">未配置</span>
        </div>
        <!-- 预设端点：一键填 base_url + 模型名 -->
        <div class="row wrap" style="margin-bottom:8px">
          <span class="dim" style="font-size:11.5px">常用预设：</span>
          <button
            v-for="p in ENDPOINT_PRESETS"
            :key="p.name"
            class="btn ghost sm"
            @click="applyPreset(p)"
          >
            {{ p.name }}
          </button>
        </div>
        <div class="row wrap">
          <input
            v-model="arkBaseUrl"
            type="text"
            placeholder="base_url，如 https://ark.cn-beijing.volces.com/api/v3"
            style="flex:1;min-width:260px"
          />
        </div>
        <div class="row wrap" style="margin-top:8px">
          <input
            v-model="arkModel"
            type="text"
            placeholder="模型 ID，如 doubao-seed-2-1-pro-260915"
            style="flex:1;min-width:220px"
          />
          <input
            v-model="arkKey"
            type="password"
            placeholder="API Key（留空则保留已保存的）"
            style="flex:1;min-width:220px"
          />
        </div>
        <div class="row" style="margin-top:10px">
          <button class="btn ghost" @click="saveArkKey(false)">仅保存</button>
          <button class="btn" @click="saveArkKey(true)">保存并切换到该链</button>
          <div class="spacer" style="flex:1"></div>
          <button v-if="arkState?.configured" class="btn ghost sm" @click="clearArkKey">
            清除密钥
          </button>
        </div>
      </div>

      </template>

      <!-- ===== 存储组 ===== -->
      <template v-if="group === 'storage'">
      <!-- Vault -->
      <div class="card">
        <div class="card-title"><span>📁</span> Vault 目录</div>
        <div class="mono dim" style="margin-bottom:10px">
          {{ settings.vault.path }}
        </div>
        <div class="row">
          <input v-model="newVaultPath" type="text" placeholder="vault 根目录绝对路径" />
          <button class="btn" @click="changeVault">切换</button>
        </div>
        <div class="grid-4" style="margin-top:12px">
          <div class="stat">
            <div class="value" style="font-size:13px">01_Inbox</div>
            <div class="label">A 层原文</div>
          </div>
          <div class="stat">
            <div class="value" style="font-size:13px">02_Processed</div>
            <div class="label">B 层提炼</div>
          </div>
          <div class="stat">
            <div class="value" style="font-size:13px">03_Notes</div>
            <div class="label">C 层创作</div>
          </div>
          <div class="stat">
            <div class="value" style="font-size:13px">_reports</div>
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
        <div class="dim" style="font-size:12px;margin-top:8px">
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
        <div class="dim mono" style="margin-top:8px">{{ settings.asr.cache_dir }}</div>
      </div>

      </template>

      <!-- ===== 系统组 ===== -->
      <template v-if="group === 'system'">
      <!-- 网络 -->
      <div class="card">
        <div class="card-title"><span>🌐</span> 网络</div>
        <div class="row">
          <label class="row" style="margin:0;cursor:pointer">
            <input
              type="checkbox"
              :checked="settings.web.allow_lan"
              style="width:auto"
              @change="toggleLan"
            />
            允许局域网访问（默认关闭；开启时强制 token 校验）
          </label>
          <div class="spacer" style="flex:1"></div>
          <span class="badge">{{ settings.web.host }}:{{ settings.web.port }}</span>
        </div>
        <div class="alert warn" style="margin-top:12px">
          笔记本休眠或关机 = 服务下线。批量任务运行时请接通电源并把电源策略设为「永不睡眠」。
        </div>
      </div>

      <!-- 开机自启 & 电源 -->
      <div class="card">
        <div class="card-title"><span>⏻</span> 开机自启与电源</div>
        <div class="row">
          <span class="muted">开机自启：</span>
          <span
            class="badge"
            :class="autostart?.autostart?.installed ? 'ok' : ''"
          >
            {{ autostart?.autostart?.installed ? '已注册' : '未注册' }}
          </span>
          <div class="spacer" style="flex:1"></div>
          <button class="btn" @click="toggleAutostart">
            {{ autostart?.autostart?.installed ? '注销开机自启' : '注册开机自启' }}
          </button>
        </div>
        <div class="dim" style="font-size:12px;margin-top:8px">
          使用 Windows 任务计划程序（登录时启动）。需要管理员权限。
        </div>

        <div class="alert warn" style="margin-top:14px">
          {{ autostart?.power?.message || '笔记本休眠或关机 = 服务下线，ASR 队列会暂停（唤醒后自动续跑）。' }}
        </div>
        <div class="row wrap" style="margin-top:8px">
          <button class="btn sm" @click="setNeverSleep">
            设置为接通电源时不睡眠
          </button>
          <span class="dim" style="font-size:11.5px">
            {{ (autostart?.power?.howto || []).join('；') }}
          </span>
        </div>
      </div>

      <!-- 危险操作 -->
      <div class="card">
        <div class="card-title"><span>⏹</span> 服务</div>
        <div class="row">
          <button class="btn danger" @click="shutdown">关闭服务</button>
          <span class="dim mono">
            db: {{ settings.paths.db_path }}
          </span>
        </div>
      </div>
      </template>
    </template>
  </div>
</template>
