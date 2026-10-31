<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const KINDS = [
  { key: 'character', label: '角色', hint: '姓 + 名，2–3 字，避开已有角色' },
  { key: 'skill', label: '功法/招式', hint: '动词 + 意象，两字' },
  { key: 'sect', label: '宗门/势力', hint: '前缀 + 建制后缀' },
  { key: 'place', label: '地名', hint: '地形修饰 + 地貌' },
]

const kind = ref('character')
const count = ref(10)
const seed = ref('')
const gender = ref('')
const save = ref(false)
const generating = ref(false)
const latest = ref(null)
const pool = ref([])
const loadingPool = ref(false)
const copied = ref('')

const kindLabel = (k) => KINDS.find((x) => x.key === k)?.label || k

const currentHint = computed(() => KINDS.find((x) => x.key === kind.value)?.hint || '')

async function loadPool() {
  if (!dir.value) return
  loadingPool.value = true
  try {
    const r = await api.listNames(dir.value)
    pool.value = r.names || []
  } catch (e) {
    store.toast(`词池加载失败：${e.message}`, 'err')
    pool.value = []
  } finally {
    loadingPool.value = false
  }
}

async function generate() {
  generating.value = true
  try {
    const r = await api.generateNames(dir.value, {
      kind: kind.value,
      count: Number(count.value) || 10,
      seed: seed.value === '' ? null : Number(seed.value),
      gender: gender.value,
      save: save.value,
    })
    latest.value = r
    const n = r.names?.length || 0
    store.toast(`生成 ${n} 个${kindLabel(kind.value)}名字${save.value ? '，已写入词池' : ''}`, 'ok')
    if (save.value) await loadPool()
  } catch (e) {
    store.toast(`生成失败：${e.message}`, 'err')
    latest.value = null
  } finally {
    generating.value = false
  }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = text
    setTimeout(() => (copied.value = ''), 1200)
  } catch {
    store.toast('复制失败：浏览器拒绝访问剪贴板', 'err')
  }
}

async function copyAll() {
  const names = latest.value?.names || []
  if (!names.length) return
  await copy(names.join('、'))
}

onMounted(loadPool)
watch(dir, () => {
  latest.value = null
  pool.value = []
  loadPool()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>起名</h1>
        <p class="dim small">
          规则生成 · 同一 seed 结果可复现 · 产物进 <code class="mono">materials/dictionary/names-pool.md</code>
        </p>
      </div>
      <div class="row">
        <button class="ghost sm" :disabled="loadingPool" @click="loadPool">
          {{ loadingPool ? '刷新中…' : '刷新词池' }}
        </button>
      </div>
    </div>

    <div class="card">
      <div class="row wrap">
        <div class="field">
          <label class="small dim">类别</label>
          <div class="kinds">
            <button
              v-for="k in KINDS"
              :key="k.key"
              class="ghost sm"
              :class="{ primary: kind === k.key }"
              @click="kind = k.key"
            >
              {{ k.label }}
            </button>
          </div>
        </div>
        <div class="field">
          <label class="small dim">数量</label>
          <input v-model="count" type="number" min="1" max="50" style="width: 90px" />
        </div>
        <div class="field">
          <label class="small dim">seed（留空随机）</label>
          <input v-model="seed" type="number" placeholder="随机" style="width: 110px" />
        </div>
        <div v-if="kind === 'character'" class="field">
          <label class="small dim">性别倾向</label>
          <select v-model="gender" style="width: 100px">
            <option value="">不限</option>
            <option value="male">偏男</option>
            <option value="female">偏女</option>
          </select>
        </div>
        <div class="field">
          <label class="small dim">写入词池</label>
          <label class="row small" style="gap: 4px">
            <input v-model="save" type="checkbox" style="width: auto" />
            <span class="faint">勾选后追加</span>
          </label>
        </div>
        <span class="spacer" />
        <button class="primary" :disabled="generating || !dir" @click="generate">
          {{ generating ? '生成中…' : '生成' }}
        </button>
      </div>
      <p class="dim small" style="margin: 8px 0 0">{{ currentHint }}</p>
    </div>

    <div v-if="latest" class="card">
      <div class="row">
        <strong class="small">本次结果</strong>
        <span class="badge">{{ kindLabel(latest.kind) }}</span>
        <span class="badge info mono">seed {{ latest.seed }}</span>
        <span class="spacer" />
        <button class="ghost sm" @click="copyAll">复制全部</button>
      </div>
      <div class="names">
        <button
          v-for="c in latest.candidates"
          :key="c.name"
          class="name-chip"
          :class="{ copied: copied === c.name }"
          :title="c.note || ''"
          @click="copy(c.name)"
        >
          {{ c.name }}
        </button>
      </div>
      <p v-if="latest.skipped?.length" class="dim small" style="margin: 6px 0 0">
        跳过 {{ latest.skipped.length }} 个重名：<span class="mono">{{ latest.skipped.join('、') }}</span>
      </p>
      <p class="faint small" style="margin: 6px 0 0">点名字可复制单个；点「复制全部」拿一行顿号分隔的列表。</p>
    </div>

    <div class="card">
      <div class="row">
        <strong class="small">词池</strong>
        <span class="badge">{{ pool.length }}</span>
        <span class="spacer" />
        <span class="faint small">不是真相源，删改随意</span>
      </div>
      <div v-if="!pool.length" class="empty">还没有生成过名字。选好类别点「生成」，勾选「写入词池」才会累积。</div>
      <div v-else class="pool">
        <div v-for="(n, i) in pool" :key="i" class="pool-row">
          <span class="mono pool-name">{{ n.name }}</span>
          <span class="badge">{{ n.kind }}</span>
          <span class="faint small trunc">{{ n.note || '—' }}</span>
          <span class="spacer" />
          <button class="ghost sm" @click="copy(n.name)">复制</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.row.wrap { flex-wrap: wrap; gap: 14px; align-items: flex-end; }
.field { display: flex; flex-direction: column; gap: 4px; }
.kinds { display: flex; gap: 6px; }
.names { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.name-chip {
  font-family: var(--serif);
  font-size: 18px;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.name-chip:hover { border-color: var(--accent); color: var(--accent); }
.name-chip.copied { border-color: var(--ok, #4caf7d); color: var(--ok, #4caf7d); }
.pool { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; }
.pool-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 9px;
  border: 1px solid var(--line);
  border-radius: 5px;
}
.pool-name { font-size: 15px; color: var(--text); }
.trunc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 340px; }
</style>
