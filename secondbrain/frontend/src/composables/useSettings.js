/**
 * 设置页上下文（v42 从 SettingsView.vue 抽出的组合式函数）。
 *
 * 拆分前 577 行：三组卡片的状态、十几个设置动作、模板全挤在一起。
 * 现在动作集中在这里，ai/storage/system 三组模板各成一个子组件。
 *
 * 管理区按组嵌入 `<SettingsView embedded group="ai" />` 时，
 * 每个实例各自 provide 一份上下文 —— 与拆分前每个实例各自 load 一次等价。
 */
import { ref, onMounted, inject, provide, watch } from 'vue'
import { settingsApi, setToken } from '../api/client'

const SETTINGS_KEY = Symbol('SETTINGS_KEY')

function createContext() {
  const toast = inject('toast')
  const refreshSignal = inject('refreshSignal')

  const settings = ref(null)
  const providers = ref(null)
  const busy = ref(false)
  const newVaultPath = ref('')
  const autostart = ref(null)
  const arkKey = ref('')
  const arkBaseUrl = ref('')
  const arkModel = ref('')
  const arkState = ref(null)

  async function loadSettingsOnly() {
    try {
      settings.value = await settingsApi.get()
      newVaultPath.value = settings.value.vault.path
    } catch {
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
    } catch {
      /* 健康检查失败不阻塞 */
    }
    try {
      autostart.value = await settingsApi.autostart()
    } catch {
      autostart.value = null
    }
    try {
      arkState.value = await settingsApi.getAiKey()
      arkBaseUrl.value = arkState.value.base_url || 'https://ark.cn-beijing.volces.com/api/v3'
      arkModel.value = arkState.value.model || ''
    } catch {
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
      toast(e.detail || e.message || '保存失败', 'error')
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
      toast(e.detail || e.message || '操作失败', 'error')
    }
  }

  async function setNeverSleep() {
    try {
      const r = await settingsApi.neverSleep()
      toast(r.message || '已设置', 'success')
    } catch (e) {
      toast(e.detail || e.message || '设置失败（可能需要管理员权限）', 'warn')
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
      toast(e.detail || e.message || '清除失败', 'error')
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
  watch(refreshSignal, () => {
    loadSettingsOnly()
  })

  onMounted(load)

  return {
    settings,
    providers,
    busy,
    newVaultPath,
    autostart,
    arkKey,
    arkBaseUrl,
    arkModel,
    arkState,
    loadSettingsOnly,
    load,
    applyPreset,
    saveArkKey,
    toggleAutostart,
    setNeverSleep,
    clearArkKey,
    switchProvider,
    resetProvider,
    toggleLan,
    changeVault,
    reindex,
    scan,
    rebuildEmbeddings,
    shutdown
  }
}

/** 由页面在 setup 顶层调用：创建上下文并下发给子组件。 */
export function provideSettings() {
  const ctx = createContext()
  provide(SETTINGS_KEY, ctx)
  return ctx
}

/** 子组件调用；漏了 provide 立刻报错，不静默拿到 undefined。 */
export function useSettings() {
  const ctx = inject(SETTINGS_KEY, null)
  if (!ctx) throw new Error('useSettings() 必须在 provideSettings() 的子组件中调用')
  return ctx
}
