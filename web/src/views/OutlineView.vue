<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const loading = ref(false)
const rows = ref([])
const planForm = ref({ start_ch: null, count: 5 })
const planning = ref(false)
const warnings = ref([])
const alerts = ref([])

const expanded = ref(null)
const edit = ref(null)
const scenesText = ref('')
const scenesError = ref('')
const saving = ref(false)

const statusKind = {
  draft: 'warn',
  planned: 'info',
  writing: 'gold',
  drafted: 'ok',
  reviewed: 'ok',
  final: 'ok',
  unknown: '',
}

const stats = computed(() => {
  const out = { total: rows.value.length, planned: 0, draft: 0, done: 0 }
  for (const r of rows.value) {
    if (r.status === 'planned') out.planned += 1
    else if (r.status === 'draft') out.draft += 1
    else if (r.status === 'drafted' || r.status === 'reviewed' || r.status === 'final') out.done += 1
  }
  return out
})

async function load() {
  if (!dir.value) return
  loading.value = true
  try {
    const chs = await store.loadChapters(dir.value)
    const list = (chs || []).slice(0, 100)
    const outlines = await Promise.all(
      list.map((c) => api.outline(dir.value, c.ch).catch(() => null)),
    )
    rows.value = list.map((c, i) => {
      const o = outlines[i]
      if (o) {
        return {
          ch: o.ch,
          title: o.title || '',
          pov: o.pov || '',
          hook: o.hook || '',
          status: o.status || 'draft',
          scenes: (o.scenes || []).length,
        }
      }
      return {
        ch: c.ch,
        title: c.title || '',
        pov: '',
        hook: '',
        status: c.status || 'unknown',
        scenes: 0,
        missing: true,
      }
    })
  } catch (e) {
    store.toast(`章纲列表加载失败：${e.message}`, 'err')
  } finally {
    loading.value = false
  }
}

function parseScenes() {
  const raw = scenesText.value.trim()
  if (!raw) return []
  const parsed = JSON.parse(raw)
  if (!Array.isArray(parsed)) throw new Error('scenes 必须是数组')
  return parsed
}

async function expand(row) {
  if (expanded.value === row.ch) {
    expanded.value = null
    edit.value = null
    return
  }
  expanded.value = row.ch
  scenesError.value = ''
  try {
    const o = await api.outline(dir.value, row.ch)
    edit.value = {
      ch: o.ch,
      title: o.title || '',
      pov: o.pov || 'mc',
      hook: o.hook || '',
      beat: o.beat || '',
      notes: o.notes || '',
      status: o.status || 'draft',
      words_min: (o.target_words || [2000, 2400])[0] ?? 2000,
      words_max: (o.target_words || [2000, 2400])[1] ?? 2400,
      must_include: (o.must_include || []).join('\n'),
    }
    scenesText.value = JSON.stringify(o.scenes || [], null, 2)
  } catch (e) {
    if (e.status === 404) {
      edit.value = {
        ch: row.ch,
        title: row.title || '',
        pov: 'mc',
        hook: '',
        beat: '',
        notes: '',
        status: 'draft',
        words_min: 2000,
        words_max: 2400,
        must_include: '',
      }
      scenesText.value = '[]'
    } else {
      store.toast(`读取第 ${row.ch} 章章纲失败：${e.message}`, 'err')
    }
  }
}

async function save() {
  if (!edit.value) return
  let scenes
  try {
    scenes = parseScenes()
    scenesError.value = ''
  } catch (e) {
    scenesError.value = `scenes JSON 解析失败：${e.message}`
    store.toast(scenesError.value, 'err')
    return
  }
  saving.value = true
  try {
    const patch = {
      title: edit.value.title,
      pov: edit.value.pov,
      hook: edit.value.hook,
      beat: edit.value.beat,
      notes: edit.value.notes,
      target_words: [Number(edit.value.words_min) || 0, Number(edit.value.words_max) || 0],
      scenes,
      must_include: (edit.value.must_include || '')
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
    }
    const o = await api.putOutline(dir.value, edit.value.ch, patch)
    syncRow(o)
    store.toast(`第 ${o.ch} 章章纲已保存`, 'ok')
  } catch (e) {
    store.toast(`保存失败：${e.message}`, 'err')
  } finally {
    saving.value = false
  }
}

async function confirmOne() {
  if (!edit.value) return
  try {
    const r = await api.confirmOutline(dir.value, edit.value.ch)
    edit.value.status = r.status
    const row = rows.value.find((x) => x.ch === edit.value.ch)
    if (row) row.status = r.status
    store.toast(`第 ${r.ch} 章已确认（draft → planned）`, 'ok')
  } catch (e) {
    store.toast(`确认失败：${e.message}`, 'err')
  }
}

function syncRow(o) {
  const row = rows.value.find((x) => x.ch === o.ch)
  const next = {
    ch: o.ch,
    title: o.title || '',
    pov: o.pov || '',
    hook: o.hook || '',
    status: o.status || 'draft',
    scenes: (o.scenes || []).length,
  }
  if (row) Object.assign(row, next)
  else rows.value = [...rows.value, next].sort((a, b) => a.ch - b.ch)
}

async function runPlan() {
  planning.value = true
  warnings.value = []
  try {
    const payload = { count: Number(planForm.value.count) || 5, save: true }
    if (planForm.value.start_ch) payload.start_ch = Number(planForm.value.start_ch)
    const r = await api.plan(dir.value, payload)
    warnings.value = r.warnings || []
    alerts.value = r.alerts || []
    for (const c of r.chapters || []) syncRow(c)
    store.toast(`已生成 ${(r.chapters || []).length} 章草案（写入 ${r.saved} 条）`, 'ok')
    await load()
  } catch (e) {
    store.toast(`生成章纲失败：${e.message}`, 'err')
  } finally {
    planning.value = false
  }
}

onMounted(load)
watch(dir, load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>章纲工作台</h1>
        <p class="dim small">
          批量生成草案 → 逐章校对 → 确认（draft → planned）后才可进入写作流水线
        </p>
      </div>
      <div class="row small dim">
        <span class="mono">{{ stats.total }}</span> 章 ·
        <span class="mono">{{ stats.draft }}</span> 草案 ·
        <span class="mono">{{ stats.planned }}</span> 已确认 ·
        <span class="mono">{{ stats.done }}</span> 已写
      </div>
    </div>

    <div class="card plan-card">
      <div class="row wrap">
        <label class="field">
          起始章
          <input v-model.number="planForm.start_ch" type="number" min="1" placeholder="自动接续" />
        </label>
        <label class="field">
          生成章数
          <input v-model.number="planForm.count" type="number" min="1" max="20" />
        </label>
        <span class="spacer" />
        <button class="primary" :disabled="planning" @click="runPlan">
          {{ planning ? '生成中…（模型可能较慢）' : '生成章纲草案' }}
        </button>
      </div>
      <p class="faint small bar-note">
        生成会落盘为 <code class="mono">outline/*.json</code>，状态 <code class="mono">draft</code>；未确认的章不会被写作流水线选中。
      </p>
    </div>

    <div v-if="warnings.length" class="block warn-block">
      <strong class="small">Planner 警告（{{ warnings.length }}）</strong>
      <ul>
        <li v-for="(w, i) in warnings" :key="i" class="small">{{ w }}</li>
      </ul>
    </div>

    <div v-if="alerts.length" class="block err-block">
      <strong class="small">伏笔报警（{{ alerts.length }}）</strong>
      <ul>
        <li v-for="a in alerts" :key="a.id" class="small">
          <span class="badge" :class="a.level === 'overdue' ? 'err' : 'warn'">{{ a.level }}</span>
          <code class="mono">{{ a.id }}</code>
          {{ a.desc }}
          <span class="faint">
            · 截止 <span class="mono">{{ a.deadline_ch }}</span>
            <template v-if="a.level === 'overdue'">· 已超 <span class="mono">{{ a.overdue_by }}</span> 章</template>
            <template v-else>· 还剩 <span class="mono">{{ a.remaining }}</span> 章</template>
          </span>
          <span v-if="a.suggestion" class="dim"> — {{ a.suggestion }}</span>
        </li>
      </ul>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!rows.length" class="empty">
      还没有章纲。点上方「生成章纲草案」开始，或先到书架确认这本书已有章节。
    </div>

    <div v-else class="list">
      <div v-for="r in rows" :key="r.ch" class="item-wrap">
        <div class="item" :class="{ open: expanded === r.ch }" @click="expand(r)">
          <span class="ch mono">#{{ r.ch }}</span>
          <span class="title">{{ r.title || '（未命名）' }}</span>
          <span v-if="r.pov" class="badge">POV {{ r.pov }}</span>
          <span class="badge" :class="statusKind[r.status] || ''">{{ r.status }}</span>
          <span class="faint small mono">{{ r.scenes }} 场景</span>
          <span class="hook faint small">{{ r.hook || '（无钩子）' }}</span>
          <span class="spacer" />
          <span class="faint small">{{ expanded === r.ch ? '收起 ▴' : '展开 ▾' }}</span>
        </div>

        <div v-if="expanded === r.ch && edit" class="editor card">
          <div class="grid">
            <label class="field">
              标题
              <input v-model="edit.title" placeholder="本章标题" />
            </label>
            <label class="field">
              POV 视角角色
              <input v-model="edit.pov" placeholder="mc" />
            </label>
            <label class="field">
              字数下限
              <input v-model.number="edit.words_min" type="number" />
            </label>
            <label class="field">
              字数上限
              <input v-model.number="edit.words_max" type="number" />
            </label>
          </div>
          <label class="field">
            章末钩子
            <input v-model="edit.hook" placeholder="留给下一章的未解信息" />
          </label>
          <label class="field">
            Beat（结构位置）
            <input v-model="edit.beat" placeholder="例如：冲突起势 / 爽点爆发 / 卷末反转" />
          </label>
          <label class="field">
            scenes（JSON 数组）
            <textarea v-model="scenesText" class="mono-input" rows="8" spellcheck="false" />
          </label>
          <p v-if="scenesError" class="err-text small">{{ scenesError }}</p>
          <label class="field">
            must_include（每行一条）
            <textarea v-model="edit.must_include" rows="3" spellcheck="false" />
          </label>
          <label class="field">
            备注
            <textarea v-model="edit.notes" rows="2" />
          </label>
          <div class="row">
            <span class="badge" :class="statusKind[edit.status] || ''">{{ edit.status }}</span>
            <span class="spacer" />
            <button class="ghost sm" @click="expand(r)">取消</button>
            <button :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
            <button class="primary" :disabled="edit.status === 'planned'" @click="confirmOne">
              {{ edit.status === 'planned' ? '已确认' : '确认（→ planned）' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 1200px; }


.plan-card { margin-bottom: 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.bar-note { margin: 8px 0 0; }
.field input[type='number'] { max-width: 140px; }

.block { border-radius: var(--radius); padding: 9px 12px; margin-bottom: 12px; border: 1px solid var(--border); border-left-width: 3px; }
.block ul { margin: 6px 0 0; padding-left: 18px; }
.block li { margin: 2px 0; }
.warn-block { background: rgba(217, 164, 65, 0.08); border-color: rgba(217, 164, 65, 0.35); border-left-color: var(--warn); }
.err-block { background: rgba(217, 83, 79, 0.08); border-color: rgba(217, 83, 79, 0.35); border-left-color: var(--err); }

.list { display: flex; flex-direction: column; gap: 6px; }
.item {
  display: flex; align-items: center; gap: 8px;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 7px 11px; cursor: pointer;
}
.item:hover { border-color: var(--border-strong); }
.item.open { border-color: var(--accent); background: var(--accent-soft); }
.ch { color: var(--accent); font-size: 12px; min-width: 42px; }
.title { font-weight: 600; font-size: 14px; }
.hook { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.editor { margin: 6px 0 2px; padding: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 12px; }
.mono-input { font-family: var(--mono); font-size: 12px; line-height: 1.6; }
.err-text { color: var(--err); margin: 2px 0 6px; }
</style>
