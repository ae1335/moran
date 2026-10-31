<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const tab = ref('stats')

// ---------------- 录入日数据 ----------------
const form = ref({ date: today(), reads: '', retention: '', chaptersText: '{\n  "1": { "drop_rate": 0.12 }\n}' })
const saving = ref(false)

function today() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

// ---------------- 历史 ----------------
const loadingHist = ref(false)
const history = ref([])

async function loadHistory() {
  if (!dir.value) return
  loadingHist.value = true
  try {
    const r = await api.feedback(dir.value)
    history.value = (r.days || []).slice().reverse()
  } catch (e) {
    store.toast(`反馈数据加载失败：${e.message}`, 'err')
    history.value = []
  } finally {
    loadingHist.value = false
  }
}

async function submitStats() {
  let chapters = null
  const raw = form.value.chaptersText.trim()
  if (raw) {
    try {
      chapters = JSON.parse(raw)
      if (typeof chapters !== 'object' || Array.isArray(chapters)) throw new Error('需要是对象')
    } catch (e) {
      store.toast(`各章 drop_rate 不是合法 JSON：${e.message}`, 'err')
      return
    }
  }
  const payload = {}
  if (form.value.date) payload.date = form.value.date
  if (form.value.reads !== '' && form.value.reads !== null) payload.reads = Number(form.value.reads)
  if (form.value.retention !== '' && form.value.retention !== null) payload.retention = Number(form.value.retention)
  if (chapters) payload.chapters = chapters
  if (!Object.keys(payload).length) {
    store.toast('至少填一项', 'err')
    return
  }
  saving.value = true
  try {
    const r = await api.feedbackStats(dir.value, payload)
    store.toast(`已录入 ${r.latest?.date || payload.date}，累计 ${r.days} 天`, 'ok')
    await loadHistory()
  } catch (e) {
    store.toast(`录入失败：${e.message}`, 'err')
  } finally {
    saving.value = false
  }
}

// ---------------- 章评 ----------------
const reviewCh = ref('')
const reviewText = ref('')
const reviewResult = ref(null)
const reviewing = ref(false)

async function submitReview() {
  const ch = Number(reviewCh.value)
  if (!ch || ch <= 0) {
    store.toast('请填写章节号', 'err')
    return
  }
  if (reviewText.value.trim().length < 4) {
    store.toast('章评内容太短', 'err')
    return
  }
  reviewing.value = true
  reviewResult.value = null
  try {
    const r = await api.feedbackReview(dir.value, ch, reviewText.value)
    reviewResult.value = r
    store.toast(`第 ${ch} 章章评已入库（${r.chars} 字）`, 'ok')
  } catch (e) {
    store.toast(`章评入库失败：${e.message}`, 'err')
  } finally {
    reviewing.value = false
  }
}

const sentiment = computed(() => {
  const v = reviewResult.value?.summary?.sentiment
  if (v === undefined || v === null) return null
  return Number(v)
})

// ---------------- 复盘 ----------------
const rechecking = ref(false)
const recheckResult = ref(null)
const acceptedIds = ref([])
const confirming = ref(false)
const confirmResult = ref(null)

async function runRecheck() {
  rechecking.value = true
  recheckResult.value = null
  confirmResult.value = null
  acceptedIds.value = []
  try {
    // 只预览，不在此步写真相源
    const r = await api.recheck(dir.value, {})
    recheckResult.value = r
    acceptedIds.value = (r.proposals || []).map((p) => p.id)
    const n = (r.findings || []).length
    store.toast(`复盘完成：${n} 章重审归因，待确认 ${acceptedIds.value.length} 条避雷提案`, 'ok')
  } catch (e) {
    store.toast(`复盘失败：${e.message}`, 'err')
  } finally {
    rechecking.value = false
  }
}

async function confirmProposals() {
  if (!acceptedIds.value.length) {
    store.toast('至少选择一条避雷提案', 'err')
    return
  }
  confirming.value = true
  try {
    const r = await api.recheckConfirm(dir.value, {
      accepted_ids: acceptedIds.value,
      chapters: recheckResult.value?.chapters || null,
    })
    confirmResult.value = r
    // 缺陷 #115：这一节只增不减，超出上界会丢最旧的条目。
    // 不告知的话，作者会以为自己确认过的避雷条目永远都在。
    const trimmed = r.trimmed || 0
    store.toast(
      `已写入 ${r.applied} 条；去重 ${r.skipped_duplicates || 0} 条` +
        (trimmed ? `；因超出 ${r.limit || 200} 条上界，已丢弃最旧的 ${trimmed} 条` : ''),
      trimmed ? 'warn' : 'ok',
    )
  } catch (e) {
    store.toast(`确认失败：${e.message}`, 'err')
  } finally {
    confirming.value = false
  }
}

function fmtPct(v) {
  const n = Number(v)
  if (!isFinite(n)) return '—'
  return `${(n * 100).toFixed(2)}%`
}

/** 某天里弃书率最高的章 */
function topDrop(d) {
  const chs = d.chapters || {}
  let best = null
  for (const [k, v] of Object.entries(chs)) {
    const r = Number(v?.drop_rate ?? 0)
    if (best === null || r > best.r) best = { ch: k, r }
  }
  if (!best) return '—'
  return `第${best.ch}章 ${(best.r * 100).toFixed(1)}%`
}

onMounted(loadHistory)
watch(dir, loadHistory)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>读者反馈</h1>
        <p class="dim small">日数据录入 → 章评归类 → 高弃书率章定向复盘与避雷提案</p>
      </div>
      <div class="row tabs">
        <button class="ghost sm" :class="{ primary: tab === 'stats' }" @click="tab = 'stats'">录入</button>
        <button class="ghost sm" :class="{ primary: tab === 'review' }" @click="tab = 'review'">章评</button>
        <button class="ghost sm" :class="{ primary: tab === 'recheck' }" @click="tab = 'recheck'">复盘</button>
      </div>
    </div>

    <!-- 录入 -->
    <template v-if="tab === 'stats'">
      <div class="card">
        <h2 class="sec">录入日数据</h2>
        <div class="grid3">
          <label class="field">
            日期
            <input v-model="form.date" type="date" />
          </label>
          <label class="field">
            阅读量 reads
            <input v-model="form.reads" type="number" min="0" placeholder="0" />
          </label>
          <label class="field">
            留存率 retention（0-1 或百分比）
            <input v-model="form.retention" type="number" step="0.001" placeholder="0.62" />
          </label>
        </div>
        <label class="field">
          各章 drop_rate（JSON：<code class="mono">{"12": {"drop_rate": 0.35}}</code>）
          <textarea v-model="form.chaptersText" rows="6" class="mono ta" spellcheck="false" />
        </label>
        <div class="row">
          <span class="spacer" />
          <button class="primary" :disabled="saving" @click="submitStats">
            {{ saving ? '提交中…' : '提交' }}
          </button>
        </div>
      </div>

      <div class="card">
        <div class="row">
          <h2 class="sec" style="margin: 0">已录入</h2>
          <span class="badge">{{ history.length }} 天</span>
          <span class="spacer" />
          <button class="ghost sm" :disabled="loadingHist" @click="loadHistory">刷新</button>
        </div>
        <div v-if="loadingHist" class="empty">加载中…</div>
        <div v-else-if="!history.length" class="empty">还没有任何日数据</div>
        <table v-else class="tbl">
          <thead>
            <tr>
              <th>日期</th>
              <th class="num">reads</th>
              <th class="num">retention</th>
              <th class="num">章数</th>
              <th>最高弃书率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in history" :key="d.date">
              <td class="mono">{{ d.date }}</td>
              <td class="num mono">{{ d.reads ?? '—' }}</td>
              <td class="num mono">{{ d.retention !== undefined ? fmtPct(d.retention) : '—' }}</td>
              <td class="num mono">{{ Object.keys(d.chapters || {}).length }}</td>
              <td class="mono">
                {{ topDrop(d) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 章评 -->
    <template v-else-if="tab === 'review'">
      <div class="card">
        <h2 class="sec">粘贴章评</h2>
        <div class="grid3">
          <label class="field">
            章节号
            <input v-model="reviewCh" type="number" min="1" placeholder="12" />
          </label>
        </div>
        <label class="field">
          章评原文（一行一条）
          <textarea v-model="reviewText" rows="9" placeholder="第12章打斗写得好爽&#10;这章太水了，一直在拖" />
        </label>
        <div class="row">
          <span class="faint small mono">{{ reviewText.length }} 字符</span>
          <span class="spacer" />
          <button class="primary" :disabled="reviewing" @click="submitReview">
            {{ reviewing ? '归类中…' : '提交并归类' }}
          </button>
        </div>
      </div>

      <div v-if="reviewResult" class="card">
        <div class="row">
          <h2 class="sec" style="margin: 0">第 {{ reviewResult.ch }} 章归类结果</h2>
          <span class="badge">{{ reviewResult.summary?.total || 0 }} 条</span>
          <span class="spacer" />
          <span
            v-if="sentiment !== null"
            class="badge"
            :class="sentiment > 0.15 ? 'ok' : sentiment < -0.15 ? 'err' : 'warn'"
          >
            情绪值 <span class="mono">{{ sentiment.toFixed(3) }}</span>
          </span>
        </div>
        <div class="stats">
          <div class="stat">
            <div class="stat-label">好评</div>
            <div class="stat-val mono ok">{{ reviewResult.summary?.positive ?? 0 }}</div>
          </div>
          <div class="stat">
            <div class="stat-label">差评</div>
            <div class="stat-val mono err">{{ reviewResult.summary?.negative ?? 0 }}</div>
          </div>
        </div>
        <div class="two">
          <div>
            <div class="small dim" style="margin-bottom: 4px">好评样本</div>
            <div v-if="!(reviewResult.summary?.positive_samples || []).length" class="empty">无</div>
            <ul v-else class="samples ok">
              <li v-for="(s, i) in reviewResult.summary.positive_samples" :key="i">{{ s }}</li>
            </ul>
          </div>
          <div>
            <div class="small dim" style="margin-bottom: 4px">差评样本</div>
            <div v-if="!(reviewResult.summary?.negative_samples || []).length" class="empty">无</div>
            <ul v-else class="samples err">
              <li v-for="(s, i) in reviewResult.summary.negative_samples" :key="i">{{ s }}</li>
            </ul>
          </div>
        </div>
      </div>
    </template>

    <!-- 复盘 -->
    <template v-else>
      <div class="card">
        <div class="row">
          <div>
            <h2 class="sec" style="margin: 0">定向复盘</h2>
            <p class="faint small" style="margin: 2px 0 0">
              取弃书率 top 5% 章节 → Reviewer 规则重审 → 归因 → 作者确认后写入「本章避雷」
            </p>
          </div>
          <span class="spacer" />
          <button class="primary" :disabled="rechecking" @click="runRecheck">
            {{ rechecking ? '复盘中…' : '生成复盘提案' }}
          </button>
        </div>
      </div>

      <div v-if="recheckResult" class="card">
        <div class="row wrap" style="gap: 6px">
          <span class="badge info">候选章 {{ (recheckResult.chapters || []).length }}</span>
          <span class="badge">归因 {{ (recheckResult.findings || []).length }}</span>
          <span class="badge gold">提案 {{ (recheckResult.proposals || []).length }}</span>
          <span class="badge warn">待作者确认</span>
        </div>

        <div v-if="!recheckResult.chapters.length" class="empty">
          没有弃书率数据，先去「录入」填 chapters.drop_rate
        </div>
        <div v-else class="mono small faint" style="margin: 6px 0">
          候选章：{{ recheckResult.chapters.join(', ') }}
        </div>

        <div v-for="f in recheckResult.findings" :key="f.ch" class="finding">
          <div class="row">
            <strong>第 {{ f.ch }} 章</strong>
            <span v-if="f.review_source === 'fresh'" class="badge info">已重审</span>
            <span v-else-if="!f.review_exists" class="badge warn">无审稿记录</span>
            <span class="spacer" />
            <span class="mono faint small">
              {{ Object.entries(f.categories || {}).map(([k, v]) => `${k}×${v}`).join(' ') || '无问题分类' }}
            </span>
          </div>
          <ul class="attr">
            <li v-for="(a, i) in f.attribution" :key="i">
              <span class="badge err">{{ a.type }}</span>
              <span class="dim">{{ a.reason }}</span>
              <span class="faint small">→ 避免：{{ a.avoid }}</span>
              <span class="mono faint small">置信度 {{ a.confidence }}</span>
            </li>
          </ul>
        </div>

        <div v-if="(recheckResult.proposals || []).length" class="proposals">
          <div class="row">
            <div class="small dim">选择要写入「本章避雷」的条目</div>
            <span class="spacer" />
            <button class="primary sm" :disabled="confirming || !!confirmResult" @click="confirmProposals">
              {{ confirming ? '写入中…' : confirmResult ? '已确认' : `确认 ${acceptedIds.length} 条` }}
            </button>
          </div>
          <label v-for="p in recheckResult.proposals" :key="p.id" class="proposal-choice">
            <input v-model="acceptedIds" type="checkbox" :value="p.id" :disabled="!!confirmResult" />
            <span class="mono small">{{ p.entry }}</span>
          </label>
          <div v-if="confirmResult" class="small ok-text" style="margin-top: 8px">
            已写入 {{ confirmResult.applied }} 条，跳过重复 {{ confirmResult.skipped_duplicates || 0 }} 条。
            后续 Planner 会携带这些避雷条目。
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.proposal-choice { display: flex; gap: 8px; align-items: flex-start; padding: 6px 0; cursor: pointer; }
.proposal-choice input { width: auto; margin-top: 3px; flex: 0 0 auto; }
.ok-text { color: var(--ok, #4caf7d); }
.page { max-width: 1100px; }

.tabs { gap: 6px; }

.sec { font-size: 14px; margin: 0 0 10px; font-weight: 600; color: var(--text); }
.card + .card { margin-top: 12px; }

.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px 14px; }
@media (max-width: 700px) { .grid3 { grid-template-columns: 1fr; } }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.ta { font-family: var(--mono); line-height: 1.6; font-size: 12px; }

.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tbl th, .tbl td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--text-faint); font-weight: 500; font-size: 11.5px; }
.tbl .num { text-align: right; }

.stats { display: flex; gap: 12px; margin: 10px 0; }
.stat { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px 14px; min-width: 90px; }
.stat-label { font-size: 11px; color: var(--text-faint); }
.stat-val { font-size: 20px; font-weight: 600; }
.stat-val.ok { color: var(--ok); }
.stat-val.err { color: var(--err); }

.two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 700px) { .two { grid-template-columns: 1fr; } }
.samples { margin: 0; padding-left: 16px; font-size: 12.5px; display: flex; flex-direction: column; gap: 3px; }
.samples.ok li { color: #9fd8b6; }
.samples.err li { color: #f0a9a7; }

.finding { border-top: 1px solid var(--border); padding: 8px 0; }
.finding:first-of-type { border-top: none; }
.attr { margin: 4px 0 0; padding-left: 14px; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.attr li { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.proposals { border-top: 1px solid var(--border); padding-top: 8px; }
.pre {
  margin: 4px 0 0; padding: 8px 10px; background: var(--bg); border-radius: var(--radius);
  font-size: 11.5px; line-height: 1.7; color: var(--text-dim); white-space: pre-wrap; word-break: break-word;
}
</style>
