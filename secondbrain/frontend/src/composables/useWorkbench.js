/**
 * 采集区上下文（v42 从 WorkbenchView.vue 抽出的组合式函数）。
 *
 * 拆分前 714 行：状态条轮询、采集解析、转写正文、配图插入全在一个 setup，
 * 样式还有 227 行。现在状态与动作集中在这里，模板在 views/workbench/ 下。
 * 逻辑逐行切片搬来，未改业务。
 */
import { ref, computed, onMounted, onUnmounted, inject, provide } from 'vue'
import { collectApi, itemsApi, workbenchApi, uploadApi } from '../api/client'

const WORKBENCH_KEY = Symbol('WORKBENCH_KEY')

function createContext() {
  const toast = inject('toast')

  // ---------------- 采集输入 ----------------
  const rawText = ref('')
  const parsed = ref(null)
  const parsing = ref(false)
  const submitting = ref(false)
  const quickNoteText = ref('')
  const showQuickNote = ref(false)

  // ---------------- 状态 ----------------
  const states = ref([])
  const stats = ref({})
  const loading = ref(false)
  const current = ref(null)
  const content = ref(null)
  const loadingContent = ref(false)
  const retrying = ref(false)

  // ---------------- 解析 ----------------

  // ---------------- 配图 ----------------
  const shotPosition = ref(0)
  const shotCaption = ref('')
  const shotFile = ref(null)
  const shotUrl = ref('')
  const shotBusy = ref(false)
  const shots = ref([])

  const platformMeta = {
    bilibili: { label: 'B站', color: 'var(--bilibili)' },
    douyin: { label: '抖音', color: 'var(--douyin)' },
    kuaishou: { label: '快手', color: 'var(--kuaishou)' },
    xiaohongshu: { label: '小红书', color: 'var(--xiaohongshu)' },
    web: { label: '网页', color: 'var(--web)' },
    quick_note: { label: '速记', color: 'var(--ok)' }
  }
  const platformOf = (p) => platformMeta[p] || { label: p, color: 'var(--web)' }

  const stateClass = (s) => ({ run: 'accent', done: 'ok', fail: 'danger', wait: '' })[s] || ''

  const items = computed(() => parsed.value?.items || [])
  // 可入库 = 有链接 + 没重复。
  // 「只有标题没链接」的条目（比如粘贴文本里的说明行）不能提交——
  // 提交了后端会 422，前端还只能显示 [object Object]。
  const validItems = computed(() => items.value.filter((i) => i.url && !i.existing))
  const dupCount = computed(() => items.value.filter((i) => i.existing).length)
  const noUrlCount = computed(() => items.value.filter((i) => !i.url).length)

  // ---------------- 数据加载 ----------------
  async function loadStatus() {
    loading.value = true
    try {
      const data = await workbenchApi.status(50)
      states.value = data.items || []
      stats.value = data.stats || {}
      if (!current.value && states.value.length) {
        await selectItem(states.value[0].id)
      }
    } catch (e) {
      toast('加载状态失败：' + e.message, 'error')
    } finally {
      loading.value = false
    }
  }

  /** 状态条上点条目 → 选中该任务（下方解析区联动） */
  async function pickTask(item) {
    if (!item || item.id === current.value?.id) return
    await selectItem(item.id)
  }

  /** 当前任务能不能重试。 */
  const canRetry = computed(() => {
    const s = current.value?.status
    return s === 'extract_failed' || s === 'ai_failed' || s === 'raw_ready'
  })

  /**
   * 转写正文按行切分（v36：用户要求「转写区要有行标」）。
   *
   * 为什么不用一个 <pre> 里塞全文、旁边画一条行号列：
   * `white-space: pre-wrap` 下长行会折行，行号就与实际视觉行错位了。
   * 每行一个 flex 行（行号 + 文本）能保证**行号永远对着逻辑行**，
   * 折行只是这一行的第二视觉行，符合代码编辑器的常规手感。
   */
  const bodyLines = computed(() => {
    const body = content.value?.body
    if (!body) return ['（还没有原文）']
    return String(body).replace(/\r\n/g, '\n').split('\n')
  })

  const bodyCharCount = computed(() => (content.value?.body || '').length)

  /**
   * 重试当前任务（v31：采集区直接重试，不用跳去状态页）。
   *
   * 后端 /api/items/{id}/retry 会按状态决定重跑哪一段：
   *   extract_failed → 重跑抓取
   *   ai_failed       → 重跑 AI
   *   raw_ready       → 重跑 AI（原文就绪，等你点 AI 提取要点）
   */
  async function retryCurrent() {
    if (!current.value || retrying.value) return
    retrying.value = true
    try {
      await itemsApi.retry(current.value.id)
      toast('已重新提交，稍等片刻…', 'success')
      // 状态条是轮询的，等一轮让它自己刷新
      await new Promise((r) => setTimeout(r, 1500))
      await loadStatus()
      const fresh = states.value.find((s) => s.id === current.value.id)
      if (fresh) current.value = fresh
    } catch (e) {
      toast('重试失败：' + e.message, 'error')
    } finally {
      retrying.value = false
    }
  }

  async function selectItem(id) {
    current.value = states.value.find((s) => s.id === id) || null
    loadingContent.value = true
    try {
      const data = await itemsApi.content(id)
      content.value = data
      shots.value = (data.frontmatter?.screenshots || []).slice()
      shotPosition.value = 0
    } catch (e) {
      content.value = null
      toast('读取原文失败：' + e.message, 'error')
    } finally {
      loadingContent.value = false
    }
  }

  // ---------------- 采集 ----------------

  /** 只解析，不提交（保留给「先看看抓到了什么」的场合）。 */
  async function doParse() {
    if (!rawText.value.trim()) {
      toast('请先粘贴分享文案或链接', 'warn')
      return false
    }
    parsing.value = true
    try {
      parsed.value = await collectApi.parse(rawText.value)
      if (!parsed.value.count) {
        toast('没有解析到任何链接，请检查粘贴内容', 'warn')
        return false
      }
      return true
    } catch (e) {
      toast('解析失败：' + e.message, 'error')
      return false
    } finally {
      parsing.value = false
    }
  }

  function removeItem(index) {
    parsed.value.items.splice(index, 1)
  }

  function removeDuplicates() {
    parsed.value.items = validItems.value.slice()
  }

  async function doSubmit() {
    if (!validItems.value.length) {
      toast(
        noUrlCount.value
          ? `没有可入库的条目（${noUrlCount.value} 条只有文字没有链接）`
          : '没有可入库的条目',
        'warn'
      )
      return
    }
    submitting.value = true
    try {
      const res = await collectApi.submit({
        entries: validItems.value.map((i) => ({
          url: i.url,
          platform: i.platform,
          title: i.title
        }))
      })
      const skipped = (res && res.skipped) || []
      const msg = `已入库 ${validItems.value.length} 条，正在后台抓取原文`
      toast(skipped.length ? `${msg}（跳过 ${skipped.length} 条无链接）` : msg, 'success')
      rawText.value = ''
      parsed.value = null
      await loadStatus()
    } catch (e) {
      toast('入库失败：' + e.message, 'error')
    } finally {
      submitting.value = false
    }
  }

  /**
   * 一键「解析并入库」（v31：合并原来的两步）。
   *
   * 流程：解析 → 立刻提交。
   * 解析失败就停在这一步，textarea 内容保留，用户改完再点一次即可 ——
   * 不需要"先点解析、再点入库"两个来回。
   */
  async function parseAndSubmit() {
    if (submitting.value) return
    const ok = await doParse()
    if (!ok) return
    await doSubmit()
  }

  async function doQuickNote() {
    const text = quickNoteText.value.trim()
    if (!text) {
      toast('速记内容不能为空', 'warn')
      return
    }
    try {
      await collectApi.quickNote(text)
      toast('速记已加入队列', 'success')
      quickNoteText.value = ''
      showQuickNote.value = false
      await loadStatus()
    } catch (e) {
      toast('速记失败：' + e.message, 'error')
    }
  }

  // ---------------- 配图 ----------------
  function onPickFile(e) {
    shotFile.value = e.target.files?.[0] || null
    if (!shotFile.value) shotUrl.value = ''
  }

  async function insertShot() {
    if (!current.value) return
    let ref = shotUrl.value.trim()
    if (shotFile.value) {
      shotBusy.value = true
      try {
        const up = await uploadApi.screenshot(shotFile.value)
        ref = up.relative || up.path
      } catch (e) {
        toast('图片上传失败：' + e.message, 'error')
        shotBusy.value = false
        return
      }
    }
    if (!ref) {
      toast('请选择图片文件或填写图片地址', 'warn')
      shotBusy.value = false
      return
    }
    shotBusy.value = true
    try {
      const r = await workbenchApi.insertScreenshot(current.value.id, {
        position: shotPosition.value,
        image_ref: ref,
        caption: shotCaption.value
      })
      shots.value = r.screenshots || []
      shotCaption.value = ''
      shotFile.value = null
      shotUrl.value = ''
      toast(`配图已插入第 ${r.position + 1} 段前`, 'success')
      await selectItem(current.value.id)
    } catch (e) {
      toast('插入配图失败：' + e.message, 'error')
    } finally {
      shotBusy.value = false
    }
  }

  async function removeShot(index) {
    if (!current.value) return
    try {
      const r = await workbenchApi.removeScreenshot(current.value.id, index)
      shots.value = r.screenshots || []
      toast('配图已移除', 'success')
      await selectItem(current.value.id)
    } catch (e) {
      toast('移除失败：' + e.message, 'error')
    }
  }

  // ---------------- 生命周期 ----------------
  let timer = null
  onMounted(async () => {
    await loadStatus()
    timer = setInterval(loadStatus, 3000)
  })
  onUnmounted(() => {
    if (timer) clearInterval(timer)
  })

  return {
    rawText,
    parsed,
    parsing,
    submitting,
    quickNoteText,
    showQuickNote,
    states,
    stats,
    loading,
    current,
    content,
    loadingContent,
    retrying,
    shotPosition,
    shotCaption,
    shotFile,
    shotUrl,
    shotBusy,
    shots,
    platformOf,
    stateClass,
    items,
    validItems,
    dupCount,
    noUrlCount,
    loadStatus,
    pickTask,
    canRetry,
    bodyLines,
    bodyCharCount,
    retryCurrent,
    selectItem,
    doParse,
    removeItem,
    removeDuplicates,
    doSubmit,
    parseAndSubmit,
    doQuickNote,
    onPickFile,
    insertShot,
    removeShot
  }
}

/** 由页面在 setup 顶层调用：创建上下文并下发给子组件。 */
export function provideWorkbench() {
  const ctx = createContext()
  provide(WORKBENCH_KEY, ctx)
  return ctx
}

/** 子组件调用；漏了 provide 立刻报错，不静默拿到 undefined。 */
export function useWorkbench() {
  const ctx = inject(WORKBENCH_KEY, null)
  if (!ctx) throw new Error('useWorkbench() 必须在 provideWorkbench() 的子组件中调用')
  return ctx
}
