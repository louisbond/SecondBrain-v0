/**
 * 加工区 · 核准上下文（v41 从 ProcessView.vue 抽出的组合式函数）。
 *
 * 拆分前 ProcessView.vue 851 行：队列加载、表单状态、AI 解析、分区树、
 * 标签交互全在一个 setup 里，模板又全在一个文件，改一个 toggle 要翻半天。
 *
 * 现在状态与逻辑集中在这里，模板拆到 views/process/ 下三个组件。
 * 子组件通过 useReview() 拿同一个上下文，**解构成顶层 ref 后模板可自动解包**。
 *
 * 注意：本文件的逻辑是从原文件**逐行切片**搬来的，未改业务逻辑。
 */
import { ref, computed, onMounted, onUnmounted, inject, provide, watch } from 'vue'
import { useRouter } from 'vue-router'
import { reviewApi, itemsApi, workbenchApi, searchApi } from '../api/client'
import { saveDraft } from '../utils/draft'
import { useTagSuggestions } from './useTagSuggestions'

const REVIEW_KEY = Symbol('review-context')

function createReviewContext() {
  const toast = inject('toast')
  const refreshSignal = inject('refreshSignal')
  const router = useRouter()

  // ---------------- 队列 ----------------
  const queue = ref([])
  const manualQueue = ref([])
  const current = ref(null)
  const loading = ref(false)
  const saving = ref(false)

  // ---------------- 核准表单 ----------------
  const form = ref({
    title: '',
    ai_summary: '',
    personal_summary: '',
    tags: [],
    partitions: [],
    confirmed_related: [],
    new_tags: []
  })
  const vocab = ref({ tags: [], partitions: [] })
  const partitionTree = ref([])
  const expanded = ref({})
  const newPartition = ref('')
  // 新建分区时「挂到」哪个父分区（null = 根分区）
  const newPartitionParent = ref(null)
  const reanalyzing = ref(false)

  // ---------------- 模板化解析（并回核准：先设模板/字数 → AI 总结）----------------
  const templateId = ref(null)
  const templates = ref([])
  const minChars = ref(800)
  const charPresets = ref([200, 500, 800, 1000, 1500, 2000, 3000, 5000])
  const parsingBusy = ref(false)
  const parseResult = ref(null)
  const parseText = ref('')

  const summaryTypeLabel = computed(() => {
    const t = current.value?.ai_summary_type
    return { bullets: '要点式', structured: '结构化', one_liner: '一句话' }[t] || '要点式'
  })

  /**
   * AI 来源的可读名称（v32）。
   *
   * 坑：`ai_model` 存的是**内部 provider 名**（openai_compat / ollama /
   * workbuddy），直接展示成 badge 就是「OPENAI_COMPAT」——
   * 用户完全不知道这是什么（截图里右上角那个）。
   * 现在映射成人话：「豆包（火山方舟）/ ollama 本地」。
   */
  const aiModelLabel = computed(() => {
    const m = String(current.value?.ai_model || '').toLowerCase()
    if (!m) return ''
    if (m.includes('openai_compat') || m.includes('ark') || m.includes('doubao')) {
      return '豆包（火山方舟）'
    }
    if (m.includes('ollama')) return 'ollama 本地'
    return m
  })

  /** 这条素材当前处在哪一步（可读描述，替代裸状态码）。 */
  const currentStageHint = computed(() => {
    const s = current.value?.item?.status
    if (s === 'raw_ready') return { text: '原文就绪 · 尚未做 AI 提取', cls: 'accent' }
    if (s === 'pending_review') return { text: 'AI 已提取 · 等你确认', cls: '' }
    if (s === 'ai_failed') return { text: 'AI 提取失败 · 可重试', cls: 'warn' }
    if (s === 'extract_failed') return { text: '抓取失败 · 可重试', cls: 'warn' }
    return { text: current.value?.item?.status || '', cls: '' }
  })

  // v31：采集完成后停在「原文就绪」（raw_ready），没跑过 AI。
  // 这时按钮该叫「AI 提取要点」而不是「重新分析」——语义完全不同。
  const isRawReady = computed(() => current.value?.item?.status === 'raw_ready')

  /**
   * 加工队列的两行分组（v37）。
   *
   * 与采集区「完成/失败/进行中/等待」不同，加工区关心的是**下一步做什么**：
   *   待加工 = pending_review（AI 已提取，等你确认）
   *   待手核 = manualQueue（原文就绪 raw_ready，或抓取/解析失败要人接手）
   */
  const queueGroups = computed(() => {
    const manual = manualQueue.value || []
    const rawList = manual.filter((i) =>
      ['raw_ready', 'extracted', 'extract_failed', 'ai_failed'].includes(i.status)
    )
    const readyList = manual.filter((i) => i.status === 'ready_to_parse')
    return [
      { key: 'raw', label: '未加工', icon: '○', cls: '', items: rawList },
      { key: 'ready', label: '待加工', icon: '✎', cls: 'accent', items: readyList },
      { key: 'pending', label: '待入库', icon: '✚', cls: 'warn', items: queue.value }
    ]
  })

  /**
   * 点队列 chip：
   *   · 未加工  → 加入「待加工」（后端 raw_ready → ready_to_parse）
   *   · 待加工  → 退回「未加工」（再点一下就返回）
   *   · 待入库 / 其它 → 选中该条目开始手核
   */
  async function pickQueue(item) {
    if (!item || !item.id) return
    const st = item.status
    if (st === 'raw_ready' || st === 'ready_to_parse') {
      try {
        await workbenchApi.toggleQueue(item.id)
        await loadQueue()
        toast(
          st === 'raw_ready' ? '已加入「待加工」' : '已退回「未加工」',
          'success'
        )
      } catch (e) {
        toast('切换失败：' + (e.message || e), 'error')
      }
      return
    }
    selectItem(item.id)
  }

  const otherTags = computed(() => {
    const used = new Set([...(current.value?.suggested_tags || []), ...form.value.tags])
    return (vocab.value.tags || [])
      .map((t) => (typeof t === 'string' ? t : t.name))
      .filter((name) => !used.has(name))
  })

  async function loadQueue() {
    loading.value = true
    try {
      const data = await reviewApi.pending()
      queue.value = data.items || []
      // manual 已包含 ai_failed（后端 MANUAL_CONFIRMABLE 覆盖），无需单独取
      manualQueue.value = data.manual || []
      const all = [...queue.value, ...manualQueue.value]
      if (all.length && !current.value) await selectItem(all[0].id)
      if (!all.length) current.value = null
    } catch (e) {
      toast('加载队列失败：' + e.message, 'error')
    } finally {
      loading.value = false
    }
  }

  async function loadVocab() {
    try {
      const [t, p] = await Promise.all([
        searchApi.vocabulary(),
        fetch('/api/partitions').then((r) => r.json())
      ])
      // 词表保持对象数组
      vocab.value = {
        tags: t.tags || [],
        partitions: p.partitions || []
      }
      partitionTree.value = p.tree || []
    } catch {
      /* 忽略 */
    }
  }

  async function loadTemplates() {
    try {
      const data = await workbenchApi.templates()
      templates.value = data.templates || []
    } catch {
      /* 忽略 */
    }
    try {
      const data = await workbenchApi.charsPresets()
      charPresets.value = data.presets || charPresets.value
      minChars.value = data.default || 800
    } catch {
      /* 忽略 */
    }
  }

  async function runParse() {
    if (!current.value) {
      toast('先选一条素材', 'warn')
      return
    }
    parsingBusy.value = true
    parseResult.value = null
    parseText.value = ''
    try {
      const r = await workbenchApi.parse({
        item_id: current.value.item.id,
        template_id: templateId.value,
        min_chars: minChars.value
      })
      parseResult.value = r
      parseText.value = r.content || ''
      // 关键：生成的内容必须回写表单，否则「确认入库」保存的还是旧摘要
      if (r.ok) form.value.ai_summary = parseText.value
      if (r.ok) {
        // 素材太短时后端会下调目标字数（如 73 字素材不可能写 800 字摘要），
        // 告诉用户实际按多少字生成，免得他以为程序没按要求做。
        const note = r.adjusted ? `（素材较短，目标已下调为 ${r.min_chars} 字）` : ''
        toast(`已生成 ${r.chars} 字${note}`, 'success')
      } else {
        // 后端返回了业务失败（如「重试 3 次仍不足 800 字」）——
        // 把原因留在面板上，别只弹一句 toast 就没了。
        toast('生成失败：' + (r.error || '未知原因'), 'error')
      }
    } catch (e) {
      // 网络/500 异常：catch 里要把错误**留在面板上**。
      // 早先这里只弹 toast，parseResult 是 null → 错误区永远不显示，
      // 用户既看不到失败原因，也没有重试的抓手。
      parseResult.value = { ok: false, error: e.message || String(e) }
      toast('生成失败：' + e.message, 'error')
    } finally {
      parsingBusy.value = false
    }
  }

  /** 把解析结果送到原创区（sessionStorage 草稿）。 */
  function sendToOriginal() {
    if (!parseText.value) {
      toast('还没有解析结果', 'warn')
      return
    }
    saveDraft({
      title: form.value.title || current.value?.item?.title || '',
      body: parseText.value,
      tags: [...form.value.tags],
      source: current.value?.item?.id ? `item:${current.value.item.id}` : ''
    })
    toast('已送到原创区，可继续修改', 'success')
    router.push('/original')
  }

  // 标签推荐：点了才生成，优先手稿。逻辑拆到 useTagSuggestions，
  // 否则本文件会顶到单文件 500 行的硬上限。
  const {
    generatedTags,
    tagBusy,
    tagSource,
    generateTags,
    reset: resetTags
  } = useTagSuggestions(toast, () => ({
    itemId: current.value?.item?.id,
    manuscript: form.value.personal_summary || ''
  }))

  async function selectItem(id) {
    current.value = { id, item: { id } }
    resetTags()
    try {
      const data = await reviewApi.get(id)
      current.value = data
      form.value = {
        title: data.item.title || '',
        ai_summary: data.ai_summary || '',
        personal_summary: '',
        // 不再预填 AI 建议标签：等用户写完手稿、点「按手稿生成」再出现
        tags: [],
        partitions: [...(data.suggested_partitions || [])],
        confirmed_related: (data.embedding_related || []).map((r) => r.note_path),
        // 待定标签**默认不勾选**——「词表外的进待定标签，用户勾选才入库」。
        new_tags: []
      }
      parseResult.value = null
      // 上次生成过的稿子要能接着改：从库里载入，避免「生成了但看不到」
      parseText.value = String(data.ai_summary || '')
    } catch (e) {
      toast('读取条目失败：' + e.message, 'error')
    }
  }

  // ---------------- 核准交互 ----------------
  function togglePartition(name) {
    const i = form.value.partitions.indexOf(name)
    if (i >= 0) form.value.partitions.splice(i, 1)
    else form.value.partitions.push(name)
  }

  function togglePartPath(path) {
    const i = form.value.partitions.indexOf(path)
    if (i >= 0) form.value.partitions.splice(i, 1)
    else form.value.partitions.push(path)
  }

  function toggleVocabTag(name) {
    const i = form.value.tags.indexOf(name)
    if (i >= 0) form.value.tags.splice(i, 1)
    else form.value.tags.push(name)
  }

  function toggleNewTag(name) {
    const i = form.value.new_tags.findIndex((t) => t.name === name)
    if (i >= 0) {
      form.value.new_tags.splice(i, 1)
    } else {
      // 保留 AI 给的理由（chip 的 tooltip 要显示它）
      const src = (current.value?.pending_tags || []).find((t) => t.name === name)
      form.value.new_tags.push({ name, reason: src?.reason || '' })
    }
  }

  function isNewTagChecked(name) {
    return form.value.new_tags.some((t) => t.name === name)
  }

  // 待定分区：勾选即视为确认该分区（后端 ensure_by_name 会自动建分区）
  function toggleNewPartition(name) {
    const i = form.value.partitions.indexOf(name)
    if (i >= 0) form.value.partitions.splice(i, 1)
    else form.value.partitions.push(name)
  }

  function isNewPartitionChecked(name) {
    return form.value.partitions.includes(name)
  }

  function addManualTag(event) {
    const value = event.target.value.trim()
    if (!value) return
    if (!form.value.tags.includes(value)) form.value.tags.push(value)
    event.target.value = ''
  }

  async function createPartition(event) {
    const value = event.target.value.trim()
    if (!value) return
    try {
      const res = await fetch('/api/partitions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: value, parent_id: newPartitionParent.value })
      })
      if (!res.ok) throw new Error((await res.json()).detail || '创建失败')
      event.target.value = ''
      newPartitionParent.value = null
      await loadVocab()
      if (!form.value.partitions.includes(value)) form.value.partitions.push(value)
      toast(`已新建分区「${value}」`, 'success')
    } catch (e) {
      toast('新建分区失败：' + e.message, 'error')
    }
  }

  function toggleExpand(path) {
    expanded.value[path] = !expanded.value[path]
  }

  function partSelected(path) {
    return form.value.partitions.includes(path)
  }

  function partCount(node) {
    let n = node.count || 0
    for (const c of node.children || []) n += partCount(c)
    return n
  }

  /** 把解析结果送到原创区（跨页草稿，见 utils/draft.js）。 */
  async function confirm() {
    if (!current.value) return
    saving.value = true
    try {
      const tags = [...form.value.tags]
      for (const t of form.value.new_tags) {
        if (!tags.includes(t.name)) tags.push(t.name)
      }
      const result = await reviewApi.confirm(current.value.item.id, {
        title: form.value.title,
        personal_summary: form.value.personal_summary,
        tags,
        partitions: form.value.partitions,
        confirmed_related: form.value.confirmed_related,
        ai_summary: form.value.ai_summary
      })
      toast(result.digested ? '已入库（含手稿）' : '已入库，手稿待补', 'success')
      current.value = null
      await loadQueue()
    } catch (e) {
      toast('入库失败：' + e.message, 'error')
    } finally {
      saving.value = false
    }
  }

  /** 重新让 AI 分析当前条目（摘要/分区/标签不满意时用）。 */
  async function reanalyze() {
    if (!current.value || reanalyzing.value) return
    const itemId = current.value.item.id
    reanalyzing.value = true
    try {
      await itemsApi.retry(itemId)
      toast('已重新提交分析，稍等片刻…', 'success')
      // 以前是按固定 60 次 × 4 秒（4 分钟）轮询条目有没有摘要 ——
      // 但开深度思考时一条要 10–20 分钟，必然"等到超时还是没变化"。
      // 改成看后台 AI 活动：还在跑就继续等，跑完（或没在跑）再刷新。
      const deadline = Date.now() + 40 * 60 * 1000
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 4000))
        let active = false
        try {
          const p = await workbenchApi.progress()
          active = !!p?.ai?.active
        } catch {
          /* 进度接口偶发失败 → 当作没在跑，下面刷新一次就知道结果 */
        }
        if (active) continue
        const data = await reviewApi.get(itemId)
        await selectItem(itemId)
        toast(
          data.ai_summary ? '重新分析完成' : '分析结束，但仍没有摘要（可看提示或改模板）',
          data.ai_summary ? 'success' : 'warn'
        )
        return
      }
      toast('分析仍在后台执行，先别重复点击，稍后刷新看结果', 'warn')
    } catch (e) {
      toast('重新分析失败：' + e.message, 'error')
    } finally {
      reanalyzing.value = false
    }
  }

  async function skip() {
    if (!current.value) return
    try {
      await reviewApi.skip(current.value.item.id)
      toast('已跳过', 'info')
      current.value = null
      await loadQueue()
    } catch (e) {
      toast('跳过失败：' + e.message, 'error')
    }
  }

  watch(refreshSignal, () => {
    loadQueue()
    loadVocab()
  })

  /**
   * 别的页面改了标题（采集区行内改名）→ 同步这里的缓存。
   * 只更新 title / form.title，**不重拉整条**——否则用户正在写的手稿会被清空。
   */
  function onItemUpdated(ev) {
    const { id, title } = ev?.detail || {}
    if (!id) return
    const curId = current.value?.item?.id || current.value?.id
    if (curId === id) {
      if (current.value?.item) current.value.item.title = title
      form.value.title = title
    }
    loadQueue()
  }

  onMounted(async () => {
    window.addEventListener('sb:item-updated', onItemUpdated)
    await loadQueue()
    await loadVocab()
    loadTemplates()
  })

  onUnmounted(() => {
    window.removeEventListener('sb:item-updated', onItemUpdated)
  })

  /** 标题框失焦即保存：改 DB title，后端 rename_inbox 会同步重命名 A 层 md。 */
  async function saveTitleNow() {
    const t = String(form.value?.title || '').trim()
    const id = current.value?.item?.id
    const old = String(current.value?.item?.title || '').trim()
    if (!id || !t || t === old) return
    try {
      const r = await itemsApi.patch(id, { title: t })
      current.value.item.title = t
      if (r?.renamed_to) current.value.item.inbox_path = r.renamed_to
      toast('标题已保存，文件名同步更新', 'success')
    } catch (e) {
      toast('改名失败：' + (e.message || e), 'error')
    }
  }

  /** 删除当前条目（含 A 层 md），删完自动选队列里的下一条。 */
  async function removeCurrent() {
    const id = current.value?.item?.id || current.value?.id
    if (!id) return
    const name = current.value?.item?.title || current.value?.title || `#${id}`
    if (!confirm(`删除「${name}」？原文文件会一并删掉，不可恢复。`)) return
    try {
      await itemsApi.del(id)
      toast('已删除', 'success')
      current.value = null
      await loadQueue()
    } catch (e) {
      toast('删除失败：' + (e.message || e), 'error')
    }
  }

  return {
    queue,
    manualQueue,
    current,
    loading,
    saving,
    form,
    saveTitleNow,
    vocab,
    partitionTree,
    expanded,
    newPartition,
    newPartitionParent,
    reanalyzing,
    templateId,
    templates,
    minChars,
    charPresets,
    parsingBusy,
    parseResult,
    parseText,
    summaryTypeLabel,
    aiModelLabel,
    currentStageHint,
    isRawReady,
    queueGroups,
    otherTags,
    generatedTags,
    tagBusy,
    tagSource,
    generateTags,
    pickQueue,
    removeCurrent,
    loadQueue,
    loadVocab,
    loadTemplates,
    runParse,
    sendToOriginal,
    selectItem,
    togglePartition,
    togglePartPath,
    toggleVocabTag,
    toggleNewTag,
    isNewTagChecked,
    toggleNewPartition,
    isNewPartitionChecked,
    addManualTag,
    createPartition,
    toggleExpand,
    partSelected,
    partCount,
    confirm,
    reanalyze,
    skip
  }
}

/** 由 ProcessView 在 setup 顶层调用：创建上下文并下发给所有核准子组件。 */
export function provideReview() {
  const ctx = createReviewContext()
  provide(REVIEW_KEY, ctx)
  return ctx
}

/** 子组件调用；漏了 provideReview 时立刻报错，而不是拿到 undefined 静默失败。 */
export function useReview() {
  const ctx = inject(REVIEW_KEY, null)
  if (!ctx) throw new Error('useReview() 必须在 provideReview() 的子组件中调用')
  return ctx
}
