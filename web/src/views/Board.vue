<script setup>
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CLOSING_STATE_TEXT, getShots, resolveTask } from '../api'

const ctx = inject('drama')

const shots = ref([])
const packs = ref([])
const loading = ref(false)
const loadError = ref('')
const notice = ref('')
const busyShot = ref('')
const brokenThumbs = ref(new Set())
const zoomShot = ref(null)
const hintShot = ref('')
const hintText = ref('')

function load() {
  if (!ctx.taskId) {
    shots.value = []
    packs.value = []
    return
  }
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  getShots(ctx.taskId)
    .then((data) => {
      shots.value = Array.isArray(data?.shots) ? data.shots : []
      packs.value = Array.isArray(data?.packs) ? data.packs : []
    })
    .catch((e) => {
      loadError.value = e?.message || String(e)
      shots.value = []
      packs.value = []
    })
    .finally(() => {
      loading.value = false
    })
}

onMounted(load)
watch(() => [ctx.taskId, ctx.tick], load)

/** episode → scene → shots 三级分组（§15 产物按集/场组织） */
const episodes = computed(() => {
  const byEp = new Map()
  for (const shot of shots.value) {
    const ep = Number(shot.episode ?? 0)
    if (!byEp.has(ep)) byEp.set(ep, new Map())
    const byScene = byEp.get(ep)
    const scene = Number(shot.scene ?? 0)
    if (!byScene.has(scene)) byScene.set(scene, [])
    byScene.get(scene).push(shot)
  }
  const eps = [...byEp.entries()].sort((a, b) => a[0] - b[0])
  return eps.map(([episode, sceneMap]) => {
    const scenes = [...sceneMap.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([scene, list]) => ({
        scene,
        shots: list.slice().sort((a, b) => String(a.shot_id).localeCompare(String(b.shot_id))),
      }))
    return {
      episode,
      scenes,
      count: scenes.reduce((n, s) => n + s.shots.length, 0),
    }
  })
})

const packByShot = computed(() => {
  const map = new Map()
  for (const pack of packs.value) if (pack?.shot_id) map.set(pack.shot_id, pack)
  return map
})

const totalShots = computed(() => shots.value.length)
const heroCount = computed(() => shots.value.filter((s) => s.quality_tier === 'hero').length)
const turnCount = computed(() => shots.value.filter((s) => s.is_turn).length)
const withFrame = computed(() => shots.value.filter((s) => !!s.keyframe_url).length)

function thumbBroken(shotId) {
  return brokenThumbs.value.has(shotId)
}

function onThumbError(shotId) {
  const next = new Set(brokenThumbs.value)
  next.add(shotId)
  brokenThumbs.value = next
}

function tierText(tier) {
  if (tier === 'hero') return 'hero 精拍'
  if (tier === 'fast') return 'fast 快档'
  return tier || '未赋档'
}

function closingText(state) {
  return CLOSING_STATE_TEXT[state] || state || '—'
}

function openZoom(shot) {
  zoomShot.value = shot
}

function closeZoom() {
  zoomShot.value = null
}

function openHint(shot) {
  hintShot.value = shot.shot_id
  hintText.value = ''
}

function cancelHint() {
  hintShot.value = ''
  hintText.value = ''
}

async function retry(shot, withHint) {
  if (busyShot.value) return
  const payload = {}
  if (withHint) {
    const hint = hintText.value.trim()
    if (!hint) {
      notice.value = '请先填写改词提示 prompt_hint（例如：把台词改短、动作收敛）'
      return
    }
    payload.prompt_hint = hint
  }
  busyShot.value = shot.shot_id
  notice.value = ''
  try {
    await resolveTask(ctx.taskId, 'retry_shot', [shot.shot_id], payload)
    notice.value = `已提交 retry_shot：${shot.shot_id}${withHint ? '（含 prompt_hint）' : ''}`
    cancelHint()
    ctx.tick += 1
    load()
  } catch (e) {
    notice.value = `重抽提交失败：${e?.message || e}`
  } finally {
    busyShot.value = ''
  }
}

// 分镜板只处理 Esc 关闭放大视图；键盘流（1/2/3）只属于资产审核页
function onKeydown(event) {
  if (event.key === 'Escape' && zoomShot.value) closeZoom()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <section class="view board-view">
    <div class="view-head">
      <div>
        <h1>分镜板</h1>
        <p class="muted">
          按 episode / scene 分组；每卡展示动作、台词、时长、相机、收束态与画质档位，首帧可点击放大。逐镜可重抽或改词重抽。
        </p>
      </div>
      <div class="head-meta">
        <span class="pill">镜头 {{ totalShots }}</span>
        <span class="pill">hero {{ heroCount }}</span>
        <span class="pill">转折镜 {{ turnCount }}</span>
        <span class="pill">有首帧 {{ withFrame }}</span>
        <button class="btn btn-ghost" type="button" :disabled="loading || !ctx.taskId" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="!ctx.taskId" class="empty">
      <div class="empty-title">尚未选择任务</div>
      <div class="empty-hint">在顶栏选择或输入 task_id 后，这里会渲染该任务的分镜卡。</div>
    </div>

    <template v-else>
      <div v-if="notice" class="inline-ok">{{ notice }}</div>
      <div v-if="loadError" class="inline-error">
        分镜数据拉取失败：{{ loadError }}（storyboard 阶段未 PASSED 时属正常）
      </div>

      <div v-if="!shots.length && !loadError" class="empty">
        <div class="empty-title">还没有分镜</div>
        <div class="empty-hint">
          storyboard 阶段尚未产出 shots.json；等 prompt_pack / gate 推进后回来刷新，或查看「进度」页泳道。
        </div>
      </div>

      <div v-for="ep in episodes" :key="ep.episode" class="ep-block">
        <div class="ep-head">
          <h2>第 {{ ep.episode }} 集</h2>
          <span class="muted small">{{ ep.count }} 镜 · {{ ep.scenes.length }} 场</span>
        </div>

        <div v-for="scene in ep.scenes" :key="scene.scene" class="scene-block">
          <div class="scene-head">
            <span class="scene-label">场次 {{ scene.scene }}</span>
            <span class="muted small">{{ scene.shots.length }} 镜</span>
          </div>

          <div class="shot-grid">
            <article v-for="shot in scene.shots" :key="shot.shot_id" class="shot-card">
              <button class="shot-thumb" type="button" @click="openZoom(shot)" :title="shot.shot_id">
                <img
                  v-if="shot.keyframe_url && !thumbBroken(shot.shot_id)"
                  :src="shot.keyframe_url"
                  :alt="shot.shot_id"
                  loading="lazy"
                  @error="onThumbError(shot.shot_id)"
                />
                <span v-else class="thumb-empty">无首帧</span>
                <span class="tier-badge" :class="'tier-' + (shot.quality_tier || 'none')">
                  {{ tierText(shot.quality_tier) }}
                </span>
                <span v-if="shot.is_turn" class="turn-badge">转折镜</span>
                <span class="dur-badge">{{ shot.duration_sec ?? '—' }}s</span>
              </button>

              <div class="shot-body">
                <div class="shot-head">
                  <code class="shot-id">{{ shot.shot_id }}</code>
                  <span class="muted small">E{{ shot.episode }} / S{{ shot.scene }}</span>
                </div>
                <p class="shot-action">{{ shot.action || '（无动作描述）' }}</p>
                <p class="shot-line">
                  <span v-if="shot.dialogue_speaker" class="speaker">{{ shot.dialogue_speaker }}</span>
                  {{ shot.line || '（无台词）' }}
                </p>
                <div class="shot-meta">
                  <span>相机 {{ shot.camera || '—' }}</span>
                  <span>收束 {{ closingText(shot.closing_state) }}</span>
                  <span>首帧 {{ shot.keyframe_url ? '已出图' : '待生成' }}</span>
                </div>
                <div v-if="shot.scene_anchor" class="shot-anchor" :title="shot.scene_anchor">
                  场景锚：{{ shot.scene_anchor }}
                </div>
                <div
                  v-if="Array.isArray(shot.characters) && shot.characters.length"
                  class="shot-chars"
                >
                  <span>出场：</span>
                  <span v-for="c in shot.characters" :key="c" class="char-chip">{{ c }}</span>
                </div>

                <div class="shot-actions">
                  <button
                    class="btn btn-ghost"
                    type="button"
                    :disabled="busyShot === shot.shot_id"
                    @click="retry(shot, false)"
                  >
                    重抽
                  </button>
                  <button class="btn btn-ghost" type="button" @click="openHint(shot)">改词重抽</button>
                  <span v-if="busyShot === shot.shot_id" class="muted small">提交中…</span>
                </div>

                <div v-if="hintShot === shot.shot_id" class="hint-editor">
                  <textarea
                    v-model="hintText"
                    class="input textarea"
                    rows="3"
                    placeholder="prompt_hint：指明这次重抽要改什么（台词更短、动作收敛、构图更近…）"
                  ></textarea>
                  <div class="hint-actions">
                    <button
                      class="btn btn-primary"
                      type="button"
                      :disabled="busyShot === shot.shot_id"
                      @click="retry(shot, true)"
                    >
                      确认改词重抽
                    </button>
                    <button class="btn btn-ghost" type="button" @click="cancelHint">取消</button>
                  </div>
                </div>
              </div>
            </article>
          </div>
        </div>
      </div>
    </template>

    <!-- 首帧放大 -->
    <div v-if="zoomShot" class="modal-mask" @click.self="closeZoom">
      <div class="modal">
        <header class="modal-head">
          <div>
            <b>{{ zoomShot.shot_id }}</b>
            <span class="muted small">E{{ zoomShot.episode }} / S{{ zoomShot.scene }} · {{ zoomShot.duration_sec }}s</span>
          </div>
          <button class="error-close" type="button" @click="closeZoom">×</button>
        </header>

        <div class="modal-media">
          <img
            v-if="zoomShot.keyframe_url && !thumbBroken(zoomShot.shot_id)"
            :src="zoomShot.keyframe_url"
            :alt="zoomShot.shot_id"
            @error="onThumbError(zoomShot.shot_id)"
          />
          <div v-else class="thumb-empty large">首帧尚未生成（keyframe 阶段未 PASSED 或文件缺失）</div>
        </div>

        <div class="modal-body">
          <dl class="kv">
            <dt>动作</dt>
            <dd>{{ zoomShot.action || '—' }}</dd>
            <dt>台词</dt>
            <dd>
              <span v-if="zoomShot.dialogue_speaker" class="speaker">{{ zoomShot.dialogue_speaker }}：</span>
              {{ zoomShot.line || '—' }}
            </dd>
            <dt>相机</dt>
            <dd>{{ zoomShot.camera || '—' }}</dd>
            <dt>收束态</dt>
            <dd>{{ closingText(zoomShot.closing_state) }}</dd>
            <dt>画质档位</dt>
            <dd>{{ tierText(zoomShot.quality_tier) }}</dd>
            <dt>场景锚</dt>
            <dd>{{ zoomShot.scene_anchor || '—' }}</dd>
            <template v-if="packByShot.get(zoomShot.shot_id)">
              <dt>图片提示词</dt>
              <dd class="mono small">{{ packByShot.get(zoomShot.shot_id).image_prompt || '—' }}</dd>
              <dt>运镜提示词</dt>
              <dd class="mono small">{{ packByShot.get(zoomShot.shot_id).motion_prompt || '—' }}</dd>
              <dt>配音文本</dt>
              <dd class="mono small">{{ packByShot.get(zoomShot.shot_id).tts_text || '—' }}</dd>
              <dt>分辨率</dt>
              <dd>{{ packByShot.get(zoomShot.shot_id).resolution || '—' }}</dd>
            </template>
          </dl>
        </div>
      </div>
    </div>
  </section>
</template>
