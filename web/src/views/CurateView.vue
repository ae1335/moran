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
const pack = ref(null)
const previews = ref({})
const warnings = ref([])
const selected = ref(new Set())
const running = ref(false)
const confirming = ref(false)
const result = ref(null)

async function loadList() {
  try {
    chapters.value = await store.loadChapters(dir.value)
  } catch (e) {
    store.toast(`章节列表加载失败：${e.message}`, 'err')
  }
}

async function open(ch) {
  currentCh.value = ch
  pack.value = null
  previews.value = {}
  result.value = null
  router.replace(`/b/${encodeURIComponent(dir.value)}/curate/${ch}`)
  try {
    const p = await api.curatePack(dir.value, ch)
    pack.value = p
    selected.value = new Set((p.proposals || []).map((x) => x.id))
    await loadPreviews(ch)
  } catch (e) {
    if (e.status !== 404) store.toast(`读取提案包失败：${e.message}`, 'err')
  }
}

async function loadPreviews(ch) {
  // previews 在生成提案时才有；已存在的包用服务端返回的 previews 为空，退化为展示 patch
  try {
    const p = await api.curatePack(dir.value, ch)
    previews.value = p.previews || {}
    pack.value = p
  } catch { /* ignore */ }
}

async function generate() {
  if (!currentCh.value) return
  running.value = true
  try {
    const r = await api.curate(dir.value, currentCh.value, true)
    pack.value = { ch: currentCh.value, proposals: r.proposals }
    previews.value = r.previews || {}
    warnings.value = r.warnings || []
    selected.value = new Set(r.proposals.map((x) => x.id))
    store.toast(`产出 ${r.proposals.length} 条提案`, 'ok')
  } catch (e) {
    store.toast(`沉淀失败：${e.message}`, 'err')
  } finally {
    running.value = false
  }
}

function toggle(id) {
  const s = new Set(selected.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selected.value = s
}

function selectAll(v) {
  selected.value = v ? new Set((pack.value?.proposals || []).map((x) => x.id)) : new Set()
}

async function confirm() {
  if (!currentCh.value || !pack.value) return
  confirming.value = true
  try {
    const ids = [...selected.value]
    const all = (pack.value.proposals || []).map((x) => x.id)
    const r = await api.curateConfirm(dir.value, currentCh.value, ids.length === all.length ? null : ids, true)
    result.value = r
    store.toast(`已落盘 ${r.applied.length} 项`, 'ok')
    if (r.warnings?.length) r.warnings.forEach((w) => store.toast(w, 'err'))
    await loadList()
  } catch (e) {
    store.toast(`确认失败：${e.message}`, 'err')
  } finally {
    confirming.value = false
  }
}

function diffLines(diff) {
  if (!diff) return []
  return diff.split('\n').map((line) => {
    let cls = 'ctx'
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'hunk'
    else if (line.startsWith('@@')) cls = 'hunk'
    else if (line.startsWith('+')) cls = 'add'
    else if (line.startsWith('-')) cls = 'del'
    return { cls, text: line }
  })
}

function patchText(p) {
  if (!p) return ''
  if (p.lines) return p.lines.join('\n')
  if (p.patch) return JSON.stringify(p.patch, null, 2)
  if (p.content) return p.content
  return ''
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
  <div class="curate-layout">
    <aside class="ch-list">
      <div class="ch-head"><span class="small dim">沉淀章节</span><span class="spacer" /><span class="badge">{{ chapters.length }}</span></div>
      <div class="ch-scroll">
        <div v-for="c in chapters" :key="c.ch" class="ch-item" :class="{ active: currentCh === c.ch }" @click="open(c.ch)">
          <div class="row">
            <span class="mono small">{{ String(c.ch).padStart(4, '0') }}</span>
            <span class="trunc">{{ c.title || '（未命名）' }}</span>
          </div>
          <div class="row small faint">
            <span class="spacer" />
            <span class="badge" :class="{ ok: c.status === 'final' }">{{ c.status }}</span>
          </div>
        </div>
        <div v-if="!chapters.length" class="empty">还没有章节</div>
      </div>
    </aside>

    <section class="main">
      <div v-if="!currentCh" class="empty">从左侧选一章</div>
      <template v-else>
        <div class="head">
          <div class="row">
            <strong>第 {{ currentCh }} 章 · Curator 提案</strong>
            <span class="badge gold">真相文件唯一写入口</span>
            <span v-if="pack" class="badge">{{ pack.proposals?.length || 0 }} 条</span>
          </div>
          <div class="row">
            <button v-if="pack?.proposals?.length" class="ghost sm" @click="selectAll(true)">全选</button>
            <button v-if="pack?.proposals?.length" class="ghost sm" @click="selectAll(false)">全不选</button>
            <button class="ghost sm" :disabled="running" @click="generate">
              {{ running ? '沉淀中…' : pack ? '重新生成提案' : '生成提案' }}
            </button>
            <button class="primary sm" :disabled="confirming || !pack?.proposals?.length" @click="confirm">
              {{ confirming ? '落盘中…' : `确认落盘 (${selected.size})` }}
            </button>
          </div>
        </div>

        <div v-if="warnings.length" class="warn-box">
          <div v-for="(w, i) in warnings" :key="i" class="small">⚠ {{ w }}</div>
        </div>

        <div v-if="!pack" class="empty">
          还没有提案包。点右上角「生成提案」——Curator 只会产出 diff，不会直接改文件。
        </div>

        <div v-if="pack" class="props">
          <div v-for="p in pack.proposals" :key="p.id" class="prop" :class="{ unchecked: !selected.has(p.id), accepted: p.accepted === true }">
            <div class="row prop-head">
              <input type="checkbox" style="width: auto" :checked="selected.has(p.id)" @change="toggle(p.id)" />
              <span class="badge" :class="p.op === 'create' ? 'ok' : p.op === 'patch' ? 'warn' : 'info'">{{ p.op }}</span>
              <code class="mono file">{{ p.file }}</code>
              <span class="spacer" />
              <span v-if="p.accepted === true" class="badge ok">已落盘</span>
              <span v-if="p.accepted === false" class="badge">未采纳</span>
              <span v-if="p.conflict" class="badge err">{{ p.conflict }}</span>
            </div>
            <div v-if="p.reason" class="small faint reason">{{ p.reason }}</div>

            <div v-if="previews[p.id]" class="diff">
              <span v-for="(l, i) in diffLines(previews[p.id])" :key="i" :class="l.cls">{{ l.text }}</span>
            </div>
            <pre v-else class="fallback">{{ patchText(p) }}</pre>
          </div>
        </div>

        <div v-if="result" class="card result-box">
          <div class="row"><strong class="small">落盘结果</strong></div>
          <div class="small dim">已应用 {{ result.applied.length }} 项，跳过 {{ result.skipped.length }} 项</div>
          <div v-if="result.commit" class="small mono faint">
            git: {{ result.commit.ok ? (result.commit.sha?.slice(0, 8) || 'noop') : '失败 ' + result.commit.error }}
          </div>
          <!-- 缺陷 #126：commit 成功但 tag 没打上时，时光机查不到这一章，
               界面上必须说，不能照显示一个不存在的 ch/0042。 -->
          <div v-if="result.commit && result.commit.tag_error" class="small warn-text">
            ⚠ 打标失败（时光机查不到该章）：{{ result.commit.tag_error }}
          </div>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.curate-layout { display: flex; height: 100%; min-height: 0; }
.ch-list { width: 220px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--bg-panel); display: flex; flex-direction: column; }
.ch-head { display: flex; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.ch-scroll { flex: 1; overflow-y: auto; padding: 4px; }
.ch-item { padding: 6px 8px; border-radius: var(--radius); cursor: pointer; }
.ch-item:hover { background: var(--bg-hover); }
.ch-item.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.trunc { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.main { flex: 1; min-width: 0; overflow-y: auto; padding: 14px 20px; }
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
.warn-box { background: rgba(217, 164, 65, 0.1); border-left: 2px solid var(--warn); padding: 6px 10px; margin-bottom: 10px; }
.props { display: flex; flex-direction: column; gap: 10px; }
.prop {
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 9px 11px;
}
.prop.unchecked { opacity: 0.5; }
.prop.accepted { border-color: rgba(76, 175, 125, 0.5); }
.prop-head { margin-bottom: 5px; }
.file { font-size: 12px; color: var(--accent); }
.reason { margin-bottom: 6px; }
.fallback {
  background: var(--bg); padding: 8px; border-radius: 4px; margin: 0;
  font-size: 11px; font-family: var(--mono); max-height: 220px; overflow: auto;
  white-space: pre-wrap; color: var(--text-dim);
}
.result-box { margin-top: 12px; }
.warn-text { color: var(--warn, #d9a441); }
</style>
