<script setup>
import { computed, inject, reactive, ref } from 'vue'
import { createTask } from '../api'

const emit = defineEmits(['submitted'])
const ctx = inject('drama')

const PLATFORMS = ['抖音', '快手', '红果', '视频号', '番茄']
const DURATION_OPTIONS = [15, 20, 25, 30, 45, 60, 90, 120]

const PRESET_TOPICS = [
  '退婚后，我在急诊手术室救下首富外公，前夫全家悔疯了',
  '真假千金归位：满级玄学老祖下山整顿豪门全员恶人',
  '重回高三：我放弃当恋爱脑，带全家摆摊致富考上清华',
  '战神退役后回归故里，发现女儿竟住狗窝，十万将士即刻奔赴',
]

/** 表单：字段名与 POST /api/drama/tasks body 完全一致（§12） */
const form = reactive({
  topic: '',
  platform: '抖音',
  episode_count: 6,
  duration_target: 60,
  cost_cap_cny: 20,
  strict_mode: false,
  overridesText: '{}',
})

function pickPreset(topic) {
  form.topic = topic
}

const submitting = ref(false)
const formError = ref('')
const lastTaskId = ref('')

/** §4.1 流程：提交后依次推进的阶段，用于给用户交代"点了会发生什么" */
const FLOW = [
  { stage: 'reference', text: '参照卡检索 + 榜单统计 + 公域时效' },
  { stage: 'constraint', text: '推导 ConstraintSpec，无据项被丢弃' },
  { stage: 'script', text: 'outline → episodes → characters → blocks' },
  { stage: 'storyboard', text: '分镜 + 时长档位 + quality_tier 赋档' },
  { stage: 'prompt_pack', text: '五要素提示词纯字符串组装' },
  { stage: 'gate', text: '5 段位闸门 · 18 个确定性校验器' },
  { stage: 'quote', text: '报价闸：只算账不花钱，等人工批准' },
  { stage: 'assets', text: '定妆图 → 首帧 → 视频（受预算熔断约束）' },
  { stage: 'assemble', text: '装配 drama_package.md / asset_report.json' },
]

const topicLen = computed(() => form.topic.trim().length)
const costHint = computed(() => {
  const cap = Number(form.cost_cap_cny)
  if (!Number.isFinite(cap) || cap <= 0) return '预算上限必须为正数'
  return `预算上限 ${cap.toFixed(3)} 元；触顶后资产泵停止提交（BUDGET_HOLD）`
})

function parseOverrides() {
  const raw = String(form.overridesText || '').trim()
  if (!raw) return {}
  let obj
  try {
    obj = JSON.parse(raw)
  } catch (e) {
    throw new Error(`overrides 不是合法 JSON：${e.message}`)
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new Error('overrides 必须是 JSON 对象，例如 {"EPISODE_LIMIT": 1}')
  }
  return obj
}

function validate() {
  if (!form.topic.trim()) return '请填写题材 topic（一句话说清故事前提）'
  if (form.topic.trim().length > 512) return 'topic 超过 512 字上限'
  if (!form.platform) return '请选择平台'
  const eps = Number(form.episode_count)
  if (!Number.isInteger(eps) || eps < 1 || eps > 200) return '集数必须是 1–200 的整数'
  const dur = Number(form.duration_target)
  if (!Number.isFinite(dur) || dur <= 0) return '单集时长必须为正数（秒）'
  const cap = Number(form.cost_cap_cny)
  if (!Number.isFinite(cap) || cap <= 0) return '预算上限必须为正数（元）'
  if (!/^\d+(\.\d{1,3})?$/.test(String(form.cost_cap_cny))) return '预算上限最多 3 位小数'
  return ''
}

async function submit() {
  formError.value = ''
  const msg = validate()
  if (msg) {
    formError.value = msg
    return
  }
  let overrides = {}
  try {
    overrides = parseOverrides()
  } catch (e) {
    formError.value = e.message
    return
  }

  submitting.value = true
  try {
    const data = await createTask({
      topic: form.topic.trim(),
      platform: form.platform,
      episode_count: Number(form.episode_count),
      duration_target: Number(form.duration_target),
      cost_cap_cny: Number(form.cost_cap_cny),
      strict_mode: Boolean(form.strict_mode),
      overrides,
    })
    const taskId = data?.task_id || ''
    lastTaskId.value = taskId
    if (!taskId) {
      formError.value = '后端返回成功但缺少 task_id，无法进入进度页'
      return
    }
    emit('submitted', { taskId })
  } catch (e) {
    // api.js 已写顶部错误条，这里再给表单内一行就地提示
    formError.value = e?.message || '提交失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="view submit-view">
    <div class="view-head">
      <div>
        <h1>提交任务</h1>
        <p class="muted">
          文本链路（检索 → 约束 → 剧本 → 分镜 → 校验 → 报价）全程零花费；报价批准后才会进入图像/视频生成。
        </p>
      </div>
      <div class="head-meta">
        <span v-if="ctx.taskId" class="pill">当前任务 {{ ctx.taskId }}</span>
        <span v-else class="muted small">尚未选择任务</span>
      </div>
    </div>

    <div class="submit-grid">
      <form class="card form-card" @submit.prevent="submit">
        <div v-if="formError" class="inline-error">{{ formError }}</div>

        <label class="field">
          <span class="field-label">题材 topic <em>必填</em></span>
          <textarea
            v-model="form.topic"
            class="input textarea"
            rows="3"
            maxlength="512"
            placeholder="例：被退婚的县城女医生，靠一场急诊手术翻盘，打脸前夫一家"
          ></textarea>
          <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; align-items: center;">
            <span class="muted small" style="font-size: 11.5px;">快速预填：</span>
            <button
              v-for="(t, idx) in PRESET_TOPICS"
              :key="idx"
              type="button"
              class="btn btn-ghost"
              style="padding: 2px 8px; font-size: 11.5px; border-radius: 999px; background: #f1f5f9;"
              @click="pickPreset(t)"
            >
              {{ t.slice(0, 12) }}…
            </button>
          </div>
          <span class="field-hint" style="margin-top: 4px;">一句话说清主角、冲突与爽点；字数：{{ topicLen }}/512</span>
        </label>

        <div class="field-row">
          <label class="field">
            <span class="field-label">平台 platform</span>
            <select v-model="form.platform" class="input">
              <option v-for="p in PLATFORMS" :key="p" :value="p">{{ p }}</option>
            </select>
            <span class="field-hint">决定平台语态约束与榜单对照</span>
          </label>

          <label class="field">
            <span class="field-label">集数 episode_count</span>
            <input v-model="form.episode_count" class="input" type="number" min="1" max="200" step="1" />
            <span class="field-hint">1–200 集</span>
          </label>

          <label class="field">
            <span class="field-label">单集时长 duration_target（秒）</span>
            <select v-model.number="form.duration_target" class="input">
              <option v-for="d in DURATION_OPTIONS" :key="d" :value="d">{{ d }} 秒</option>
            </select>
            <span class="field-hint">短剧常用 60–90 秒</span>
          </label>
        </div>

        <div class="field-row">
          <label class="field">
            <span class="field-label">预算上限 cost_cap_cny（元）</span>
            <input
              v-model="form.cost_cap_cny"
              class="input"
              type="number"
              min="0.001"
              step="0.001"
              placeholder="20.000"
            />
            <span class="field-hint">{{ costHint }}</span>
          </label>

          <div class="field checkbox-field">
            <span class="field-label">严格模式 strict_mode</span>
            <label class="checkline">
              <input v-model="form.strict_mode" type="checkbox" />
              <span>开启（违规直接驳回并返工，不放行 warn）</span>
            </label>
            <span class="field-hint">strict_mode 会写进 task.params_json</span>
          </div>
        </div>

        <label class="field">
          <span class="field-label">overrides（JSON 对象，可选）</span>
          <textarea
            v-model="form.overridesText"
            class="input textarea mono"
            rows="3"
            spellcheck="false"
            placeholder='{"EPISODE_LIMIT": 1}'
          ></textarea>
          <span class="field-hint">为空等价于 {}；键名按后端配置项，前端原样透传</span>
        </label>

        <div class="notice">
          <b>EPISODE_LIMIT 提示：</b>
          后端 <code>EPISODE_LIMIT &gt; 0</code> 时只跑前 N 集（调试用），<code>= 0</code> 为全量。
          该开关属于后端环境配置，前端不传；如确需临时覆盖，可写进 overrides，例如
          <code>{"EPISODE_LIMIT": 1}</code>。
        </div>

        <div class="form-actions">
          <button class="btn btn-primary" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '提交任务' }}
          </button>
          <span class="muted small">提交成功后自动切到「进度」页，并建立 SSE 订阅</span>
        </div>

        <div v-if="lastTaskId" class="inline-ok">
          已创建任务 <b>{{ lastTaskId }}</b>
        </div>
      </form>

      <aside class="card flow-card">
        <h2>提交后会发生什么</h2>
        <ol class="flow-list">
          <li v-for="(step, i) in FLOW" :key="step.stage">
            <span class="flow-index">{{ i + 1 }}</span>
            <div>
              <div class="flow-stage">{{ step.stage }}</div>
              <div class="muted small">{{ step.text }}</div>
            </div>
          </li>
        </ol>
        <p class="muted small">
          完整泳道（14 阶段 × gate 5 slot）在「进度」页查看；人工闸口（报价 / 图筛 / 视频）在「待决议」与「资产审核」页处理。
        </p>
      </aside>
    </div>
  </section>
</template>
