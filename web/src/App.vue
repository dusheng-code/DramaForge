<script setup>
import { computed, onBeforeUnmount, onMounted, provide, reactive, ref, watch } from 'vue'
import { clearError, errorState, formatDateTime, listTasks, setActiveTask, subscribe } from './api'
import Submit from './views/Submit.vue'
import Progress from './views/Progress.vue'
import Review from './views/Review.vue'
import Board from './views/Board.vue'
import Assets from './views/Assets.vue'

/** 导航配置：添加生动的现代图标与中文标签 */
const NAV = [
  { key: 'submit', label: '发起任务', icon: '✦' },
  { key: 'progress', label: '生产进度', icon: '⚡' },
  { key: 'review', label: '待办决议', icon: '⚖' },
  { key: 'board', label: '分镜画廊', icon: '◫' },
  { key: 'assets', label: '资产中心', icon: '◈' },
]

const current = ref('submit')
const taskInput = ref('')
const tasks = ref([])
const tasksLoading = ref(false)

/**
 * 全局上下文：由 App 统一持有 task_id / 轮询开关 / SSE 事件流，
 * 各页面 inject('drama') 后自行按需拉取接口数据。
 */
const ctx = reactive({
  taskId: '',
  polling: true,
  tick: 0, // 轮询或 SSE 触发的"该刷新了"信号
  events: [],
  sseState: 'idle',
  sseDetail: '',
})
provide('drama', ctx)

const SSE_TEXT = {
  idle: '未连接',
  connecting: '连接中',
  connected: '已连接',
  reconnecting: '重连中',
  reconnected: '已重连',
  closed: '已断开',
  snapshot_failed: '快照失败',
}

const sseText = computed(() => SSE_TEXT[ctx.sseState] || ctx.sseState)
const taskInfo = computed(() => tasks.value.find((t) => t.task_id === ctx.taskId) || null)

let unsubscribe = null
let pollTimer = null

function stopStream() {
  if (unsubscribe) {
    unsubscribe()
    unsubscribe = null
  }
}

function startStream() {
  stopStream()
  ctx.events = []
  ctx.sseState = 'idle'
  ctx.sseDetail = ''
  if (!ctx.taskId) return
  ctx.sseState = 'connecting'
  unsubscribe = subscribe(ctx.taskId, {
    onEvent: (ev) => {
      if (ev.type === 'SNAPSHOT') {
        // 重连后的全量快照：不进入事件流列表，只作为刷新信号
        ctx.tick += 1
        return
      }
      ctx.events.push(ev)
      if (ctx.events.length > 400) ctx.events.splice(0, ctx.events.length - 400)
      ctx.tick += 1
    },
    onStatus: (s) => {
      ctx.sseState = s.state
      ctx.sseDetail = s.detail || ''
    },
  })
}

async function refreshTasks() {
  tasksLoading.value = true
  try {
    const data = await listTasks()
    tasks.value = Array.isArray(data?.tasks) ? data.tasks : []
    if (!ctx.taskId && tasks.value.length) {
      ctx.taskId = tasks.value[0].task_id
      taskInput.value = ctx.taskId
    }
  } catch (e) {
    // 错误已由 api.js 写入顶部错误条
    tasks.value = []
  } finally {
    tasksLoading.value = false
  }
}

function applyTaskId(id) {
  const value = String(id || '').trim()
  if (!value) {
    ctx.taskId = ''
    return
  }
  ctx.taskId = value
  taskInput.value = value
  current.value = 'progress'
}

function onPickTask(event) {
  const value = event.target.value
  taskInput.value = value
  ctx.taskId = value
}

function onSubmitted(result) {
  const id = result?.taskId || ''
  if (id) applyTaskId(id)
  refreshTasks()
}

watch(
  () => ctx.taskId,
  (id, prev) => {
    if (id === prev) return
    // 切任务：清掉上一个任务的错误条，并让迟到失败不再污染新任务
    setActiveTask(id)
    clearError()
    startStream()
    ctx.tick += 1
  },
)

onMounted(() => {
  refreshTasks()
  // 轮询开关：开启时每 5s 触发一次刷新信号（SSE 也在跑，轮询只是兜底）
  pollTimer = setInterval(() => {
    if (ctx.polling && ctx.taskId && document.visibilityState === 'visible') ctx.tick += 1
  }, 5000)
})

onBeforeUnmount(() => {
  stopStream()
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">DF</span>
        <span class="brand-name">DramaForge</span>
        <span class="brand-sub">M10 控制台</span>
      </div>

      <nav class="nav">
        <button
          v-for="item in NAV"
          :key="item.key"
          class="nav-btn"
          :class="{ active: current === item.key }"
          type="button"
          @click="current = item.key"
        >
          <span style="margin-right: 5px; opacity: 0.85;">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </button>
      </nav>

      <div class="task-bar">
        <label class="task-label" for="task-select">当前任务</label>
        <select
          id="task-select"
          class="task-select"
          :value="ctx.taskId"
          @change="onPickTask"
        >
          <option value="">（未选择任务）</option>
          <option v-for="t in tasks" :key="t.task_id" :value="t.task_id">
            {{ t.task_id }} · {{ t.topic }}
          </option>
        </select>

        <input
          v-model="taskInput"
          class="task-input"
          type="text"
          placeholder="手动输入 task_id 后回车"
          @keyup.enter="applyTaskId(taskInput)"
        />
        <button class="btn btn-ghost" type="button" @click="applyTaskId(taskInput)">切换</button>
        <button class="btn btn-ghost" type="button" :disabled="tasksLoading" @click="refreshTasks">
          {{ tasksLoading ? '刷新中…' : '刷新列表' }}
        </button>
      </div>

      <div class="status-bar">
        <span class="task-id" :title="ctx.taskId || '未选择任务'">
          task_id：<b>{{ ctx.taskId || '—' }}</b>
        </span>
        <span v-if="taskInfo" class="pill">{{ taskInfo.status }}</span>
        <span v-if="taskInfo" class="muted small">
          创建 {{ formatDateTime(taskInfo.created_at) }} · rev {{ taskInfo.revision ?? 0 }}
        </span>
        <span class="sse-chip" :class="'sse-' + ctx.sseState" :title="ctx.sseDetail">
          SSE：{{ sseText }}
        </span>
        <label class="switch">
          <input v-model="ctx.polling" type="checkbox" />
          <span>轮询 5s</span>
        </label>
      </div>
    </header>

    <div v-if="errorState.message" class="error-bar" role="alert">
      <span class="error-icon">!</span>
      <div class="error-body">
        <div class="error-msg">{{ errorState.message }}</div>
        <div v-if="errorState.detail" class="error-detail">{{ errorState.detail }}</div>
      </div>
      <button class="error-close" type="button" title="关闭错误条" @click="clearError">×</button>
    </div>

    <main class="page">
      <Submit v-if="current === 'submit'" @submitted="onSubmitted" />
      <Progress v-else-if="current === 'progress'" />
      <Review v-else-if="current === 'review'" />
      <Board v-else-if="current === 'board'" />
      <Assets v-else-if="current === 'assets'" />
    </main>
  </div>
</template>
