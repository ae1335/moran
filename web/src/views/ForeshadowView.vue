<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const loading = ref(false)
const items = ref([])
const alerts = ref([])
const currentCh = ref(0)
const busyId = ref('')
const onlyActive = ref(false)

// 时间线（§13.2 要求伏笔页同时给「状态机总览 + 超期报警 + 时间线视图」）
const tab = ref('board')          // board | timeline
const timeline = ref([])
const tlIssues = ref([])
const loadingTl = ref(false)

const ORDER = ['draft', 'planted', 'hinted', 'resolved', 'dropped']
const STATUS_LABEL = {
  draft: '草案',
  planted: '已埋',
  hinted: '已暗示',
  resolved: '已回收',
  dropped: '已放弃',
}
const STATUS_KIND = {
  draft: 'warn',
  planted: 'info',
  hinted: 'gold',
  resolved: 'ok',
  dropped: '',
}
// 与后端 server/core/domain.py TRANSITIONS 保持一致
const TRANSITIONS = {
  draft: ['planted', 'dropped'],
  planted: ['hinted', 'resolved', 'dropped'],
  hinted: ['resolved', 'dropped'],
  resolved: [],
  dropped: [],
}

const groups = computed(() => {
  const g = {}
  for (const s of ORDER) g[s] = []
  for (const it of items.value) {
    const s = ORDER.includes(it.status) ? it.status : 'draft'
    g[s].push(it)
  }
  for (const s of ORDER) g[s].sort((a, b) => (a.id > b.id ? 1 : -1))
  return g
})

const alertMap = computed(() => {
  const m = {}
  for (const a of alerts.value) m[a.id] = a
  return m
})

const overdue = computed(() => alerts.value.filter((a) => a.level === 'overdue'))
const dueSoon = computed(() => alerts.value.filter((a) => a.level === 'due_soon'))

const shownGroups = computed(() => {
  return ORDER.map((s) => {
    const list = groups.value[s]
    const filtered = onlyActive.value ? list.filter((it) => it.status === 'planted' || it.status === 'hinted') : list
    return { status: s, list: filtered, total: list.length }
  }).filter((g) => g.list.length)
})

function optionsFor(item) {
  const next = TRANSITIONS[item.status] || []
  return [item.status, ...next.filter((s) => s !== item.status)]
}

async function load() {
  if (!dir.value) return
  loading.value = true
  try {
    const r = await api.foreshadow(dir.value)
    items.value = r.items || []
    alerts.value = r.alerts || []
    currentCh.value = r.current_chapter || 0
  } catch (e) {
    store.toast(`伏笔面板加载失败：${e.message}`, 'err')
    items.value = []
    alerts.value = []
  }
  // 时间线独立加载：伏笔接口失败也不该拖累它
  try {
    await loadTimeline()
  } finally {
    loading.value = false
  }
}

async function loadTimeline() {
  if (!dir.value) return
  loadingTl.value = true
  try {
    const r = await api.timeline(dir.value)
    timeline.value = r.entries || []
    tlIssues.value = r.issues || []
  } catch (e) {
    store.toast(`时间线加载失败：${e.message}`, 'err')
    timeline.value = []
    tlIssues.value = []
  } finally {
    loadingTl.value = false
  }
}

/** 把日期换成相对天数，方便看跨度（无法解析的显示原文） */
function gapFromPrev(idx) {
  if (idx === 0) return ''
  const cur = timeline.value[idx]
  const prev = timeline.value[idx - 1]
  const d1 = cur.days_since_start
  const d2 = prev.days_since_start
  if (d1 == null || d2 == null) return ''
  const diff = d1 - d2
  return diff === 0 ? '同日' : `+${diff} 天`
}

async function changeStatus(item, ev) {
  const next = ev.target.value
  if (next === item.status) return
  busyId.value = item.id
  try {
    const updated = await api.patchForeshadow(dir.value, item.id, { status: next })
    Object.assign(item, updated)
    store.toast(`伏笔 ${item.id} → ${STATUS_LABEL[updated.status] || updated.status}`, 'ok')
    await load()
  } catch (e) {
    if (e.status === 409) {
      store.toast(`状态迁移被拒绝：${e.message}`, 'err')
    } else {
      store.toast(`更新伏笔失败：${e.message}`, 'err')
    }
    ev.target.value = item.status
  } finally {
    busyId.value = ''
  }
}

onMounted(load)
watch(dir, load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>伏笔面板</h1>
        <p class="dim small">
          状态机 <span class="mono">draft → planted → hinted → resolved | dropped</span> ·
          当前进度 <span class="mono">第 {{ currentCh }} 章</span>
        </p>
      </div>
      <div class="row">
        <div class="tabs">
          <button class="ghost sm" :class="{ primary: tab === 'board' }" @click="tab = 'board'">
            伏笔 {{ items.length }}
          </button>
          <button class="ghost sm" :class="{ primary: tab === 'timeline' }" @click="tab = 'timeline'">
            时间线 {{ timeline.length }}
            <span v-if="tlIssues.length" class="badge err" style="margin-left: 4px">{{ tlIssues.length }}</span>
          </button>
        </div>
        <label v-if="tab === 'board'" class="row small dim" style="gap: 4px">
          <input v-model="onlyActive" type="checkbox" style="width: auto" /> 只看活跃
        </label>
        <button class="ghost sm" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
      </div>
    </div>

    <!-- 时间线视图（§13.2） -->
    <template v-if="tab === 'timeline'">
      <div v-if="loadingTl" class="empty">加载时间线…</div>
      <div v-else-if="!timeline.length" class="empty">
        还没有时间线条目。章纲里写 foreshadow_actions 并确认沉淀，或正文里出现「XX历 N年 N月 N日」后会自动追加。
      </div>
      <template v-else>
        <div v-if="tlIssues.length" class="block warn-block">
          <strong class="small">{{ tlIssues.length }} 个时间线问题</strong>
          <div v-for="(i, k) in tlIssues" :key="k" class="small alert-row">
            <span class="badge" :class="i.level === 'error' ? 'err' : 'warn'">{{ i.level }}</span>
            {{ i.message }}
          </div>
        </div>
        <div class="card">
          <div class="tl">
            <div v-for="(e, idx) in timeline" :key="e.ch" class="tl-row">
              <div class="tl-ch mono">第 {{ e.ch }} 章</div>
              <div class="tl-line">
                <span class="tl-dot" />
                <span v-if="gapFromPrev(idx)" class="tl-gap mono">{{ gapFromPrev(idx) }}</span>
              </div>
              <div class="tl-body">
                <div class="row">
                  <span class="mono tl-date">{{ e.date || '（无日期）' }}</span>
                  <span class="spacer" />
                  <span class="faint small">{{ e.event }}</span>
                </div>
                <div v-if="e.note" class="faint small">{{ e.note }}</div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </template>

    <template v-if="tab === 'board'">
    <div v-if="overdue.length" class="block err-block">
      <strong class="small">超期伏笔（{{ overdue.length }}）</strong>
      <div v-for="a in overdue" :key="a.id" class="alert-row small">
        <code class="mono">{{ a.id }}</code>
        <span>{{ a.desc }}</span>
        <span class="faint">
          截止 <span class="mono">{{ a.deadline_ch }}</span> · 已超 <span class="mono">{{ a.overdue_by }}</span> 章
        </span>
        <span v-if="a.suggestion" class="dim">— {{ a.suggestion }}</span>
      </div>
    </div>

    <div v-if="dueSoon.length" class="block warn-block">
      <strong class="small">临近到期（{{ dueSoon.length }} 章内）</strong>
      <div v-for="a in dueSoon" :key="a.id" class="alert-row small">
        <code class="mono">{{ a.id }}</code>
        <span>{{ a.desc }}</span>
        <span class="faint">截止 <span class="mono">{{ a.deadline_ch }}</span> · 还剩 <span class="mono">{{ a.remaining }}</span> 章</span>
      </div>
    </div>

    <div class="card stat-card">
      <div class="stat-bar">
        <div
          v-for="s in ORDER"
          :key="s"
          class="stat-seg"
          :class="'seg-' + s"
          :style="{ flex: Math.max(groups[s].length, 0.001) }"
          :title="`${STATUS_LABEL[s]}：${groups[s].length}`"
        />
      </div>
      <div class="row wrap stat-legend">
        <span v-for="s in ORDER" :key="s" class="row small" style="gap: 5px">
          <i class="dot" :class="'seg-' + s" />
          {{ STATUS_LABEL[s] }}
          <span class="mono dim">{{ groups[s].length }}</span>
        </span>
        <span class="spacer" />
        <span class="faint small">共 <span class="mono">{{ items.length }}</span> 条</span>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!items.length" class="empty">
      还没有伏笔条目。章纲里写 <code class="mono">foreshadow_actions</code> 并确认后会自动登记。
    </div>

    <div v-else class="groups">
      <section v-for="g in shownGroups" :key="g.status" class="group">
        <div class="group-head">
          <span class="badge" :class="STATUS_KIND[g.status] || ''">{{ STATUS_LABEL[g.status] }}</span>
          <span class="faint small mono">{{ g.list.length }} / {{ g.total }}</span>
        </div>

        <div class="cards">
          <div v-for="it in g.list" :key="it.id" class="fs-card" :class="{ hot: alertMap[it.id] }">
            <div class="row wrap">
              <code class="mono id">{{ it.id }}</code>
              <span class="badge" :class="STATUS_KIND[it.status] || ''">{{ STATUS_LABEL[it.status] }}</span>
              <span v-if="alertMap[it.id]" class="badge" :class="alertMap[it.id].level === 'overdue' ? 'err' : 'warn'">
                {{ alertMap[it.id].level === 'overdue' ? '已超期' : '临近到期' }}
              </span>
              <span class="spacer" />
              <select
                class="status-select"
                :value="it.status"
                :disabled="busyId === it.id || !(TRANSITIONS[it.status] || []).length"
                @change="changeStatus(it, $event)"
              >
                <option v-for="s in optionsFor(it)" :key="s" :value="s">{{ STATUS_LABEL[s] }}</option>
              </select>
            </div>

            <div class="desc">{{ it.desc || '（无描述）' }}</div>

            <div class="row wrap meta small faint">
              <span>埋设 <span class="mono">{{ it.planted_ch ?? '—' }}</span></span>
              <span>暗示 <span class="mono">{{ (it.hinted_chs || []).length ? (it.hinted_chs || []).join(',') : '—' }}</span></span>
              <span>截止 <span class="mono">{{ it.deadline_ch ?? '—' }}</span></span>
              <span v-if="it.resolution_ch != null">回收 <span class="mono">{{ it.resolution_ch }}</span></span>
            </div>

            <div v-if="it.payoff_plan" class="payoff small">
              <span class="faint">回收计划：</span>{{ it.payoff_plan }}
            </div>
            <div v-if="it.dropped_reason" class="payoff small">
              <span class="faint">放弃原因：</span>{{ it.dropped_reason }}
            </div>
          </div>
        </div>
      </section>
    </div>
    </template>
  </div>
</template>

<style scoped>
.tabs { display: flex; gap: 6px; }
.tl { display: flex; flex-direction: column; gap: 0; margin-top: 4px; }
.tl-row { display: grid; grid-template-columns: 84px 40px 1fr; align-items: stretch; min-height: 54px; }
.tl-ch { font-size: 12px; color: var(--text-dim); padding-top: 16px; line-height: 1.4; }
/* 节点固定在距顶 20px 处，和 .tl-body 的第一行文字垂直居中 */
.tl-line { position: relative; display: flex; align-items: flex-start; justify-content: center; padding-top: 20px; }
.tl-line::before {
  content: ''; position: absolute; top: 0; bottom: 0; left: 50%; width: 1px;
  background: var(--border); transform: translateX(-50%);
}
.tl-dot {
  position: relative; z-index: 1; width: 9px; height: 9px; border-radius: 50%;
  background: var(--accent); box-shadow: 0 0 0 3px var(--bg);
}
.tl-gap {
  position: absolute; top: 3px; left: 50%; transform: translateX(-50%);
  font-size: 10px; color: var(--text-faint); background: var(--bg); padding: 0 3px;
  white-space: nowrap;
}
.tl-body {
  padding: 14px 0 14px 6px; border-bottom: 1px solid var(--line);
}
.tl-row:last-child .tl-body { border-bottom: none; }
.tl-date { font-size: 13px; color: var(--text); }
.warn-block { border-color: rgba(217, 164, 65, 0.4); }

.page { max-width: 1200px; }


.block { border-radius: var(--radius); padding: 9px 12px; margin-bottom: 12px; border: 1px solid var(--border); border-left-width: 3px; }
.alert-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; margin-top: 6px; }
.warn-block { background: rgba(217, 164, 65, 0.08); border-color: rgba(217, 164, 65, 0.35); border-left-color: var(--warn); }
.err-block { background: rgba(217, 83, 79, 0.08); border-color: rgba(217, 83, 79, 0.35); border-left-color: var(--err); }

.stat-card { margin-bottom: 14px; }
.stat-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: var(--bg); }
.stat-seg { height: 100%; }
.stat-legend { margin-top: 8px; gap: 14px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.seg-draft { background: var(--warn); }
.seg-planted { background: var(--info); }
.seg-hinted { background: var(--accent); }
.seg-resolved { background: var(--ok); }
.seg-dropped { background: var(--border-strong); }

.groups { display: flex; flex-direction: column; gap: 16px; }
.group-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px; }
.fs-card {
  background: var(--bg-panel); border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong); border-radius: var(--radius); padding: 10px 12px;
}
.fs-card.hot { border-left-color: var(--err); }
.id { color: var(--accent); font-size: 12px; }
.desc { margin: 7px 0 6px; font-size: 14px; }
.meta { gap: 12px; }
.payoff { margin-top: 6px; color: var(--text-dim); }
.status-select { width: auto; font-size: 12px; padding: 2px 6px; }
</style>
