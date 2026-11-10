/**
 * 加工区标签推荐：点了才生成，优先个人手稿，手稿空回退 AI 原文。
 *
 * 从 useReview.js 拆出（那个文件已顶到单文件 500 行上限）。
 * `getContext()` 由调用方提供，这样本模块不依赖核准表单的具体形状。
 */
import { ref } from 'vue'
import { reviewApi } from '../api/client'

export function useTagSuggestions(toast, getContext) {
  const generatedTags = ref([])
  const tagBusy = ref(false)
  const tagSource = ref('')

  function reset() {
    generatedTags.value = []
    tagSource.value = ''
  }

  async function generateTags() {
    const { itemId, manuscript } = getContext()
    if (!itemId) return
    tagBusy.value = true
    try {
      const res = await reviewApi.suggestTags(itemId, { manuscript: manuscript || '' })
      generatedTags.value = res.tags || []
      tagSource.value = res.source === 'manuscript' ? '个人手稿' : 'AI 原文'
      const count = generatedTags.value.length
      toast?.(
        count
          ? `已按${tagSource.value}生成 ${count} 个标签，点选后才入库`
          : '没生成出标签，可手动添加',
        count ? 'success' : 'warn'
      )
    } catch (e) {
      toast?.('生成标签失败：' + e.message, 'error')
    } finally {
      tagBusy.value = false
    }
  }

  return { generatedTags, tagBusy, tagSource, generateTags, reset }
}
