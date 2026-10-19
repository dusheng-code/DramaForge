<script setup>
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ASSET_STATUS_META, formatCny, getAssets, resolveTask } from '../api'

const ctx = inject('drama')

const data = ref(null)
const loading = ref(false)
const loadError = ref('')
const notice = ref('')
const busyShot = ref('')
const selectedIndex = ref(0)
const hintOpen = ref(false)
const hintText = ref('')
const brokenVideo = ref(new Set())

const assets = computed(() => (Array.isArray(data.value?.assets) ? data.value.assets : []))
const videos = computed(() => assets.value.filter((a) => a.kind === 'video'))
const imageAssets = computed(() => assets.value.filter((a) => a.kind !== 'video'))
const selected = computed(() => videos.value[selectedIndex.value] || null)

const pendingReview = computed(() => Number(data.value?.pending_review || 0))
const spent = computed(() => Number(data.value?.spent_cny ?? 0))
const reserved = computed(() => Number(data.value?.reserved_cny ?? 0))
const cap = computed(() => Number(data.value?.cap_cny ?? 0))
const usedPct = computed(() => (cap.value > 0 ? Math.min(100, (spent.value / cap.value) * 100) : 0))
const reservedPct = computed(() =>
  cap.value > 0 ? Math.min(100 - usedPct.value, (reserved.value / cap.value) * 100) : 0,
)
const overBudget = computed(() => cap.value > 0 && spent.value >= cap.value)
const nearBudget = computed(() => cap.value > 0 && spent.value + reserved.value >= cap.value && !overBudget.value)

/** attempt 分布：{n: count} → 有序数组，用于画分布条 */
const attemptBars = computed(() => {
  const hist = data.value?.attempt_hist || {}
  const rows = Object.entries(hist)
    .map(([n, count]) => ({ n: Number(n), count: Number(count) || 0 }))
    .filter((r) => Number.isFinite(r.n))
    .sort((a, b) => a.n - b.n)
  const max = rows.reduce((m, r) => Math.max(m, r.count), 0)
  return rows.map((r) => ({ ...r, pct: max > 0 ? (r.count / max) * 100 : 0 }))
})

const statusOrder = ['REVIEW', 'PENDING', 'SUBMITTED', 'GENERATING', 'PASSED', 'REJECTED', 'BUDGET_HOLD', 'ORPHANED']
const videoStatusTally = computed(() => {
  const out = Object.fromEntries(statusOrder.map((s) => [s, 0]))
  for (const a of videos.value) if (a.status in out) out[a.status] += 1
  return out
})

function load() {
  if (!ctx.taskId) {
    data.value = null
    return
  }
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  getAssets(ctx.taskId)
    .then((res) => {
      data.value = res || null
      if (selectedIndex.value >= videos.value.length) selectedIndex.value = 0
    })
    .catch((e) => {
      loadError.value = e?.message || String(e)
      data.value = null
    })
    .finally(() => {
      loading.value = false
    })
}

onMounted(() => {
  load()
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
watch(() => [ctx.taskId, ctx.tick], load)

function statusMeta(status) {
  return ASSET_STATUS_META[status] || { text: status || '未知', cls: 'st-pending' }
}

function select(index) {
  selectedIndex.value = index
  hintOpen.value = false
}

function moveSelection(step) {
  if (!videos.value.length) return
  const next = selectedIndex.value + step
  if (next < 0 || next >= videos.value.length) return
  selectedIndex.value = next
  hintOpen.value = false
}

function videoBroken(shotId) {
  return brokenVideo.value.has(shotId)
}

function onVideoError(shotId) {
  const next = new Set(brokenVideo.value)
  next.add(shotId)
  brokenVideo.value = next
}

async function submitAction(action, payload, label) {
  const target = selected.value
  if (!target) {
    notice.value = '没有选中项：先用 ↑/↓ 或点击选中一条视频资产'
    return
  }
  if (busyShot.value) return
  busyShot.value = target.shot_id
  notice.value = ''
  try {
    await resolveTask(ctx.taskId, action, [target.shot_id], payload)
    notice.value = `${label}：${target.shot_id}`
    hintOpen.value = false
    hintText.value = ''
    ctx.tick += 1
    load()
  } catch (e) {
    notice.value = `${label}失败：${e?.message || e}`
  } finally {
    busyShot.value = ''
  }
}

/** 1 = 通过（PASSED）：契约 action 集合无 approve_video，视频通过用 pass_slot 语义 */
function passSelected() {
  submitAction('pass_slot', {}, '已通过')
}

/** 2 = 驳回并重抽（REJECTED → attempt+1） */
function retrySelected() {
  submitAction('retry_shot', {}, '已驳回重抽')
}

/** 3 = 改提示词后重抽 */
function openHint() {
  if (!selected.value) {
    notice.value = '没有选中项：先选中一条视频资产再按 3'
    return
  }
  hintOpen.value = true
}

function submitHint() {
  const hint = hintText.value.trim()
  if (!hint) {
    notice.value = '请填写 prompt_hint（本次重抽要改什么）'
    return
  }
  submitAction('retry_shot', { prompt_hint: hint }, '已改词重抽')
}

/** 键盘流仅在本页聚焦、且焦点不在输入框内时生效 */
function onKeydown(event) {
  const target = event.target
  const tag = String(target?.tagName || '').toUpperCase()
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || target?.isContentEditable) return

  if (event.key === '1') {
    event.preventDefault()
    passSelected()
  } else if (event.key === '2') {
    event.preventDefault()
    retrySelected()
  } else if (event.key === '3') {
    event.preventDefault()
    openHint()
  } else if (event.key === 'Escape') {
    hintOpen.value = false
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveSelection(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveSelection(-1)
  }
}
</script>

<template>
  <section class="view assets-view">
    <div class="view-head">
      <div>
        <h1>资产审核</h1>
        <p class="muted">
          视频逐条审核：<b>1</b> 通过 · <b>2</b> 驳回重抽 · <b>3</b> 改提示词后重抽（弹输入框写 prompt_hint）。键盘流仅在本页且不在输入框时生效。
        </p>
      </div>
      <div class="head-meta">
        <span class="pill">视频 {{ videos.length }}</span>
        <span class="pill" :class="{ 'pill-warn': pendingReview > 0 }">待审核 {{ pendingReview }}</span>
        <button class="btn btn-ghost" type="button" :disabled="loading || !ctx.taskId" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="!ctx.taskId" class="empty">
      <div class="empty-title">尚未选择任务</div>
      <div class="empty-hint">在顶栏选择或输入 task_id 后，这里会列出视频资产与花费账本。</div>
    </div>

    <template v-else>
      <div v-if="notice" class="inline-ok">{{ notice }}</div>
      <div v-if="loadError" class="inline-error">
        资产拉取失败：{{ loadError }}（video 阶段未开始属正常；报价未批准前不会花钱）
      </div>

      <!-- 花费 / 预算 -->
      <div class="card budget-card">
        <div class="card-title-row">
          <h2>已花费 / 预算</h2>
          <span class="muted small">
            spent {{ formatCny(spent) }} / cap {{ cap > 0 ? formatCny(cap) : '未设置' }} · 已预留
            {{ formatCny(reserved) }}
          </span>
        </div>
        <div class="budget">
          <div class="budget-bar" :class="{ over: overBudget, near: nearBudget }">
            <div class="budget-used" :style="{ width: usedPct + '%' }"></div>
            <div class="budget-reserved" :style="{ width: reservedPct + '%' }"></div>
          </div>
          <div class="budget-meta">
            <span>spent <b>{{ formatCny(spent) }}</b> / cap <b>{{ cap > 0 ? formatCny(cap) : '未设置' }}</b></span>
            <span v-if="overBudget" class="budget-alert">已触顶：BUDGET_HOLD，需 extend_budget 才能继续</span>
            <span v-else-if="nearBudget" class="budget-warn">余量不足：spent + reserved 已达 cap</span>
            <span v-else class="muted small">剩余 {{ formatCny(Math.max(0, cap - spent)) }}</span>
          </div>
        </div>

        <div class="attempt-panel">
          <div class="attempt-title">attempt 分布（重抽次数 → 镜头数）</div>
          <div v-if="!attemptBars.length" class="muted small">暂无 attempt 数据</div>
          <div v-else class="attempt-bars">
            <div v-for="row in attemptBars" :key="row.n" class="attempt-row">
              <span class="attempt-n">attempt {{ row.n }}</span>
              <span class="attempt-bar"><i :style="{ width: row.pct + '%' }"></i></span>
              <span class="attempt-count">{{ row.count }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 视频列表 -->
      <div class="card video-card">
        <div class="card-title-row">
          <h2>视频资产</h2>
          <div class="kb-legend">
            <span class="kb">1</span> 通过
            <span class="kb">2</span> 驳回重抽
            <span class="kb">3</span> 改词重抽
            <span class="kb">↑ ↓</span> 切换选中
          </div>
        </div>

        <div v-if="!videos.length" class="empty inner">
          <div class="empty-title">还没有视频资产</div>
          <div class="empty-hint">
            视频在 keyframe PASSED 且报价已批准后才会提交；先去「待决议」页处理报价闸。
          </div>
        </div>

        <div v-else class="video-list">
          <article
            v-for="(asset, index) in videos"
            :key="asset.shot_id + '-' + asset.revision + '-' + index"
            class="video-item"
            :class="{ selected: index === selectedIndex }"
            @click="select(index)"
          >
            <div class="video-thumb">
              <video
                v-if="asset.file_url && !videoBroken(asset.shot_id)"
                :src="asset.file_url"
                controls
                preload="metadata"
                @error="onVideoError(asset.shot_id)"
              ></video>
              <div v-else class="thumb-empty large">
                视频不可用
                <span class="muted small">{{ asset.file_url || 'file_url 为空' }}</span>
              </div>
            </div>

            <div class="video-meta">
              <div class="video-head">
                <code class="shot-id">{{ asset.shot_id }}</code>
                <span class="badge" :class="statusMeta(asset.status).cls">{{ statusMeta(asset.status).text }}</span>
                <span v-if="asset.tier" class="tier" :class="'tier-' + asset.tier">{{ asset.tier }}</span>
                <span class="muted small">第 {{ asset.episode ?? '—' }} 集</span>
              </div>

              <dl class="kv compact">
                <dt>attempt</dt>
                <dd>{{ asset.attempt ?? 0 }}</dd>
                <dt>revision</dt>
                <dd>{{ asset.revision ?? 0 }}</dd>
                <dt>花费</dt>
                <dd>{{ formatCny(asset.cost_cny) }}</dd>
                <dt>预估</dt>
                <dd>{{ formatCny(asset.est_cost_cny) }}</dd>
                <dt>文件</dt>
                <dd class="mono small ellipsis" :title="asset.file_path || ''">{{ asset.file_path || '—' }}</dd>
              </dl>

              <div v-if="asset.reject_reason" class="inline-error small">
                驳回原因：{{ asset.reject_reason }}
              </div>

              <div class="row-actions">
                <button
                  class="btn btn-primary"
                  type="button"
                  :disabled="busyShot === asset.shot_id"
                  @click.stop="select(index); passSelected()"
                >
                  1 · 通过
                </button>
                <button
                  class="btn btn-danger"
                  type="button"
                  :disabled="busyShot === asset.shot_id"
                  @click.stop="select(index); retrySelected()"
                >
                  2 · 驳回重抽
                </button>
                <button class="btn btn-ghost" type="button" @click.stop="select(index); openHint()">
                  3 · 改词重抽
                </button>
              </div>

              <div v-if="index === selectedIndex && hintOpen" class="hint-editor">
                <textarea
                  v-model="hintText"
                  class="input textarea"
                  rows="2"
                  placeholder="prompt_hint：例如「动作更克制、镜头更近、台词放慢」"
                ></textarea>
                <div class="hint-actions">
                  <button class="btn btn-primary" type="button" :disabled="!!busyShot" @click.stop="submitHint">
                    确认重抽
                  </button>
                  <button class="btn btn-ghost" type="button" @click.stop="hintOpen = false">取消</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>

      <!-- 图像资产概览 -->
      <div class="card image-card">
        <div class="card-title-row">
          <h2>图像资产（定妆图 / 首帧）</h2>
          <span class="muted small">{{ imageAssets.length }} 条</span>
        </div>
        <div v-if="!imageAssets.length" class="empty inner">
          <div class="empty-title">还没有图像资产</div>
          <div class="empty-hint">anchor_image / keyframe 阶段产出后会在这里按状态汇总。</div>
        </div>
        <div v-else class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>shot_id</th>
                <th>kind</th>
                <th>revision</th>
                <th>status</th>
                <th>attempt</th>
                <th class="num">花费</th>
                <th>file</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(a, i) in imageAssets" :key="a.shot_id + '-' + a.kind + '-' + i">
                <td class="mono">{{ a.shot_id }}</td>
                <td>{{ a.kind }}</td>
                <td>{{ a.revision ?? 0 }}</td>
                <td>
                  <span class="badge small" :class="statusMeta(a.status).cls">{{ statusMeta(a.status).text }}</span>
                </td>
                <td>{{ a.attempt ?? 0 }}</td>
                <td class="num">{{ formatCny(a.cost_cny) }}</td>
                <td class="mono small ellipsis" :title="a.file_path || ''">{{ a.file_path || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="video-status-strip">
        <span v-for="s in statusOrder" :key="s" class="status-chip" :class="statusMeta(s).cls">
          {{ statusMeta(s).text }} <b>{{ videoStatusTally[s] }}</b>
        </span>
      </div>
    </template>
  </section>
</template>
