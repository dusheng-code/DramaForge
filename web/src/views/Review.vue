<script setup>
import { computed, inject, onMounted, ref, watch } from 'vue'
import {
  formatCny,
  formatTime,
  getAssets,
  getPendingHuman,
  resolveTask,
} from '../api'

const ctx = inject('drama')

const pending = ref([])
const pendingError = ref('')
const assets = ref(null)
const assetsError = ref('')
const loading = ref(false)
const busyKey = ref('')
const notice = ref('')
const extraCny = ref(10)

const KIND_LABEL = {
  quote: '报价闸',
  gate: '校验闸门',
  asset: '资产审核',
  budget: '预算',
  compliance: '合规存疑',
}

const ACTION_LABEL = {
  approve_quote: '批准报价（approve_quote）',
  reject_quote: '驳回报价（reject_quote）',
  extend_budget: '追加预算（extend_budget）',
  pass_slot: '通过（pass_slot）',
  retry_shot: '重抽（retry_shot）',
  skip_shot: '跳过（skip_shot）',
  veto_keyframe: '否决首帧（veto_keyframe）',
  approve_keyframe: '批准首帧（approve_keyframe）',
}

/** 后端没给 actions 时的兜底动作集合（按 pending-human 的 kind） */
const KIND_ACTIONS = {
  quote: ['approve_quote', 'reject_quote', 'extend_budget'],
  gate: ['pass_slot', 'retry_shot'],
  asset: ['pass_slot', 'retry_shot', 'skip_shot'],
  budget: ['extend_budget'],
  compliance: ['pass_slot', 'veto_keyframe', 'approve_keyframe'],
}

const quoteItems = computed(() => pending.value.filter((i) => i.kind === 'quote'))
const otherItems = computed(() => pending.value.filter((i) => i.kind !== 'quote'))

/** 预算账本：优先用 /assets 的 spent/reserved/cap */
const budget = computed(() => {
  const a = assets.value || {}
  const spent = Number(a.spent_cny ?? 0)
  const reserved = Number(a.reserved_cny ?? 0)
  const cap = Number(a.cap_cny ?? 0)
  const usedPct = cap > 0 ? Math.min(100, (spent / cap) * 100) : 0
  const reservedPct = cap > 0 ? Math.min(100 - usedPct, (reserved / cap) * 100) : 0
  return {
    spent,
    reserved,
    cap,
    usedPct,
    reservedPct,
    over: cap > 0 && spent >= cap,
    near: cap > 0 && spent + reserved >= cap && spent < cap,
  }
})

const quoteTotal = computed(() => {
  let sum = 0
  let hasTotal = false
  for (const item of quoteItems.value) {
    const p = item.payload || {}
    if (Number.isFinite(Number(p.est_total_cny))) {
      sum += Number(p.est_total_cny)
      hasTotal = true
      continue
    }
    if (Array.isArray(p.items)) {
      for (const row of p.items) {
        const v = Number(row?.est_cost_cny)
        if (Number.isFinite(v)) {
          sum += v
          hasTotal = true
        }
      }
    }
  }
  return hasTotal ? sum : null
})

function actionsOf(item) {
  const list = Array.isArray(item?.actions) && item.actions.length ? item.actions : KIND_ACTIONS[item?.kind] || []
  return list.filter((a) => a in ACTION_LABEL)
}

function isQuoteItem(item) {
  return item?.kind === 'quote' && Array.isArray(item?.payload?.items) && item.payload.items.length > 0
}

function quoteRows(item) {
  return Array.isArray(item?.payload?.items) ? item.payload.items : []
}

function itemEstTotal(item) {
  const p = item?.payload || {}
  if (Number.isFinite(Number(p.est_total_cny))) return Number(p.est_total_cny)
  let sum = 0
  for (const row of quoteRows(item)) sum += Number(row?.est_cost_cny || 0)
  return sum
}

function targetIdsOf(item) {
  const p = item?.payload || {}
  if (Array.isArray(p.target_ids) && p.target_ids.length) return p.target_ids
  return item?.target_id ? [item.target_id] : []
}

async function loadAll() {
  if (!ctx.taskId) {
    pending.value = []
    assets.value = null
    return
  }
  if (loading.value) return
  loading.value = true
  pendingError.value = ''
  assetsError.value = ''
  try {
    const data = await getPendingHuman(ctx.taskId)
    pending.value = Array.isArray(data?.items) ? data.items : []
  } catch (e) {
    pendingError.value = e?.message || String(e)
    pending.value = []
  }
  try {
    assets.value = await getAssets(ctx.taskId)
  } catch (e) {
    assetsError.value = e?.message || String(e)
    assets.value = null
  }
  loading.value = false
}

onMounted(loadAll)
watch(() => [ctx.taskId, ctx.tick], loadAll)

function busy(action, ids) {
  return busyKey.value === `${action}:${ids.join(',')}`
}

async function act(action, items, extraPayload = {}) {
  const list = Array.isArray(items) ? items : [items]
  const ids = [...new Set(list.flatMap((i) => targetIdsOf(i)))]
  const key = `${action}:${ids.join(',')}`
  if (busyKey.value) return
  busyKey.value = key
  notice.value = ''
  try {
    let payload = { ...extraPayload }
    if (action === 'extend_budget') {
      const amount = Number(extraCny.value)
      if (!Number.isFinite(amount) || amount <= 0) {
        notice.value = '追加预算金额必须为正数（元）'
        return
      }
      payload = { ...payload, extra_cny: Number(amount.toFixed(3)) }
    }
    await resolveTask(ctx.taskId, action, ids, payload)
    notice.value = `已提交决议：${ACTION_LABEL[action] || action} · 目标 ${ids.join(', ') || '—'}`
    ctx.tick += 1
    await loadAll()
  } catch (e) {
    // api.js 已写顶部错误条
    notice.value = ''
  } finally {
    busyKey.value = ''
  }
}

function prettyPayload(item) {
  try {
    return JSON.stringify(item?.payload ?? {}, null, 2)
  } catch (e) {
    return String(item?.payload)
  }
}
</script>

<template>
  <section class="view review-view">
    <div class="view-head">
      <div>
        <h1>待决议</h1>
        <p class="muted">
          人工闸口统一收口：报价批准、gate 存疑放行、图筛/视频批审、预算追加。决议走 POST /resolve，不被前端缓存。
        </p>
      </div>
      <div class="head-meta">
        <span class="pill">待决议 {{ pending.length }}</span>
        <button class="btn btn-ghost" type="button" :disabled="loading || !ctx.taskId" @click="loadAll">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="!ctx.taskId" class="empty">
      <div class="empty-title">尚未选择任务</div>
      <div class="empty-hint">在顶栏选择或输入 task_id 后，这里会列出所有待人工决议项。</div>
    </div>

    <template v-else>
      <div v-if="notice" class="inline-ok">{{ notice }}</div>
      <div v-if="pendingError" class="inline-error">待决议列表拉取失败：{{ pendingError }}</div>

      <!-- 预算条 -->
      <div class="card budget-card">
        <div class="card-title-row">
          <h2>预算</h2>
          <span class="muted small">
            已花费 {{ formatCny(budget.spent) }} / 上限
            {{ budget.cap > 0 ? formatCny(budget.cap) : '未设置' }}
            · 已预留 {{ formatCny(budget.reserved) }}
          </span>
        </div>
        <div v-if="assetsError" class="inline-error">预算账本拉取失败：{{ assetsError }}</div>
        <div v-else class="budget">
          <div class="budget-bar" :class="{ over: budget.over, near: budget.near }">
            <div class="budget-used" :style="{ width: budget.usedPct + '%' }"></div>
            <div class="budget-reserved" :style="{ width: budget.reservedPct + '%' }"></div>
          </div>
          <div class="budget-meta">
            <span>
              spent <b>{{ formatCny(budget.spent) }}</b> / cap
              <b>{{ budget.cap > 0 ? formatCny(budget.cap) : '未设置' }}</b>
            </span>
            <span v-if="budget.over" class="budget-alert">已触顶：资产泵停止提交（BUDGET_HOLD）</span>
            <span v-else-if="budget.near" class="budget-warn">余量不足：spent + reserved 已达上限</span>
            <span v-else class="muted small">剩余可用 {{ formatCny(Math.max(0, budget.cap - budget.spent)) }}</span>
          </div>
        </div>
      </div>

      <!-- 报价明细 -->
      <div class="card quote-card">
        <div class="card-title-row">
          <h2>报价明细（只算账不花钱）</h2>
          <div class="quote-head-actions">
            <span v-if="quoteTotal !== null" class="pill">预估合计 {{ formatCny(quoteTotal) }}</span>
            <label class="inline-field">
              追加金额
              <input v-model="extraCny" class="input tiny" type="number" min="0.001" step="0.001" />
              元
            </label>
            <button
              v-if="quoteItems.length"
              class="btn btn-primary"
              type="button"
              :disabled="!!busyKey"
              @click="act('approve_quote', quoteItems)"
            >
              {{ busyKey ? '处理中…' : '一键批准报价' }}
            </button>
            <button
              v-if="quoteItems.length"
              class="btn btn-ghost"
              type="button"
              :disabled="!!busyKey"
              @click="act('reject_quote', quoteItems)"
            >
              一键驳回报价
            </button>
            <button
              v-if="quoteItems.length"
              class="btn btn-ghost"
              type="button"
              :disabled="!!busyKey"
              @click="act('extend_budget', quoteItems)"
            >
              追加预算
            </button>
          </div>
        </div>

        <div v-if="!quoteItems.length" class="empty inner">
          <div class="empty-title">没有待批准的报价</div>
          <div class="empty-hint">报价闸只会出现在 gate×5 全部 PASSED/已决议之后；等待 COST_QUOTE_READY 事件。</div>
        </div>

        <div v-else class="quote-blocks">
          <div v-for="item in quoteItems" :key="item.target_id" class="quote-block">
            <div class="quote-block-head">
              <div>
                <span class="badge kind-quote">{{ KIND_LABEL[item.kind] || item.kind }}</span>
                <b>{{ item.title || '报价单' }}</b>
                <span v-if="item.slot_key" class="muted small">slot {{ item.slot_key }}</span>
              </div>
              <div class="quote-block-total">
                合计 {{ formatCny(itemEstTotal(item)) }}
                <span class="muted small">target {{ item.target_id }}</span>
              </div>
            </div>
            <p v-if="item.detail" class="muted detail-text">{{ item.detail }}</p>

            <div v-if="isQuoteItem(item)" class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>shot_id</th>
                    <th>kind</th>
                    <th>tier</th>
                    <th>model</th>
                    <th>resolution</th>
                    <th class="num">时长(s)</th>
                    <th class="num">单价</th>
                    <th class="num">预计次数</th>
                    <th class="num">预估花费</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, idx) in quoteRows(item)" :key="row.shot_id + '-' + idx">
                    <td class="mono">{{ row.shot_id ?? '—' }}</td>
                    <td>{{ row.kind ?? '—' }}</td>
                    <td>
                      <span class="tier" :class="'tier-' + (row.tier || 'fast')">{{ row.tier ?? '—' }}</span>
                    </td>
                    <td class="mono">{{ row.model ?? '—' }}</td>
                    <td>{{ row.resolution ?? '—' }}</td>
                    <td class="num">{{ row.duration_sec ?? '—' }}</td>
                    <td class="num">{{ formatCny(row.unit_price_cny) }}</td>
                    <td class="num">{{ row.expected_attempts ?? '—' }}</td>
                    <td class="num">{{ formatCny(row.est_cost_cny) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="muted small">该报价项未提供 payload.items 明细，仅显示总额。</div>

            <details class="payload-details">
              <summary>payload 原文（对接排查用）</summary>
              <pre class="pre">{{ prettyPayload(item) }}</pre>
            </details>

            <div class="row-actions">
              <button
                v-for="action in actionsOf(item)"
                :key="action"
                class="btn"
                :class="action.startsWith('reject') || action.startsWith('veto') ? 'btn-danger' : 'btn-primary'"
                type="button"
                :disabled="!!busyKey"
                @click="act(action, [item])"
              >
                {{ ACTION_LABEL[action] }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 其余待决议 -->
      <div class="card pending-card">
        <div class="card-title-row">
          <h2>其他待决议项</h2>
          <span class="muted small">{{ otherItems.length }} 项</span>
        </div>

        <div v-if="!otherItems.length" class="empty inner">
          <div class="empty-title">没有其他待决议项</div>
          <div class="empty-hint">校验闸门存疑、图筛、视频批审与预算触顶都会出现在这里。</div>
        </div>

        <div v-else class="pending-list">
          <article v-for="item in otherItems" :key="item.target_id" class="pending-item" :class="'kind-' + item.kind">
            <div class="pending-head">
              <span class="badge" :class="'kind-' + item.kind">{{ KIND_LABEL[item.kind] || item.kind }}</span>
              <b class="pending-title">{{ item.title || item.target_id }}</b>
              <span class="muted small mono">{{ item.target_id }}</span>
              <span v-if="item.slot_key" class="muted small">slot {{ item.slot_key }}</span>
            </div>
            <p v-if="item.detail" class="detail-text">{{ item.detail }}</p>
            <details class="payload-details">
              <summary>payload 原文</summary>
              <pre class="pre">{{ prettyPayload(item) }}</pre>
            </details>
            <div class="row-actions">
              <button
                v-for="action in actionsOf(item)"
                :key="action"
                class="btn"
                :class="action.startsWith('reject') || action.startsWith('veto') || action === 'skip_shot' ? 'btn-danger' : 'btn-primary'"
                type="button"
                :disabled="!!busyKey"
                @click="act(action, [item])"
              >
                {{ ACTION_LABEL[action] }}
              </button>
            </div>
          </article>
        </div>
      </div>

      <p class="muted small foot-note">
        最近刷新：{{ formatTime(new Date().toISOString()) }} · 顶栏「轮询 5s」开启后本页会自动重拉。
      </p>
    </template>
  </section>
</template>
