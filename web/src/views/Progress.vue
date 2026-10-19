<script setup>
import { computed, inject, nextTick, onMounted, ref, watch } from 'vue'
import { formatCny, formatTime, getStages, STAGE_STATUS_META } from '../api'

const ctx = inject('drama')

/** 14 阶段（§5.1 Stage），顺序与 §6.1 依赖表一致 */
const STAGES = [
  'reference',
  'constraint',
  'outline',
  'episodes',
  'characters',
  'blocks',
  'storyboard',
  'prompt_pack',
  'gate',
  'quote',
  'anchor_image',
  'keyframe',
  'video',
  'assemble',
]

const STAGE_LABELS = {
  reference: '参照卡检索',
  constraint: '约束推导',
  outline: '大纲',
  episodes: '分集剧本',
  characters: '人物设定',
  blocks: '剧本块',
  storyboard: '分镜',
  prompt_pack: '提示词包',
  gate: '校验闸门',
  quote: '报价闸',
  anchor_image: '定妆图',
  keyframe: '首帧',
  video: '视频',
  assemble: '装配',
}

/** gate 按 5 个 slot 各自成行（§6.1：gate 的 slot = 每个 slot） */
const SLOTS = ['premise', 'structure', 'emotion', 'platform_voice', 'risk']
const SLOT_LABELS = {
  premise: '前提',
  structure: '结构',
  emotion: '情绪',
  platform_voice: '平台语态',
  risk: '风险',
}

const STATUS_ORDER = ['PENDING', 'RUNNING', 'PASSED', 'FAILED', 'NEEDS_HUMAN', 'STALE']

const loading = ref(false)
const snapshot = ref(null)
const stages = ref([])
const loadError = ref('')

async function load() {
  if (!ctx.taskId) {
    snapshot.value = null
    stages.value = []
    return
  }
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const data = await getStages(ctx.taskId)
    snapshot.value = data || null
    stages.value = Array.isArray(data?.stages) ? data.stages : []
  } catch (e) {
    loadError.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => [ctx.taskId, ctx.tick], load)

function statusMeta(status) {
  return STAGE_STATUS_META[status] || { text: status || '未知', cls: 'st-pending' }
}

/** 同一 (stage, slot_key) 只保留 revision 最大的一行，其余视为历史 */
const latestRows = computed(() => {
  const map = new Map()
  for (const row of stages.value) {
    const key = `${row.stage}|${row.slot_key}`
    const prev = map.get(key)
    if (!prev || Number(row.revision || 0) >= Number(prev.revision || 0)) map.set(key, row)
  }
  return map
})

/** 14 条泳道；gate 展开为 5 个 slot 子行 */
const lanes = computed(() => {
  const map = latestRows.value
  return STAGES.map((stage, index) => {
    if (stage === 'gate') {
      const slotRows = SLOTS.map((slot) => {
        const row = map.get(`gate|${slot}`) || null
        return { slot, label: SLOT_LABELS[slot], row, status: row?.status || 'PENDING' }
      })
      return {
        stage,
        index: index + 1,
        label: STAGE_LABELS[stage],
        isGate: true,
        slots: slotRows,
        row: null,
        status: gateAggregate(slotRows),
      }
    }
    const row = map.get(`${stage}|*`) || null
    return {
      stage,
      index: index + 1,
      label: STAGE_LABELS[stage],
      isGate: false,
      slots: [],
      row,
      status: row?.status || 'PENDING',
    }
  })
})

/** gate 聚合：全部 PASSED 才算 PASSED，任一 STALE/待人工/失败优先暴露 */
function gateAggregate(slotRows) {
  const statuses = slotRows.map((s) => s.status)
  if (statuses.every((s) => s === 'PASSED')) return 'PASSED'
  if (statuses.includes('FAILED')) return 'FAILED'
  if (statuses.includes('NEEDS_HUMAN')) return 'NEEDS_HUMAN'
  if (statuses.includes('STALE')) return 'STALE'
  if (statuses.includes('RUNNING')) return 'RUNNING'
  return 'PENDING'
}

/** 六态计数：优先用后端 counts，缺失时按当前行现算 */
const tally = computed(() => {
  const out = Object.fromEntries(STATUS_ORDER.map((s) => [s, 0]))
  const src = snapshot.value?.counts || {}
  const hasSrc = STATUS_ORDER.some((s) => typeof src[s] === 'number')
  if (hasSrc) {
    for (const s of STATUS_ORDER) out[s] = Number(src[s] || 0)
    return out
  }
  for (const row of stages.value) {
    if (row.status in out) out[row.status] += 1
  }
  return out
})

const totalRows = computed(() => stages.value.length)
const passedLanes = computed(() => lanes.value.filter((l) => l.status === 'PASSED').length)

const maxRevision = computed(() => {
  let max = 0
  for (const row of stages.value) max = Math.max(max, Number(row.revision || 0))
  return max
})

const revision = computed(() =>
  typeof snapshot.value?.revision === 'number' ? snapshot.value.revision : maxRevision.value,
)
const reworkCount = computed(() => Number(snapshot.value?.rework_count || 0))
const taskStatus = computed(() => snapshot.value?.status || '—')

const EVENT_TONE = {
  STAGE_UPDATED: 'ev-stage',
  CONSTRAINT_READY: 'ev-constraint',
  PROGRESS_MESSAGE: 'ev-progress',
  COST_QUOTE_READY: 'ev-quote',
  ASSET_REVIEW_REQUIRED: 'ev-asset',
  NEEDS_HUMAN: 'ev-human',
  BUDGET_EXCEEDED: 'ev-budget',
  TASK_FINISHED: 'ev-done',
}

function eventSummary(ev) {
  const p = ev?.payload || {}
  const d = p.data || {}
  switch (ev?.type) {
    case 'STAGE_UPDATED': {
      const label = STAGE_LABELS[p.stage] || p.stage || '?'
      const slot = p.slot_key && p.slot_key !== '*' ? `/${SLOT_LABELS[p.slot_key] || p.slot_key}` : ''
      const st = statusMeta(d.status).text
      return `${label}${slot} → ${st}${d.fail_reason ? `：${d.fail_reason}` : ''}（rev ${p.revision ?? 0}）`
    }
    case 'CONSTRAINT_READY':
      return `约束装配完成，共 ${d.count ?? '?'} 条（rev ${p.revision ?? 0}）`
    case 'PROGRESS_MESSAGE':
      return `${STAGE_LABELS[p.stage] || p.stage || ''} ${d.message || ''}`.trim()
    case 'COST_QUOTE_READY':
      return `报价就绪，预估合计 ${formatCny(d.est_total_cny)}（rev ${p.revision ?? 0}）`
    case 'ASSET_REVIEW_REQUIRED':
      return `资产待审：${d.shot_id || '?'} · ${d.kind || '?'} · attempt ${d.attempt ?? 0} · ${formatCny(d.cost_cny)}`
    case 'NEEDS_HUMAN': {
      const label = STAGE_LABELS[p.stage] || p.stage || '?'
      return `${label} 挂人工：${d.reason || '待处理'}`
    }
    case 'BUDGET_EXCEEDED':
      return `预算触顶：已花 ${formatCny(d.spent_cny)} / 上限 ${formatCny(d.cap_cny)}${d.episode != null ? `（第 ${d.episode} 集）` : ''}`
    case 'TASK_FINISHED':
      return `任务结束：${d.status || '?'}${d.gaps ? ` · 缺口 ${d.gaps}` : ''}`
    default:
      return JSON.stringify(d).slice(0, 180)
  }
}

const logRef = ref(null)
let stickBottom = true

function onLogScroll() {
  const el = logRef.value
  if (!el) return
  stickBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

watch(
  () => ctx.events.length,
  async () => {
    if (!stickBottom) return
    await nextTick()
    const el = logRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

function scrollLogToBottom() {
  const el = logRef.value
  if (!el) return
  stickBottom = true
  el.scrollTop = el.scrollHeight
}
</script>

<template>
  <section class="view progress-view">
    <div class="view-head">
      <div>
        <h1>进度</h1>
        <p class="muted">
          阶段泳道共 14 段，gate 展开为 5 个 slot；徽标区分 PENDING / RUNNING / PASSED / FAILED / NEEDS_HUMAN / STALE 六态。
        </p>
      </div>
      <div class="head-meta">
        <span class="pill">任务状态 {{ taskStatus }}</span>
        <button class="btn btn-ghost" type="button" :disabled="loading || !ctx.taskId" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="!ctx.taskId" class="empty">
      <div class="empty-title">尚未选择任务</div>
      <div class="empty-hint">先在「提交任务」页创建任务，或在顶栏选择/输入 task_id。</div>
    </div>

    <template v-else>
      <div v-if="loadError" class="inline-error">
        阶段快照拉取失败：{{ loadError }}（SSE 仍会推送增量事件，可稍后重试）
      </div>

      <div class="stat-strip">
        <div class="stat-card">
          <div class="stat-label">阶段通过</div>
          <div class="stat-value">{{ passedLanes }}<em>/ {{ lanes.length }}</em></div>
        </div>
        <div class="stat-card">
          <div class="stat-label">当前 revision</div>
          <div class="stat-value">rev {{ revision }}</div>
        </div>
        <div class="stat-card" :class="{ 'stat-warn': reworkCount > 0 }">
          <div class="stat-label">返工次数</div>
          <div class="stat-value">{{ reworkCount }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">阶段行总数</div>
          <div class="stat-value">{{ totalRows }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">事件流</div>
          <div class="stat-value">{{ ctx.events.length }}</div>
        </div>
      </div>

      <div class="card queue-card">
        <div class="card-title-row">
          <h2>顶部队列统计</h2>
          <span class="muted small">来自 GET /stages 的 counts（缺省时按当前行现算）</span>
        </div>
        <div class="queue-stats">
          <div v-for="s in STATUS_ORDER" :key="s" class="queue-stat" :class="statusMeta(s).cls">
            <span class="queue-dot"></span>
            <span class="queue-name">{{ statusMeta(s).text }}</span>
            <span class="queue-count">{{ tally[s] }}</span>
          </div>
        </div>
      </div>

      <div class="card lanes-card">
        <div class="card-title-row">
          <h2>阶段泳道</h2>
          <div class="legend">
            <span v-for="s in STATUS_ORDER" :key="s" class="legend-item">
              <i class="badge" :class="statusMeta(s).cls">{{ statusMeta(s).text }}</i>
            </span>
            <span class="legend-item muted small">STALE = 上游 revision 变更后待重跑</span>
          </div>
        </div>

        <div v-if="!stages.length" class="empty inner">
          <div class="empty-title">还没有阶段快照</div>
          <div class="empty-hint">后端 /stages 返回空数组；任务可能刚创建，稍等或点「刷新」。</div>
        </div>

        <div v-else class="lanes">
          <article
            v-for="lane in lanes"
            :key="lane.stage"
            class="lane"
            :class="[statusMeta(lane.status).cls, { 'lane-gate': lane.isGate }]"
          >
            <header class="lane-head">
              <span class="lane-index">{{ lane.index }}</span>
              <div class="lane-title">
                <span class="lane-name">{{ lane.label }}</span>
                <code class="lane-key">{{ lane.stage }}</code>
              </div>
              <span class="badge" :class="statusMeta(lane.status).cls">{{ statusMeta(lane.status).text }}</span>
            </header>

            <div v-if="lane.isGate" class="lane-slots">
              <div v-for="slot in lane.slots" :key="slot.slot" class="slot-row">
                <div class="slot-main">
                  <span class="slot-label">{{ slot.label }}</span>
                  <code class="slot-key">{{ slot.slot }}</code>
                </div>
                <span class="badge small" :class="statusMeta(slot.status).cls">
                  {{ statusMeta(slot.status).text }}
                </span>
                <span class="slot-rev">rev {{ slot.row?.revision ?? 0 }}</span>
              </div>
            </div>

            <div v-else class="lane-body">
              <div class="lane-meta">
                <span>slot <code>{{ lane.row?.slot_key || '*' }}</code></span>
                <span>rev {{ lane.row?.revision ?? 0 }}</span>
                <span v-if="lane.row?.status === 'STALE'" class="stale-note">已失效重跑</span>
              </div>
              <div v-if="lane.row?.fail_reason" class="lane-fail" :title="lane.row.fail_reason">
                失败原因：{{ lane.row.fail_reason }}
              </div>
              <div v-if="lane.row?.output_path" class="lane-path" :title="lane.row.output_path">
                产物：{{ lane.row.output_path }}
              </div>
              <div v-if="!lane.row" class="muted small">该阶段尚未落库（PENDING）</div>
            </div>
          </article>
        </div>
      </div>

      <div class="card log-card">
        <div class="card-title-row">
          <h2>事件流</h2>
          <div class="log-actions">
            <span class="muted small">SSE 实时推送，最多保留 400 条</span>
            <button class="btn btn-ghost" type="button" @click="scrollLogToBottom">跳到最新</button>
          </div>
        </div>

        <div v-if="!ctx.events.length" class="empty inner">
          <div class="empty-title">还没有事件</div>
          <div class="empty-hint">SSE 未推送或订阅尚未建立；可查看顶栏 SSE 状态，或开启轮询兜底。</div>
        </div>

        <div v-else ref="logRef" class="log-list" @scroll="onLogScroll">
          <div v-for="(ev, i) in ctx.events" :key="ev.id || i + '-' + ev.type" class="log-row">
            <span class="log-time">{{ formatTime(ev.payload?.created_at || ev.receivedAt) }}</span>
            <span class="log-seq">#{{ ev.payload?.seq ?? '—' }}</span>
            <span class="log-type" :class="EVENT_TONE[ev.type] || 'ev-default'">{{ ev.type }}</span>
            <span class="log-text">{{ eventSummary(ev) }}</span>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>
