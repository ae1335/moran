<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const dir = computed(() => route.params.book)

const chapters = ref([])
const currentCh = ref(null)
const report = ref(null)
const running = ref(false)
const filter = ref('all')
const useModel = ref(true)
const selectedDiff = ref('')

const severityOrder = { high: 0, mid: 1, low: 2 }

const shown = computed(() => {
  if (!report.value) return []
  const list = [...(report.value.issues || [])]
  return list
    .filter((i) => filter.value === 'all' || i.severity === filter.value)
    .sort((a, b) => (severityOrder[a.severity] ?? 3) - (severityOrder[b.severity] ?? 3))
})

const counts = computed(() => {
  const c = { high: 0, mid: 0, low: 0 }
  for (const i of report.value?.issues || []) c[i.severity] = (c[i.severity] || 0) + 1
  return c
})

async function loadList() {
  try {
    chapters.value = await store.loadChapters(dir.value)
  } catch (e) {
    store.toast(`章节列表加载失败：${e.message}`, 'err')
  }
}

async function open(ch) {
  currentCh.value = ch
  report.value = null
  router.replace(`/b/${encodeURIComponent(dir.value)}/review/${ch}`)
  adopted.value = new Set()
  selectedDiff.value = ''
  suggestions.value = {}
  try {
    report.value = await api.getReview(dir.value, ch)
    await loadSuggestions()
  } catch (e) {
    if (e.status !== 404) store.toast(`读取审稿报告失败：${e.message}`, 'err')
  }
}

async function run() {
  if (!currentCh.value) return
  running.value = true
  try {
    const r = await api.review(dir.value, currentCh.value, { use_model: useModel.value })
    report.value = { ch: currentCh.value, issues: r.issues, model: r.model, stats: { counts: r.counts } }
    store.toast(`审稿完成：${r.issues.length} 个问题`, 'ok')
    adopted.value = new Set()
    selectedDiff.value = ''
    await loadSuggestions()
    await loadList()
  } catch (e) {
    store.toast(`审稿失败：${e.message}`, 'err')
  } finally {
    running.value = false
  }
}

/** 采纳 = 选中该条问题的改写建议（diff 由后端生成，预览不落盘） */
const adopted = ref(new Set())
const suggesting = ref(false)
/** issue_index → Suggestion */
const suggestions = ref({})

const issueKey = (issue) => `${issue.category}:${(issue.evidence || '').slice(0, 20)}`

/** diff 行着色：+ 绿 / - 红 / @@ 淡 */
function diffClass(line) {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  if (line.startsWith('@@')) return 'meta'
  return ''
}

async function loadSuggestions() {
  if (!currentCh.value) return
  suggesting.value = true
  try {
    const r = await api.reviewSuggest(dir.value, currentCh.value, null)
    const map = {}
    for (const s of r.suggestions || []) map[s.issue_index] = s
    suggestions.value = map
  } catch (e) {
    store.toast(`生成改写建议失败：${e.message}`, 'err')
    suggestions.value = {}
  } finally {
    suggesting.value = false
  }
}

function adopt(issue, idx) {
  const key = issueKey(issue)
  if (adopted.value.has(key)) adopted.value.delete(key)
  else adopted.value.add(key)
  adopted.value = new Set(adopted.value)
}

/** 只预览已采纳的条目 */
async function previewSelected() {
  if (!currentCh.value) return
  const picked = adopted.value.size
  if (!picked) {
    store.toast('先勾选要采纳的问题', 'err')
    return
  }
  suggesting.value = true
  try {
    const r = await api.reviewSuggest(dir.value, currentCh.value, null)
    const keep = {}
    for (const [idx, s] of Object.entries(suggestions.value)) {
      const key = issueKey(report.value.issues[idx])
      if (adopted.value.has(key)) keep[idx] = s
    }
    selectedDiff.value = Object.values(keep)
      .filter((s) => s.applicable && s.diff)
      .map((s) => s.diff)
      .join('\n')
    store.toast(`已采纳 ${picked} 条，其中 ${Object.values(keep).filter((s) => s.applicable).length} 条可自动改写`, 'ok')
  } catch (e) {
    store.toast(`预览失败：${e.message}`, 'err')
  } finally {
    suggesting.value = false
  }
}

onMounted(async () => {
  await loadList()
  const ch = Number(route.params.ch)
  if (ch) open(ch)
  else if (chapters.value.length) open(chapters.value[0].ch)
})

watch(() => route.params.ch, (v) => {
  const n = Number(v)
  if (v && n !== currentCh.value) open(n)
})
</script>

<template>
  <div class="review-layout">
    <aside class="ch-list">
      <div class="ch-head"><span class="small dim">待审章节</span><span class="spacer" /><span class="badge">{{ chapters.length }}</span></div>
      <div class="ch-scroll">
        <div v-for="c in chapters" :key="c.ch" class="ch-item" :class="{ active: currentCh === c.ch }" @click="open(c.ch)">
          <div class="row">
            <span class="mono small">{{ String(c.ch).padStart(4, '0') }}</span>
            <span class="trunc">{{ c.title || '（未命名）' }}</span>
          </div>
          <div class="row small faint">
            <span class="mono">{{ c.words }} 字</span>
            <span class="spacer" />
            <span v-if="c.has_review" class="badge info">已审</span>
          </div>
        </div>
        <div v-if="!chapters.length" class="empty">还没有章节</div>
      </div>
    </aside>

    <section class="main">
      <div v-if="!currentCh" class="empty">从左侧选一章开始审稿</div>
      <template v-else>
        <div class="head">
          <div class="row">
            <strong>第 {{ currentCh }} 章 审稿</strong>
            <span v-if="report" class="badge err">high {{ counts.high }}</span>
            <span v-if="report" class="badge warn">mid {{ counts.mid }}</span>
            <span v-if="report" class="badge">low {{ counts.low }}</span>
            <span v-if="report?.model" class="faint small">{{ report.model }}</span>
          </div>
          <div class="row">
            <label class="row small dim" style="gap: 4px">
              <input type="checkbox" v-model="useModel" style="width: auto" /> 启用模型审稿
            </label>
            <button class="primary sm" :disabled="running" @click="run">
              {{ running ? '审稿中…' : report ? '重新审稿' : '开始审稿' }}
            </button>
          </div>
        </div>

        <div v-if="report" class="filters">
          <button v-for="f in ['all', 'high', 'mid', 'low']" :key="f"
                  class="ghost sm" :class="{ primary: filter === f }" @click="filter = f">
            {{ f === 'all' ? '全部' : f }}
          </button>
          <span class="spacer" />
          <span class="faint small">共 {{ report.issues.length }} 条 · 已采纳 {{ adopted.size }}</span>
          <button class="ghost sm" :disabled="suggesting || !adopted.size" @click="previewSelected">
            {{ suggesting ? '生成中…' : '预览已采纳的改写' }}
          </button>
        </div>

        <div v-if="selectedDiff" class="card diff-card">
          <div class="row">
            <strong class="small">改写预览（不会自动保存）</strong>
            <span class="spacer" />
            <button class="ghost sm" @click="selectedDiff = ''">关闭</button>
          </div>
          <pre class="diff mono"><span
            v-for="(line, i) in selectedDiff.split('\n')"
            :key="i"
            class="diff-line"
            :class="diffClass(line)"
          >{{ line }}</span></pre>
          <p class="faint small" style="margin: 6px 0 0">
            这里只是「采纳后会变成什么样」。要真正落盘，去写作台用它覆盖，或跑反 AI 味润色。
          </p>
        </div>

        <div v-if="report && !report.issues.length" class="empty">
          ✓ 未检出问题。注意：规则层只覆盖可量化项，最终仍需人工通读。
        </div>

        <div class="issues">
          <div v-for="(i, idx) in shown" :key="idx" class="issue" :class="i.severity">
            <div class="row">
              <span class="badge" :class="{ err: i.severity === 'high', warn: i.severity === 'mid', info: i.severity === 'low' }">
                {{ i.severity }}
              </span>
              <span class="badge">{{ i.category }}</span>
              <span class="spacer" />
              <button class="ghost sm" :class="{ primary: adopted.has(issueKey(i)) }" @click="adopt(i, idx)">
                {{ adopted.has(issueKey(i)) ? '已采纳' : (suggestions[idx]?.applicable ? '采纳' : '标记') }}
              </button>
            </div>
            <div v-if="i.evidence" class="evidence">{{ i.evidence }}</div>
            <div v-if="i.suggestion" class="suggestion">→ {{ i.suggestion }}</div>
            <template v-if="suggestions[idx]">
              <pre v-if="suggestions[idx].diff" class="diff mono"><span
                v-for="(line, i) in suggestions[idx].diff.split('\n')"
                :key="i"
                class="diff-line"
                :class="diffClass(line)"
              >{{ line }}</span></pre>
              <div v-else-if="!suggestions[idx].applicable" class="faint small not-applicable">
                无法自动改写：{{ suggestions[idx].reason }}
              </div>
            </template>
          </div>
          <div v-if="!shown.length && report" class="empty">该严重级别下没有问题</div>
        </div>

        <div v-if="!report && !running" class="empty">还没有审稿报告，点右上角开始审稿</div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.diff-card { margin: 10px 0; }
.diff {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 9px 11px;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
  margin: 8px 0 0;
}
.diff-line { display: block; }
.diff-line.add { color: var(--ok, #4caf7d); }
.diff-line.del { color: var(--err, #d9534f); }
.diff-line.meta { color: var(--text-faint, #7a8290); }
.not-applicable { margin-top: 6px; font-style: italic; }

.review-layout { display: flex; height: 100%; min-height: 0; }
.ch-list { width: 230px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--bg-panel); display: flex; flex-direction: column; }
.ch-head { display: flex; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.ch-scroll { flex: 1; overflow-y: auto; padding: 4px; }
.ch-item { padding: 6px 8px; border-radius: var(--radius); cursor: pointer; }
.ch-item:hover { background: var(--bg-hover); }
.ch-item.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.trunc { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.main { flex: 1; min-width: 0; overflow-y: auto; padding: 14px 20px; }
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
.filters { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
.issues { display: flex; flex-direction: column; gap: 8px; }
.issue {
  background: var(--bg-panel); border: 1px solid var(--border);
  border-left-width: 3px; border-radius: var(--radius); padding: 9px 11px;
}
.issue.high { border-left-color: var(--err); }
.issue.mid { border-left-color: var(--warn); }
.issue.low { border-left-color: var(--info); }
.evidence {
  font-family: var(--serif); font-size: 14px; color: var(--text);
  background: var(--bg); padding: 6px 9px; border-radius: 4px;
  margin: 6px 0 4px; white-space: pre-wrap; word-break: break-all;
}
.suggestion { font-size: 13px; color: var(--text-dim); }
</style>
