/**
 * 职责：前端唯一 HTTP / SSE 出口。
 * - axios 实例 + 统一响应包 `{code, message, data}` 解包：code !== 0 抛 ApiError；
 * - 任何请求失败都写入全局 errorState（由 App.vue 顶部错误条渲染），不使用 alert；
 * - 覆盖 §12 全部接口 + SSE 订阅（EventSource + Last-Event-ID 重连 + 重连后 /stages 全量快照兜底）。
 *
 * 字段名严格照 §12 契约，不在前端做字段重命名或默认值填充（payload 假设见 README/交付说明）。
 */
import axios from 'axios'
import { reactive } from 'vue'

/** 后端基址：dev 下为空串，由 vite proxy 把 /api、/var 转发到 :8000 */
export const API_BASE = import.meta.env?.VITE_API_BASE || ''

/** 全局错误条状态：任何接口失败都会写这里，App.vue 顶部渲染 */
export const errorState = reactive({ message: '', detail: '', at: 0 })

/**
 * 当前任务上下文：切任务时由 App 写入。
 * 用途：丢弃「上一个任务」的迟到响应错误，避免错误条串到新任务上。
 */
let activeTaskId = ''

export function setActiveTask(taskId) {
  activeTaskId = String(taskId || '')
}

function isStaleRequest(url) {
  if (!activeTaskId) return false
  const m = /\/api\/drama\/tasks\/([^/]+)\//.exec(url || '')
  if (!m) return false
  return decodeURIComponent(m[1]) !== activeTaskId
}

export function setError(message, detail = '') {
  errorState.message = message || '请求失败'
  errorState.detail = detail || ''
  errorState.at = Date.now()
}

export function clearError() {
  errorState.message = ''
  errorState.detail = ''
  errorState.at = 0
}

/** 业务/网络错误统一类型：code 为响应包 code，status 为 HTTP 状态码 */
export class ApiError extends Error {
  constructor(message, { code = -1, status = 0, url = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.url = url
  }
}

const http = axios.create({
  baseURL: API_BASE,
  timeout: 20000,
  headers: { 'Content-Type': 'application/json' },
})

function toApiError(err, url) {
  if (axios.isAxiosError?.(err)) {
    const status = err.response?.status || 0
    const body = err.response?.data
    if (body && typeof body === 'object' && body.message) {
      return new ApiError(body.message, { code: body.code ?? status, status, url })
    }
    if (err.code === 'ECONNABORTED') {
      return new ApiError(`请求超时：${url}（后端可能未启动或响应过慢）`, { status, url })
    }
    if (!err.response) {
      return new ApiError(
        `无法连接后端 ${API_BASE || 'http://127.0.0.1:8000'}：${err.message}。开发环境请确认 FastAPI 已监听 :8000`,
        { status: 0, url },
      )
    }
    return new ApiError(`HTTP ${status}：${url}`, { status, url })
  }
  return new ApiError(err?.message || String(err), { url })
}

/** 统一请求：解包 data 字段，任何异常转 ApiError 并落全局错误条 */
async function request(method, url, { params, data, silent = false } = {}) {
  try {
    const res = await http.request({ method, url, params, data })
    const body = res.data
    if (!body || typeof body !== 'object' || !('code' in body)) {
      throw new ApiError(`响应缺少统一响应包 {code, message, data}：${url}`, {
        code: -1,
        status: res.status,
        url,
      })
    }
    if (body.code !== 0) {
      throw new ApiError(body.message || `接口返回 code=${body.code}`, {
        code: body.code,
        status: res.status,
        url,
      })
    }
    return body.data
  } catch (err) {
    const e = err instanceof ApiError ? err : toApiError(err, url)
    // 迟到错误（属于已被切走的任务）不弹错误条，避免串台
    if (!silent && !isStaleRequest(url)) setError(e.message, e.url ? `接口：${e.url}` : '')
    throw e
  }
}

// ── §12 接口 ────────────────────────────────────────────────

/** POST /api/drama/tasks → {task_id} */
export function createTask(payload) {
  return request('post', '/api/drama/tasks', { data: payload })
}

/** GET /api/drama/tasks → {tasks:[{task_id, topic, status, created_at, revision}]} */
export function listTasks() {
  return request('get', '/api/drama/tasks')
}

/** GET /api/drama/tasks/{id}/stages → {task_id, status, revision, stages[], counts, rework_count} */
export function getStages(taskId) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/stages`)
}

/** GET /api/drama/tasks/{id}/constraints → {constraints[], report} */
export function getConstraints(taskId) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/constraints`)
}

/** GET /api/drama/tasks/{id}/segments/{slot} → {slot_key, summary, claims[], gaps[]} */
export function getSegment(taskId, slotKey) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/segments/${encodeURIComponent(slotKey)}`)
}

/** GET /api/drama/tasks/{id}/shots → {shots[], packs[]} */
export function getShots(taskId) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/shots`)
}

/** GET /api/drama/tasks/{id}/assets → {assets[], spent_cny, reserved_cny, cap_cny, attempt_hist, pending_review} */
export function getAssets(taskId) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/assets`)
}

/** GET /api/drama/tasks/{id}/pending-human → {items[]} */
export function getPendingHuman(taskId) {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/pending-human`)
}

/** POST /api/drama/tasks/{id}/resolve {action, target_ids[], payload{}} */
export function resolveTask(taskId, action, targetIds = [], payload = {}) {
  return request('post', `/api/drama/tasks/${encodeURIComponent(taskId)}/resolve`, {
    data: { action, target_ids: targetIds, payload },
  })
}

/** GET /api/drama/tasks/{id}/package?as=md|json → {kind, content} */
export function getPackage(taskId, as = 'md') {
  return request('get', `/api/drama/tasks/${encodeURIComponent(taskId)}/package`, { params: { as } })
}

// ── SSE（§12 / §3.1）─────────────────────────────────────────

/** 八类事件名：后端用 `event:` 字段逐条点名，前端逐个注册监听 */
export const EVENT_TYPES = [
  'STAGE_UPDATED',
  'CONSTRAINT_READY',
  'PROGRESS_MESSAGE',
  'COST_QUOTE_READY',
  'ASSET_REVIEW_REQUIRED',
  'NEEDS_HUMAN',
  'BUDGET_EXCEEDED',
  'TASK_FINISHED',
]

/**
 * 订阅任务事件流。
 * - 断线自动重连：EventSource 自动重连时会自带 Last-Event-ID 头；若浏览器放弃重连
 *   （readyState === CLOSED），本函数按退避手动重建连接，并额外用 ?last_event_id= 兜底；
 * - 重连成功后先拉一次 GET /stages 全量快照（§3.1 明确要求），以 SNAPSHOT 事件回调给页面，
 *   避免"断线期间丢事件 → 泳道状态永久不一致"。
 *
 * @param {string} taskId
 * @param {{onEvent?:Function, onStatus?:Function}} handlers
 * @returns {() => void} 取消订阅
 */
export function subscribe(taskId, { onEvent, onStatus } = {}) {
  if (!taskId) throw new Error('subscribe 需要 task_id')

  const base = `${API_BASE}/api/drama/tasks/${encodeURIComponent(taskId)}/stream`
  let es = null
  let closed = false
  let attempt = 0
  let openedOnce = false
  let retryTimer = null
  let snapshotBusy = false
  let lastEventId = ''
  let snapshotPending = false

  const emitStatus = (state, detail = '') => {
    try {
      onStatus?.({ state, detail })
    } catch (e) {
      console.warn('[sse] onStatus 回调异常', e)
    }
  }

  const keepLastId = (id) => {
    if (id) lastEventId = id
  }

  async function pullSnapshot(reason) {
    if (snapshotBusy) {
      snapshotPending = true
      return
    }
    snapshotBusy = true
    try {
      // 兜底拉取失败只影响兜底，不阻塞重连
      const data = await getStages(taskId).catch(() => null)
      if (!closed && data) {
        onEvent?.({
          type: 'SNAPSHOT',
          eventName: 'SNAPSHOT',
          id: lastEventId,
          payload: data,
          reason,
          receivedAt: new Date().toISOString(),
        })
      } else if (!closed && !data) {
        emitStatus('snapshot_failed', '重连成功但全量快照拉取失败')
      }
    } finally {
      snapshotBusy = false
      if (snapshotPending) {
        snapshotPending = false
        pullSnapshot('queued')
      }
    }
  }

  function handle(eventName, ev) {
    keepLastId(ev.lastEventId)
    let payload = null
    try {
      payload = ev.data ? JSON.parse(ev.data) : null
    } catch (e) {
      payload = { raw: ev.data, parse_error: String(e?.message || e) }
    }
    const type = payload?.type || eventName
    try {
      onEvent?.({
        type,
        eventName,
        id: ev.lastEventId || lastEventId,
        payload,
        receivedAt: new Date().toISOString(),
      })
    } catch (e) {
      console.warn('[sse] onEvent 回调异常', e)
    }
  }

  function scheduleReconnect() {
    if (closed || retryTimer) return
    const delay = Math.min(1000 * 2 ** attempt, 15000)
    attempt += 1
    emitStatus('reconnecting', `${Math.round(delay / 1000)}s 后第 ${attempt} 次重连`)
    retryTimer = setTimeout(() => {
      retryTimer = null
      open()
    }, delay)
  }

  function open() {
    if (closed) return
    // Last-Event-ID 既走请求头（EventSource 自动带），也用 query 兜底（防代理丢头）
    const url = lastEventId ? `${base}?last_event_id=${encodeURIComponent(lastEventId)}` : base
    es = new EventSource(url)

    es.onopen = () => {
      attempt = 0
      emitStatus(openedOnce ? 'reconnected' : 'connected')
      if (openedOnce) pullSnapshot('reconnect')
      openedOnce = true
    }

    es.onerror = () => {
      if (closed) return
      emitStatus('reconnecting', '连接中断')
      if (es && es.readyState === EventSource.CLOSED) {
        try {
          es.close()
        } catch (e) {
          /* 忽略 */
        }
        es = null
        openedOnce = true
        scheduleReconnect()
      }
    }

    // 未命名的 message 事件也收（后端若漏写 event: 字段仍可用）
    es.onmessage = (ev) => handle('MESSAGE', ev)
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, (ev) => handle(type, ev))
    }
  }

  open()

  return function unsubscribe() {
    closed = true
    if (retryTimer) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    if (es) {
      try {
        es.close()
      } catch (e) {
        /* 忽略 */
      }
      es = null
    }
    emitStatus('closed')
  }
}

// ── 展示用字典与格式化（前后端枚举见 §5.1，避免各页面重复硬编码）──

export const STAGE_STATUS_META = {
  PENDING: { text: '待运行', cls: 'st-pending' },
  RUNNING: { text: '运行中', cls: 'st-running' },
  PASSED: { text: '通过', cls: 'st-passed' },
  FAILED: { text: '失败', cls: 'st-failed' },
  NEEDS_HUMAN: { text: '待人工', cls: 'st-human' },
  STALE: { text: '已失效重跑', cls: 'st-stale' },
}

export const ASSET_STATUS_META = {
  PENDING: { text: '排队中', cls: 'st-pending' },
  SUBMITTED: { text: '已提交', cls: 'st-running' },
  GENERATING: { text: '生成中', cls: 'st-running' },
  REVIEW: { text: '待审核', cls: 'st-human' },
  PASSED: { text: '已通过', cls: 'st-passed' },
  REJECTED: { text: '已驳回', cls: 'st-failed' },
  ORPHANED: { text: '已孤立', cls: 'st-stale' },
  BUDGET_HOLD: { text: '预算挂起', cls: 'st-failed' },
}

export const CLOSING_STATE_TEXT = {
  resolved: '收束',
  cliffhanger_line: '台词悬念',
  unresolved_action: '动作未决',
  reveal_tease: '预告钩子',
}

/** 金额统一 3 位小数 + ¥ 前缀（规格要求） */
export function formatCny(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '¥—'
  return `¥${n.toFixed(3)}`
}

/** ISO 时间 → 本地 HH:MM:SS */
export function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

/** ISO 时间 → 本地 YYYY-MM-DD HH:MM:SS */
export function formatDateTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}
