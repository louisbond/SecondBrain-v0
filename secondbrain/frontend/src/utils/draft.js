/**
 * 创作草稿的跨页传递。
 *
 * 加工区拆成两页后，核准页的「解析结果 → 创作底稿」需要跨页带数据。
 * 用 sessionStorage 传（不落盘、关标签页即失效、刷新不丢）。
 */

const KEY = 'sb.draft.v1'

export function saveDraft(payload) {
  try {
    sessionStorage.setItem(
      KEY,
      JSON.stringify({
        title: payload.title || '',
        body: payload.body || '',
        tags: payload.tags || [],
        source: payload.source || '',
        at: Date.now()
      })
    )
    return true
  } catch {
    return false
  }
}

export function loadDraft() {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return null
    const data = JSON.parse(raw)
    // 超过 1 小时视为过期
    if (Date.now() - (data.at || 0) > 3600 * 1000) {
      sessionStorage.removeItem(KEY)
      return null
    }
    return data
  } catch {
    return null
  }
}

export function clearDraft() {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    /* 忽略 */
  }
}
