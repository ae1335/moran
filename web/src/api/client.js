/** API 客户端：统一 fetch 封装 + SSE 解析。 */

const BASE = ''

async function req(path, { method = 'GET', body, headers = {}, signal } = {}) {
  const opts = { method, headers: { ...headers }, signal }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(BASE + path, opts)
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = { raw: text } }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `${res.status} ${res.statusText}`
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    err.status = res.status
    err.payload = data
    throw err
  }
  return data
}

export const api = {
  // 书架 / 书
  health: () => req('/api/health'),
  shelf: () => req('/api/shelf'),
  createBook: (payload) => req('/api/books', { method: 'POST', body: payload }),
  book: (dir) => req(`/api/books/${encodeURIComponent(dir)}`),
  bookStats: (dir) => req(`/api/books/${encodeURIComponent(dir)}/stats`),
  dailyPanel: (dir) => req(`/api/books/${encodeURIComponent(dir)}/daily-panel`),
  publish: (dir, chapters, published = true) =>
    req(`/api/books/${encodeURIComponent(dir)}/publish`, { method: 'POST', body: { chapters, published } }),

  // 章纲
  outline: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/outline/${ch}`),
  putOutline: (dir, ch, patch) => req(`/api/books/${encodeURIComponent(dir)}/outline/${ch}`, { method: 'PUT', body: patch }),
  confirmOutline: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/outline/${ch}/confirm`, { method: 'POST' }),
  plan: (dir, payload) => req(`/api/books/${encodeURIComponent(dir)}/plan`, { method: 'POST', body: payload }),

  // 章节
  chapters: (dir) => req(`/api/books/${encodeURIComponent(dir)}/chapters`),
  chapter: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/chapters/${ch}`),
  putChapter: (dir, ch, text, commit = false) =>
    req(`/api/books/${encodeURIComponent(dir)}/chapters/${ch}`, { method: 'PUT', body: { text, commit } }),
  adoptVersion: (dir, ch, v) =>
    req(`/api/books/${encodeURIComponent(dir)}/chapters/${ch}/version/${v}/adopt`, { method: 'POST' }),
  scoreVersion: (dir, ch, v, score, note) =>
    req(`/api/books/${encodeURIComponent(dir)}/chapters/${ch}/version/${v}/score`, { method: 'POST', body: { score, note } }),

  // 审稿 / Curator
  review: (dir, ch, opts = {}) =>
    req(`/api/books/${encodeURIComponent(dir)}/review`, { method: 'POST', body: { ch, ...opts } }),
  getReview: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/review/${ch}`),
  curate: (dir, ch, useModel = true) =>
    req(`/api/books/${encodeURIComponent(dir)}/curate`, { method: 'POST', body: { ch, use_model: useModel } }),
  curatePack: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/curate/${ch}`),
  curateConfirm: (dir, ch, ids = null, commit = true) =>
    req(`/api/books/${encodeURIComponent(dir)}/curate/confirm`, { method: 'POST', body: { ch, accepted_ids: ids, commit } }),

  // 装配
  assemble: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/assemble`, { method: 'POST', body: { ch } }),
  assembled: (dir, ch) => req(`/api/books/${encodeURIComponent(dir)}/assembled/${ch}`),

  // 真相源
  truthList: (dir) => req(`/api/books/${encodeURIComponent(dir)}/truth`),
  truthRead: (dir, path) => req(`/api/books/${encodeURIComponent(dir)}/truth/${path}`),
  truthWrite: (dir, path, content, commit = true) =>
    req(`/api/books/${encodeURIComponent(dir)}/truth/${path}`, { method: 'PUT', body: { content, commit } }),
  foreshadow: (dir) => req(`/api/books/${encodeURIComponent(dir)}/foreshadow`),
  patchForeshadow: (dir, id, patch) =>
    req(`/api/books/${encodeURIComponent(dir)}/foreshadow/${id}`, { method: 'PATCH', body: patch }),
  timeline: (dir) => req(`/api/books/${encodeURIComponent(dir)}/timeline`),
  threads: (dir) => req(`/api/books/${encodeURIComponent(dir)}/threads`),
  profiles: (dir) => req(`/api/books/${encodeURIComponent(dir)}/profiles`),

  // 模型层
  providers: () => req('/api/providers'),
  providersHealth: () => req('/api/providers/health'),
  reloadRouting: () => req('/api/routing/reload', { method: 'POST' }),
  usage: (days = 7) => req(`/api/usage?days=${days}`),

  // 导出 / 反 AI 味
  exportBook: (dir, payload) => req(`/api/books/${encodeURIComponent(dir)}/export`, { method: 'POST', body: payload }),
  reviewSuggest: (dir, ch, indices) =>
    req(`/api/books/${encodeURIComponent(dir)}/review/${ch}/suggest`, {
      method: 'POST',
      body: { indices: indices ?? null },
    }),

  generateNames: (dir, payload) => req(`/api/books/${encodeURIComponent(dir)}/names`, { method: 'POST', body: payload }),
  listNames: (dir) => req(`/api/books/${encodeURIComponent(dir)}/names`),

  // 设定集 / 封面 / 模板（M8）
  settingBook: (dir, opts = {}) => {
    const q = new URLSearchParams()
    if (opts.includeChapters) q.set('include_chapters', 'true')
    if (opts.outlineLimit != null) q.set('outline_limit', String(opts.outlineLimit))
    const qs = q.toString()
    return req(`/api/books/${encodeURIComponent(dir)}/setting-book${qs ? `?${qs}` : ''}`)
  },
  settingBookExport: (dir, opts = {}) => {
    const q = new URLSearchParams()
    if (opts.includeChapters) q.set('include_chapters', 'true')
    if (opts.outlineLimit != null) q.set('outline_limit', String(opts.outlineLimit))
    const qs = q.toString()
    return req(`/api/books/${encodeURIComponent(dir)}/setting-book${qs ? `?${qs}` : ''}`, { method: 'POST' })
  },
  coverPrompt: (dir) => req(`/api/books/${encodeURIComponent(dir)}/cover/prompt`),
  coverGenerate: (dir, opts = {}) => {
    const q = new URLSearchParams()
    if (opts.width) q.set('width', String(opts.width))
    if (opts.height) q.set('height', String(opts.height))
    if (opts.useImageApi) q.set('use_image_api', 'true')
    const qs = q.toString()
    return req(`/api/books/${encodeURIComponent(dir)}/cover${qs ? `?${qs}` : ''}`, { method: 'POST' })
  },
  coverUrl: (dir) => `/api/books/${encodeURIComponent(dir)}/cover/file`,
  covers: (dir) => req(`/api/books/${encodeURIComponent(dir)}/covers`),
  templates: (dir) => req(`/api/books/${encodeURIComponent(dir)}/templates`),
  allTemplates: () => req('/api/templates'),

  exportPrecheck: (dir, platform) =>
    req(
      `/api/books/${encodeURIComponent(dir)}/export/precheck` +
        (platform ? `?platform=${encodeURIComponent(platform)}` : ''),
    ),
  polish: (dir, ch, useModel = false, apply = false) =>
    req(`/api/books/${encodeURIComponent(dir)}/polish`, { method: 'POST', body: { ch, use_model: useModel, apply } }),

  // 反馈
  feedback: (dir) => req(`/api/books/${encodeURIComponent(dir)}/feedback`),
  feedbackStats: (dir, payload) =>
    req(`/api/books/${encodeURIComponent(dir)}/feedback/stats`, { method: 'POST', body: payload }),
  feedbackReview: (dir, ch, text) =>
    req(`/api/books/${encodeURIComponent(dir)}/feedback/reviews/${ch}`, { method: 'POST', body: { text } }),
  recheck: (dir, payload) => req(`/api/books/${encodeURIComponent(dir)}/feedback/recheck`, { method: 'POST', body: payload }),
  recheckConfirm: (dir, payload) =>
    req(`/api/books/${encodeURIComponent(dir)}/feedback/recheck/confirm`, { method: 'POST', body: payload }),

  // 拆书
  benchmarks: (dir) => req(`/api/books/${encodeURIComponent(dir)}/benchmarks`),
  benchmarkImport: (dir, title, content, useModel = true) =>
    req(`/api/books/${encodeURIComponent(dir)}/benchmarks/import`, { method: 'POST', body: { title, content, use_model: useModel } }),
  benchmarkDetail: (dir, id) => req(`/api/books/${encodeURIComponent(dir)}/benchmarks/${id}`),

  // 索引
  indexStats: (dir) => req(`/api/books/${encodeURIComponent(dir)}/index/stats`),
  indexBuild: (dir, full = false) => req(`/api/books/${encodeURIComponent(dir)}/index/build?full=${full}`, { method: 'POST' }),
  indexVerify: (dir) => req(`/api/books/${encodeURIComponent(dir)}/index/verify`),

  // git
  gitLog: (dir, limit = 50) => req(`/api/books/${encodeURIComponent(dir)}/git/log?limit=${limit}`),
  gitShow: (dir, rev, path) =>
    req(`/api/books/${encodeURIComponent(dir)}/git/show?rev=${encodeURIComponent(rev)}&path=${encodeURIComponent(path)}`),
  gitHistory: (dir, path) => req(`/api/books/${encodeURIComponent(dir)}/git/history?path=${encodeURIComponent(path)}`),

  nightly: (dir) => req(`/api/books/${encodeURIComponent(dir)}/jobs/nightly`, { method: 'POST' }),
}

/**
 * SSE 流式生成。返回一个可取消的句柄。
 * @param {string} dir
 * @param {number} ch
 * @param {{onDelta:Function,onDone:Function,onError:Function,onStart:Function}} handlers
 */
export function generateStream(dir, ch, handlers = {}, opts = {}) {
  const controller = new AbortController()
  const run = async () => {
    try {
      const res = await fetch(`/api/books/${encodeURIComponent(dir)}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ch, stream: true, as_version: !!opts.asVersion }),
        signal: controller.signal,
      })
      if (!res.ok) {
        const t = await res.text()
        let msg = t
        try { msg = JSON.parse(t).detail || t } catch {}
        handlers.onError?.(msg)
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const blocks = buf.split('\n\n')
        buf = blocks.pop() || ''
        for (const block of blocks) {
          const lines = block.split('\n')
          let event = 'message'
          let data = ''
          for (const line of lines) {
            if (line.startsWith('event:')) event = line.slice(6).trim()
            else if (line.startsWith('data:')) data += line.slice(5).trim()
          }
          if (!data) continue
          let payload = {}
          try { payload = JSON.parse(data) } catch { continue }
          if (event === 'delta') handlers.onDelta?.(payload.text || '')
          else if (event === 'done') handlers.onDone?.(payload)
          else if (event === 'error') handlers.onError?.(payload.message || '未知错误')
          else if (event === 'start') handlers.onStart?.(payload)
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') handlers.onError?.(String(e.message || e))
    }
  }
  run()
  return { abort: () => controller.abort() }
}
