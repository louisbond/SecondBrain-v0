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

  // ---------------- 状态 ----------------
  const states = ref([])
  const stats = ref({})
  // 采集时是否强制下载音频转写（勾选「采集音频」）
  const collectAudio = ref(false)
  // 刚提交的任务 id：等它出现在状态列表里就自动选中（解析区跟着换）
  const pendingSelectId = ref(null)

  // ---------------- 采集核实分组（顶部提示条与底部核实区共用）--------
  // 已采集：抓取完成，等用户核实（入库 / 重解析 / 删除）
  // 采集失败：抓取失败，可重新采集或删除
  const collectedItems = computed(() =>
    (states.value || []).filter((i) => i.status === 'extracted')
  )
  const failedItems = computed(() =>
    (states.value || []).filter(
      // 后端真实产生的 error_reason 取值见 pipeline.py：
      //   asr_failed（转写失败，状态仍是 extracted）
      //   parse_failed（加工失败，只打标记不改状态）
      // 这两种都不满足 status === 'extract_failed'，
      // 不显式列出来的话它们永远不会出现在「采集失败」分组里。
      (i) =>
        i.status === 'extract_failed' ||
        i.error_reason === 'asr_failed' ||
        i.error_reason === 'parse_failed'
    )
  )
  // 有没有「待核实」的东西——顶部提示条与底部核实区都用它决定显不显示
  const hasReview = computed(
    () => collectedItems.value.length > 0 || failedItems.value.length > 0
  )
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
      const prev = current.value
      states.value = data.items || []
      stats.value = data.stats || {}

      // 提交完新链接 → 自动切到它（否则解析区还停在上一条，用户以为"没变"）
      if (pendingSelectId.value) {
        const hit = states.value.find((s) => s.id === pendingSelectId.value)
        if (hit) {
          pendingSelectId.value = null
          await selectItem(hit.id)
          return
        }
      }

      if (!current.value && states.value.length) {
        await selectItem(states.value[0].id)
        return
      }

      // 当前选中条目还在，但**原文长度变了**（采集刚跑完，raw_text 才出现）
      // → 重新拉一次正文，否则解析区一直显示采集前的空内容。
      if (current.value) {
        const fresh = states.value.find((s) => s.id === current.value.id)
        if (fresh && fresh.raw_len !== prev?.raw_len) {
          await selectItem(current.value.id)
        }
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
      // 用 items.length 判断而不是 parsed.count：count 是后端快照，
      // 前端删条目/去重后不会跟着变，两者会打架。
      if (!items.value.length) {
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

  /**
   * 从解析预览里移除一条。
   * 解析即采集 → 条目可能已经入库（有 id），那就要真删；
   * 还没入库（没 id）就只从列表里去掉。
   */
  async function removeItem(index) {
    const it = (parsed.value?.items || [])[index]
    if (!it) return
    if (it.id) {
      const name = it.title || it.url || `#${it.id}`
      if (!confirm(`删除「${name}」？原始文件一并清掉，不可恢复。`)) return
      try {
        await itemsApi.del(it.id)
        toast('已删除', 'success')
      } catch (e) {
        toast('删除失败：' + (e.message || e), 'error')
        return
      }
    }
    parsed.value.items.splice(index, 1)
    await loadStatus()
  }

  function removeDuplicates() {
    parsed.value.items = validItems.value.slice()
  }

  async function doSubmit() {
    if (!validItems.value.length) {
      toast(
        noUrlCount.value
          ? `没有可提交的条目（${noUrlCount.value} 条只有文字没有链接）`
          : '没有可提交的条目',
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
          title: i.title,
          collect_audio: collectAudio.value
        }))
      })
      const skipped = (res && res.skipped) || []
      const accepted = (res && res.accepted) || []
      const msg = `已提交 ${validItems.value.length} 条到采集队列，正在后台抓取原文`
      toast(skipped.length ? `${msg}（跳过 ${skipped.length} 条无链接）` : msg, 'success')
      // 记住刚提交的第一条：等它出现在状态列表就自动选中，
      // 解析区立刻换成新素材（否则用户看到的是"解析区内容还是没变"）
      pendingSelectId.value = accepted[0]?.id || null
      rawText.value = ''
      // **不**清空 parsed：解析出来的列表保留下来当"这次解析了什么"的回看，
      // 而且下面把真实 id 回填进去 —— 这样列表里的改名/删除才是真操作
      // （解析即采集，一步到位，条目已经入库了）。
      if (parsed.value && Array.isArray(parsed.value.items)) {
        const byCanonical = new Map(
          accepted.map((a) => [String(a.url || '').toLowerCase(), a])
        )
        for (const it of parsed.value.items) {
          const hit =
            byCanonical.get(String(it.canonical_url || '').toLowerCase()) ||
            byCanonical.get(String(it.url || '').toLowerCase())
          if (hit && hit.id) {
            it.id = hit.id
            it.inbox_path = hit.inbox_path || it.inbox_path
          }
        }
      }
      await loadStatus()
    } catch (e) {
      toast('提交失败：' + e.message, 'error')
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

  /**
   * 采集队列三态（用户要求）：待采集 / 采集中 / 已采集。
   *
   * 只有采集阶段的条目才出现在这里——已入库的（raw_ready / ready_to_parse /
   * pending_review …）归加工队列管，不要在同一屏出现两次。
   */
  const collectGroups = computed(() => {
    const list = states.value || []
    return [
      {
        key: 'collect-pending',
        label: '待采集',
        icon: '⏳',
        cls: '',
        items: list.filter((i) => ['queued', 'extract_failed'].includes(i.status))
      },
      {
        key: 'collect-doing',
        label: '采集中',
        icon: '⟳',
        cls: 'accent',
        items: list.filter((i) =>
          ['extracting', 'asr', 'asr_failed'].includes(i.status)
        )
      },
      {
        key: 'collect-done',
        label: '已采集',
        icon: '✓',
        cls: 'ok',
        items: list.filter((i) => i.status === 'extracted')
      }
    ]
  })

  return {
    rawText,
    parsed,
    parsing,
    submitting,
    collectAudio,
    states,
    stats,
    collectGroups,
    collectedItems,
    failedItems,
    hasReview,
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
