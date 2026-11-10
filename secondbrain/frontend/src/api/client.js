/**
 * API 客户端。
 *
 * 统一处理 JSON、错误提示、LAN token。
 */

const BASE = ''

// 手机/别的电脑第一次访问时，用设置页复制出来的「带 token 链接」打开：
//   http://192.168.0.2:7688/?token=xxxx
// 静态页壳不需要 token，但后续每个 /api 请求都要。这里把 token 从地址栏
// 收进 localStorage，之后所有请求都带 x-sb-token；同时立刻从地址栏抹掉，
// 免得它留在浏览历史、截图或"分享此页"里。
const query = new URLSearchParams(window.location.search)
const urlToken = query.get('token') || ''
let lanToken = urlToken || localStorage.getItem('sb_token') || getCookieToken()

export function setToken(token) {
  lanToken = token || ''
  if (token) {
    localStorage.setItem('sb_token', token)
    // cookie 是给「localStorage 被清/微信内置浏览器换了存储分区」兜底的：
    // 后端也认这个 cookie，刷新后不用再拿一次链接。
    document.cookie = `sb_token=${encodeURIComponent(token)}; path=/; max-age=31536000; SameSite=Lax`
  } else {
    localStorage.removeItem('sb_token')
    document.cookie = 'sb_token=; path=/; max-age=0; SameSite=Lax'
  }
}

function getCookieToken() {
  const hit = document.cookie.split('; ').find((part) => part.startsWith('sb_token='))
  return hit ? decodeURIComponent(hit.slice('sb_token='.length)) : ''
}

if (urlToken) {
  setToken(urlToken)
  query.delete('token')
  const rest = query.toString()
  window.history.replaceState(
    null,
    '',
    window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash
  )
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

/**
 * 默认请求超时（毫秒）。
 *
 * v39：以前 fetch **没有任何超时** —— 后端卡住（AI 生成、ASR、索引重建）
 * 时界面就一直转圈，用户既看不到提示也等不到结果，只能强刷。
 * 成熟软件的每个网络调用都必须有上限：
 *   · 普通读接口 30s 足够
 *   · 长任务（模板化解析 / 备份 / 索引）单独放宽
 */
const DEFAULT_TIMEOUT_MS = 30000

/**
 * 读接口超时。
 *
 * 实测所有 GET 都在毫秒级（最慢的 /api/settings 约 0.6s），10s 已经极宽松。
 * 缩短它的唯一目的是配合下面的自动重试：程序重启后 WebView 会复用旧 TCP
 * 连接，首屏那几个 GET 会一直挂到超时 —— 30s 等一轮用户直接以为坏了。
 */
const GET_TIMEOUT_MS = 10000

/** 重试前等待，让旧连接彻底断开（逐次退避，累计约 3.1s）。 */
const RETRY_DELAYS_MS = [300, 800, 2000]

async function request(path, options = {}) {
  const explicit = Number(options.timeout) > 0
  const method = String(options.method || 'GET').toUpperCase()
  const isGet = method === 'GET'
  const timeout = explicit
    ? Number(options.timeout)
    : isGet
      ? GET_TIMEOUT_MS
      : DEFAULT_TIMEOUT_MS
  // 只重试「幂等 + 用默认超时」的 GET。
  // 两次真实踩坑：
  //   1. 程序重启后 WebView 复用了指向旧进程的 TCP 连接，首个 GET 一直挂到超时；
  //   2. 刚双击启动时后端还在做模块冷加载（实测冷启动 30s+），首屏 GET 直接失败。
  // 写操作绝不重试 —— 会重复入库。
  // 重试的判定是 status === 0（网络层失败），4xx/5xx 是业务结果，不重试。
  const retriable = isGet && !explicit
  const maxAttempts = retriable ? RETRY_DELAYS_MS.length + 1 : 1
  let lastErr = null
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      return await _request(path, options, timeout)
    } catch (err) {
      lastErr = err
      const isNetwork = err && err.status === 0
      if (!retriable || !isNetwork || attempt === maxAttempts - 1) throw err
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]))
    }
  }
  throw lastErr
}

async function _request(path, options, timeout) {
  const headers = {
    ...(options.headers || {})
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }
  if (lanToken) headers['x-sb-token'] = lanToken

  // 超时控制：AbortController（浏览器原生，不引依赖）
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  const startedAt = Date.now()

  let res
  try {
    res = await fetch(BASE + path, { ...options, headers, signal: controller.signal })
  } catch (err) {
    // 超时和"连不上"要分开说：前者是后端忙/卡，后者是没启动
    if (err && err.name === 'AbortError') {
      const mins = Math.round(timeout / 60000)
      throw new ApiError(
        `等待超过 ${mins} 分钟仍未返回。` +
          '本地模型开启深度思考时 800 字可能需要 10–20 分钟，任务通常仍在后台执行——' +
          '先别重复点击，稍后刷新页面看结果；若长时间无变化，到「设置 → 维护与系统」看诊断。',
        0,
        String(err)
      )
    }
    throw new ApiError('无法连接后端服务，请确认 SecondBrain 正在运行', 0, String(err))
  } finally {
    clearTimeout(timer)
    if (options.onSlow) {
      const cost = Date.now() - startedAt
      if (cost > timeout * 0.6) options.onSlow(cost)
    }
  }

  if (res.status === 401) {
    // 手机端常常不是从「带 token 的完整链接」进来的：手输 IP、微信内置浏览器
    // 吞掉 query、清过存储……都会 401。与其让每个卡片各甩一条红字，
    // 不如弹一个统一的「访问口令」入口，让用户粘贴一次就能继续。
    window.dispatchEvent(new CustomEvent('sb:auth-required'))
    throw new ApiError('需要访问口令（LAN 模式已开启）', 401, await res.text())
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
  get: (path, options) => request(path, { ...(options || {}) }),
  post: (path, body, options) =>
    request(path, {
      ...(options || {}),
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body)
    }),
  put: (path, body, options) =>
    request(path, { ...(options || {}), method: 'PUT', body: JSON.stringify(body) }),
  patch: (path, body, options) =>
    request(path, { ...(options || {}), method: 'PATCH', body: JSON.stringify(body) }),
  del: (path, options) => request(path, { ...(options || {}), method: 'DELETE' })
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
  manualFill: (id, text, title) => api.post(`/api/items/${id}/manual-fill`, { text, title }),
  // FormData 不能走 api.post（它会 JSON.stringify），直接调 request
  uploadMedia: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request(`/api/items/${id}/upload-media`, {
      method: 'POST',
      body: fd,
      timeout: 900000
    })
  },
  // 直接上传视频/音频建条目（不需要先有链接）——
  // 抖音/快手匿名不给直链时，用户自己保存视频再导入是唯一可靠的路。
  uploadNew: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request('/api/items/upload', {
      method: 'POST',
      body: fd,
      // 500MB 的文件读盘 + 抽音频可能几十秒，别在 5 分钟就断
      timeout: 900000
    })
  },
  // 带时间码的分段（播放器跳转 + 校对）
  segments: (id) => api.get(`/api/items/${id}/segments`),
  retry: (id) => api.post(`/api/items/${id}/retry`),
  // 已采集 → 未加工（入库）：采集队列核实无误后送进加工队列
  del: (id) => api.del(`/api/items/${id}`),
  patch: (id, payload) => api.patch(`/api/items/${id}`, payload),
  remove: (id) => api.del(`/api/items/${id}`),
  confirmCollect: (id) => api.post(`/api/items/${id}/confirm-collect`)
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
  // 点了才生成标签推荐：优先个人手稿，手稿空了后端回退 AI 原文。
  // 同样是 LLM 调用，按长任务给超时。
  suggestTags: (id, payload) =>
    api.post(`/api/review/${id}/suggest-tags`, payload, { timeout: 180000 }),
  confirm: (id, payload) => api.post(`/api/review/${id}/confirm`, payload),
  skip: (id) => api.post(`/api/review/${id}/skip`)
}

export const searchApi = {
  query: (payload) => api.post('/api/search', payload),
  vocabulary: () => api.get('/api/search/vocabulary'),
  similar: (notePath, topK = 10) =>
    api.get(`/api/search/similar?note_path=${encodeURIComponent(notePath)}&top_k=${topK}`),
  backlinks: (notePath) =>
    api.get(`/api/search/backlinks?note_path=${encodeURIComponent(notePath)}`),
  graph: (notePath) => api.get(`/api/search/graph?note_path=${encodeURIComponent(notePath)}`)
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
  move: (notePath, toDir) =>
    api.post('/api/notes/move', { note_path: notePath, to_dir: toDir }),
  importExternal: (content, title) => api.post('/api/notes/import', { content, title }),
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
  weekly: (weekOffset = 0) => api.post(`/api/recap/weekly?week_offset=${weekOffset}`),
  daily: (start, end) => api.post('/api/recap/daily', { start, end }),
  reports: () => api.get('/api/recap/reports')
}

export const settingsApi = {
  get: () => api.get('/api/settings'),
  providers: () => api.get('/api/settings/providers'),
  switchProvider: (provider) => api.post('/api/settings/provider', { provider }),
  resetProvider: () => api.post('/api/settings/provider/reset'),
  setAutoFallback: (enabled) => api.post('/api/settings/auto-fallback', { enabled }),
  setAutoStartOllama: (enabled) => api.post('/api/settings/auto-start-ollama', { enabled }),
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
  // 本地模型（ollama）：采集区 / 加工区就地控件用
  getOllamaSettings: () => api.get('/api/settings/ollama'),
  saveOllamaSettings: (payload) => api.post('/api/settings/ollama', payload),
  // 就地模型控件：跟随当前生效 provider（ollama / openai_compat）
  getAiModel: () => api.get('/api/settings/ai-model'),
  saveAiModel: (payload) => api.post('/api/settings/ai-model', payload),
  // 提取链提示词模板（创作链模板在管理 → 解析模板）
  getPrompts: () => api.get('/api/settings/prompts'),
  savePrompts: (payload) => api.post('/api/settings/prompts', payload),
  resetPrompts: (scope = 'extract') =>
    api.post(`/api/settings/prompts/reset?scope=${encodeURIComponent(scope)}`),
  // 适配器实测：拿一条真实链接跑一次抓取（不写库）
  probeAdapter: (url) =>
    api.post('/api/settings/adapters/probe', { url }, { timeout: 120000 }),
  getParserApi: () => api.get('/api/settings/parser-api'),
  getParserPresets: () => api.get('/api/settings/parser-api/presets'),
  saveParserApi: (payload) => api.post('/api/settings/parser-api', payload),
  testParserApi: (url) => api.post('/api/settings/parser-api/test', { url }),
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
  // 顶栏全局进度：只回汇总 + 当前在跑的那一条，2 秒一次也不重
  progress: () => api.get('/api/workbench/progress'),
  templates: () => api.get('/api/workbench/templates'),
  createTemplate: (payload) => api.post('/api/workbench/templates', payload),
  updateTemplate: (id, payload) => api.put(`/api/workbench/templates/${id}`, payload),
  deleteTemplate: (id) => api.del(`/api/workbench/templates/${id}`),
  // AI 生成走本地大模型，开了深度思考后 800 字实测 10-20 分钟 ——
  // 301s 的旧值会让前端先断开、用户看到"请求超时"却不知道后端还在跑。
  parse: (payload) =>
    api.post('/api/workbench/parse', payload, { timeout: 1800000 }),
  charsPresets: () => api.get('/api/workbench/chars-presets'),
  // 未加工 ↔ 待加工 切换（点一下进队列，再点一下退回）
  graph: (limit = 400) => api.get(`/api/workbench/graph?limit=${limit}`),
  vaultTree: () => api.get('/api/workbench/vault-tree'),
  aggregate: () => api.get('/api/workbench/aggregate'),
  diagnose: () => api.get('/api/workbench/diagnose'),
  cleanCache: () => api.post('/api/workbench/cache/clean'),
  backup: () => api.post('/api/workbench/backup'),
  toggleQueue: (itemId) => api.post(`/api/workbench/queue/${itemId}/toggle`),
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
  create: (name, parentId = null) => api.post('/api/partitions', { name, parent_id: parentId }),
  update: (id, payload) => api.put(`/api/partitions/${id}`, payload),
  remove: (id) => api.del(`/api/partitions/${id}`),
  subtree: (id) => api.get(`/api/partitions/${id}/subtree`),
  seedFromVault: () => api.post('/api/partitions/seed-from-vault')}

/** Vault 索引维护（存量笔记扫描 / 批量补 AI） */
export const vaultApi = {
  scan: () => api.post('/api/vault/scan', null, { timeout: 300000 }),
  estimate: () => api.get('/api/vault/backfill/estimate'),
  progress: () => api.get('/api/vault/backfill/progress'),
  start: (force = false) =>
    api.post('/api/vault/backfill/start', { force }, { timeout: 60000 }),
  retry: () => api.post('/api/vault/backfill/retry', null, { timeout: 60000 }),
  reset: () => api.post('/api/vault/backfill/reset')
}
