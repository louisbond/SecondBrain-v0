/**
 * API 客户端。
 *
 * 统一处理 JSON、错误提示、LAN token。
 */

const BASE = ''

let lanToken = localStorage.getItem('sb_token') || ''

export function setToken(token) {
  lanToken = token || ''
  if (token) localStorage.setItem('sb_token', token)
  else localStorage.removeItem('sb_token')
}

export function getToken() {
  return lanToken
}

/**
 * 路径段编码。
 *
 * note_path 形如 `03_Notes/2026-11-01-中文标题.md`，含中文与 `/`。
 * 直接拼进 URL 会让浏览器按非 ASCII 发送请求行而失败，
 * 必须逐段 encodeURIComponent 后再用 `/` 连接。
 */
function encodePath(path) {
  return String(path || '')
    .split('/')
    .map((seg) => encodeURIComponent(seg))
    .join('/')
}

/**
 * 笔记详情页的路由地址。
 *
 * note_path 是**绝对路径**（含盘符 `D:`），盘符里的冒号必须编码，
 * 否则会被当成 URL 的 scheme。所有跳转到阅读页的地方都用这个函数，
 * 免得各处自己拼 —— 之前 NoteView 用裸路径、HomeView 用 window.location.href，
 * 两种行为不一致（后者还会整页重载，丢掉 SPA 状态）。
 */
export function noteRoute(path) {
  return `/note/${encodePath(path)}`
}

class ApiError extends Error {
  constructor(message, status, detail) {
    super(typeof message === 'string' ? message : String(message))
    this.status = status
    this.detail = detail
  }
}

/** 把后端错误响应转成人能读的文本。

FastAPI 的 422 校验错误返回 detail 是**数组**：
  {detail: [{loc: ['body','entries',0,'url'], msg: 'Field required', type: 'value_error.missing'}]}
直接 String(数组) 会得到 "[object Object]"——用户完全看不懂。
 */
function readableError(data, status) {
  const fallback = `HTTP ${status}`
  if (!data || typeof data !== 'object') {
    return typeof data === 'string' && data ? data : fallback
  }
  const detail = data.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (!d || typeof d !== 'object') return String(d)
      const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body').join('.') : ''
      const msg = d.msg || '校验失败'
      return loc ? `${loc}：${msg}` : msg
    })
    if (parts.length) return parts.join('；')
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message
    try {
      return JSON.stringify(detail)
    } catch {
      /* 忽略 */
    }
  }
  return fallback
}

async function request(path, options = {}) {
  const headers = {
    ...(options.headers || {})
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }
  if (lanToken) headers['x-sb-token'] = lanToken

  let res
  try {
    res = await fetch(BASE + path, { ...options, headers })
  } catch (err) {
    throw new ApiError('无法连接后端服务，请确认 SecondBrain 正在运行', 0, String(err))
  }

  if (res.status === 401) {
    throw new ApiError('token 无效或缺失（LAN 模式已开启）', 401, await res.text())
  }

  const text = await res.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }

  if (!res.ok) {
    throw new ApiError(readableError(data, res.status), res.status, data)
  }
  return data
}

export const api = {
  get: (path) => request(path),
  post: (path, body) =>
    request(path, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body)
    }),
  put: (path, body) =>
    request(path, { method: 'PUT', body: JSON.stringify(body) }),
  del: (path) => request(path, { method: 'DELETE' })
}

// ---- 具体接口 --------------------------------------------------------

export const healthApi = {
  get: () => api.get('/api/health')
}

export const collectApi = {
  parse: (text) => api.post('/api/collect/parse', { text }),
  /**
   * 提交采集条目。
   *
   * 参数是**整个请求体**，字段与后端 SubmitRequest 一一对应
   * （entries / force_similar）—— 调用方直接透传，不要在这里再包一层。
   *
   * 坑：早先签名是 `submit(entries, forceSimilar)`，
   * 而调用方传了 `{entries: [...]}` → 请求体变成
   * `{entries: {entries: [...]}}` → 后端 422「Input should be a valid list」。
   * 采集区入库因此**从来没成功过**，且前端 catch 到 422 只弹了
   * 一句看不懂的提示。
   */
  submit: (payload) => api.post('/api/collect/submit', payload),
  quickNote: (text, title) => api.post('/api/collect/quick-note', { text, title }),
  stats: () => api.get('/api/collect/stats')
}

export const itemsApi = {
  list: (params = {}) => {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.append(k, v)
    })
    return api.get(`/api/items?${qs.toString()}`)
  },
  get: (id) => api.get(`/api/items/${id}`),
  content: (id) => api.get(`/api/items/${id}/content`),
  manualFill: (id, text, title) =>
    api.post(`/api/items/${id}/manual-fill`, { text, title }),
  retry: (id) => api.post(`/api/items/${id}/retry`),
  remove: (id) => api.del(`/api/items/${id}`)
}

export const jobsApi = {
  list: (params = {}) => {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.append(k, v)
    })
    return api.get(`/api/jobs?${qs.toString()}`)
  },
  retryFailed: () => api.post('/api/jobs/retry-failed'),
  cancel: (id) => api.post(`/api/jobs/${id}/cancel`)
}

export const reviewApi = {
  pending: () => api.get('/api/review/pending'),
  needManuscript: () => api.get('/api/review/need-manuscript'),
  get: (id) => api.get(`/api/review/${id}`),
  confirm: (id, payload) => api.post(`/api/review/${id}/confirm`, payload),
  skip: (id) => api.post(`/api/review/${id}/skip`)
}

export const searchApi = {
  query: (payload) => api.post('/api/search', payload),  vocabulary: () => api.get('/api/search/vocabulary'),
  similar: (notePath, topK = 10) =>
    api.get(`/api/search/similar?note_path=${encodeURIComponent(notePath)}&top_k=${topK}`),
  backlinks: (notePath) =>
    api.get(`/api/search/backlinks?note_path=${encodeURIComponent(notePath)}`),
  graph: (notePath) =>
    api.get(`/api/search/graph?note_path=${encodeURIComponent(notePath)}`)
}

export const notesApi = {
  list: (params = {}) => {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.append(k, v)
    })
    return api.get(`/api/notes?${qs.toString()}`)
  },
  // note_path 含中文与路径分隔符，必须做 URL 编码
  get: (path) => api.get(`/api/notes/${encodePath(path)}`),
  create: (payload) => api.post('/api/notes', payload),
  update: (path, payload) => api.put(`/api/notes/${encodePath(path)}`, payload),
  remove: (path) => api.del(`/api/notes/${encodePath(path)}`),
  importExternal: (content, title) =>
    api.post('/api/notes/import', { content, title }),
  inspiration: (title, body, topK = 5) =>
    api.post('/api/notes/inspiration', { title, body, top_k: topK }),
  archive: (path) => api.post(`/api/notes/${encodePath(path)}/archive`),
  /** 补写个人手稿（已入库但手稿为空的 B 层笔记）。 */
  fillManuscript: (path, manuscript) =>
    api.post('/api/notes/manuscript', { note_path: path, manuscript })
}

export const tagsApi = {
  vocabulary: () => api.get('/api/tags'),
  notesWithTag: (name) => api.get(`/api/tags/${encodeURIComponent(name)}/notes`),
  merge: (src, dst) => api.post('/api/tags/merge', { src, dst }),
  mergePartition: (src, dst) => api.post('/api/tags/merge-partition', { src, dst }),
  rename: (src, dst) => api.post('/api/tags/rename', { src, dst }),
  deactivate: (name) => api.post('/api/tags/deactivate', { name }),
  add: (name) => api.post('/api/tags', { name }),
  addPartition: (name) => api.post('/api/tags/partitions', { name }),
  seed: () => api.post('/api/tags/seed')
}

export const recapApi = {
  card: () => api.get('/api/recap/card'),
  weekly: (weekOffset = 0) =>
    api.post(`/api/recap/weekly?week_offset=${weekOffset}`),
  daily: (start, end) => api.post('/api/recap/daily', { start, end }),
  reports: () => api.get('/api/recap/reports')
}

export const settingsApi = {
  get: () => api.get('/api/settings'),
  providers: () => api.get('/api/settings/providers'),
  switchProvider: (provider) => api.post('/api/settings/provider', { provider }),
  resetProvider: () => api.post('/api/settings/provider/reset'),
  setAutoFallback: (enabled) =>
    api.post('/api/settings/auto-fallback', { enabled }),
  setLan: (allowLan, token) => api.post('/api/settings/lan', { allow_lan: allowLan, token }),
  setVault: (path) => api.post('/api/settings/vault', { path }),
  reindex: () => api.post('/api/settings/reindex'),
  scan: () => api.post('/api/settings/scan'),
  rebuildEmbeddings: () => api.post('/api/settings/embeddings/rebuild'),
  getPrompt: () => api.get('/api/settings/prompt'),
  savePrompt: (template) => api.put('/api/settings/prompt', { template }),
  autostart: () => api.get('/api/settings/autostart'),
  enableAutostart: () => api.post('/api/settings/autostart/enable'),
  disableAutostart: () => api.post('/api/settings/autostart/disable'),
  neverSleep: () => api.post('/api/settings/power/never-sleep'),
  getAiKey: () => api.get('/api/settings/ai-key'),
  saveAiKey: (payload) => api.post('/api/settings/ai-key', payload),
  shutdown: () => api.post('/api/settings/shutdown')
}

export const uploadApi = {
  screenshot: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request('/api/upload', { method: 'POST', body: fd })
  }
}

export { ApiError }

export const workbenchApi = {
  status: (limit = 50) => api.get(`/api/workbench/status?limit=${limit}`),
  templates: () => api.get('/api/workbench/templates'),
  createTemplate: (payload) => api.post('/api/workbench/templates', payload),
  updateTemplate: (id, payload) => api.put(`/api/workbench/templates/${id}`, payload),
  deleteTemplate: (id) => api.del(`/api/workbench/templates/${id}`),
  parse: (payload) => api.post('/api/workbench/parse', payload),
  charsPresets: () => api.get('/api/workbench/chars-presets'),
  graph: (limit = 400) => api.get(`/api/workbench/graph?limit=${limit}`),
  vaultTree: () => api.get('/api/workbench/vault-tree'),
  aggregate: () => api.get('/api/workbench/aggregate'),
  diagnose: () => api.get('/api/workbench/diagnose'),
  cleanCache: () => api.post('/api/workbench/cache/clean'),
  backup: () => api.post('/api/workbench/backup'),
  insertScreenshot: (itemId, payload) =>
    api.post(`/api/workbench/items/${itemId}/screenshots`, payload),
  removeScreenshot: (itemId, index) =>
    api.del(`/api/workbench/items/${itemId}/screenshots/${index}`)
}

/**
 * 创作模板（原创区「空模板」）——v37 从硬编码搬到库里，管理区可增删改。
 * 别和 workbenchApi.templates()（解析模板，给 AI 的提示词）搞混。
 */
export const noteTemplatesApi = {
  list: () => api.get('/api/note-templates'),
  create: (payload) => api.post('/api/note-templates', payload),
  update: (id, payload) => api.put(`/api/note-templates/${id}`, payload),
  remove: (id) => api.del(`/api/note-templates/${id}`),
  restoreDefaults: () => api.post('/api/note-templates/restore-defaults')
}

export const partitionsApi = {
  list: () => api.get('/api/partitions'),
  tree: () => api.get('/api/partitions/tree'),
  create: (name, parentId = null) =>
    api.post('/api/partitions', { name, parent_id: parentId }),
  update: (id, payload) => api.put(`/api/partitions/${id}`, payload),
  remove: (id) => api.del(`/api/partitions/${id}`),
  subtree: (id) => api.get(`/api/partitions/${id}/subtree`),
  seedFromVault: () => api.post('/api/partitions/seed-from-vault')
}
