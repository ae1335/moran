<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const loading = ref(false)
const list = ref([])

const title = ref('')
const text = ref('')
const useModel = ref(true)
const importing = ref(false)

const currentId = ref(null)
const detail = ref(null)
const loadingDetail = ref(false)
const activeFile = ref('')
const showImport = ref(false)

const fileNames = computed(() => Object.keys(detail.value?.files || {}))

const activeContent = computed(() => {
  const f = detail.value?.files || {}
  return f[activeFile.value] ?? ''
})

const prettyContent = computed(() => {
  const raw = activeContent.value
  if (activeFile.value.endsWith('.json')) {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }
  return raw
})

async function loadList() {
  if (!dir.value) return
  loading.value = true
  try {
    const r = await api.benchmarks(dir.value)
    list.value = r.benchmarks || []
  } catch (e) {
    store.toast(`对标书列表加载失败：${e.message}`, 'err')
    list.value = []
  } finally {
    loading.value = false
  }
}

async function openDetail(id) {
  currentId.value = id
  loadingDetail.value = true
  detail.value = null
  activeFile.value = ''
  try {
    const r = await api.benchmarkDetail(dir.value, id)
    detail.value = r
    const names = Object.keys(r.files || {})
    activeFile.value = names.includes('structure.json') ? 'structure.json' : names[0] || ''
  } catch (e) {
    store.toast(`读取拆书产物失败：${e.message}`, 'err')
  } finally {
    loadingDetail.value = false
  }
}

async function doImport() {
  const t = title.value.trim()
  const body = text.value.trim()
  if (!t) {
    store.toast('请填写标题', 'err')
    return
  }
  if (body.length < 200) {
    store.toast('正文太短（至少 200 字），拆书需要足够样本', 'err')
    return
  }
  importing.value = true
  try {
    const r = await api.benchmarkImport(dir.value, t, body, useModel.value)
    store.toast(`已拆解《${r.title}》：${r.chapters} 章 / ${r.words} 字`, 'ok')
    // 缺陷 #111：拆书经常是「半成功」——模型那一步挂了，只剩启发式产物。
    // 服务端把话说了（Deconstructor 的告警字段 + 响应体带回），但原样只报章数字数，
    // 作者会以为这是 AI 深度拆书的结果，照着它调节奏。
    ;(r.warnings || []).forEach((w) => store.toast(w, 'warn'))
    title.value = ''
    text.value = ''
    showImport.value = false
    await loadList()
    await openDetail(r.id)
  } catch (e) {
    store.toast(`拆书失败：${e.message}`, 'err')
  } finally {
    importing.value = false
  }
}

function onFile(ev) {
  const file = ev.target.files && ev.target.files[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    text.value = String(reader.result || '')
    if (!title.value.trim()) title.value = file.name.replace(/\.[^.]+$/, '')
    store.toast(`已读入 ${file.name}（${text.value.length} 字符）`, 'ok')
  }
  reader.onerror = () => store.toast('文件读取失败', 'err')
  reader.readAsText(file, 'utf-8')
  ev.target.value = ''
}

onMounted(async () => {
  await loadList()
  // 已拆过的书自动展开第一本：否则右侧永远是「从左选一本」（第一次进来像坏了）
  if (!currentId.value && list.value.length) openDetail(list.value[0].id)
})
watch(dir, async () => {
  currentId.value = null
  detail.value = null
  await loadList()
  if (list.value.length) openDetail(list.value[0].id)
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>拆书台</h1>
        <p class="dim small">
          导入对标书 → 产出 <code class="mono">structure.json</code> /
          <code class="mono">pacing.md</code> /
          <code class="mono">hooks.md</code> /
          <code class="mono">template.json</code>
        </p>
      </div>
      <button class="primary" @click="showImport = !showImport">
        {{ showImport ? '取消导入' : '+ 导入对标书' }}
      </button>
    </div>

    <div v-if="showImport" class="card import-card">
      <div class="grid">
        <label class="field">
          标题
          <input v-model="title" placeholder="对标书名" />
        </label>
        <label class="field row" style="gap: 6px; align-items: flex-end">
          <span class="row small dim" style="gap: 4px">
            <input v-model="useModel" type="checkbox" style="width: auto" /> 用模型精拆
          </span>
          <span class="spacer" />
          <span class="faint small">或上传 txt：</span>
          <input type="file" accept=".txt,.md,text/plain" class="file" @change="onFile" />
        </label>
      </div>
      <label class="field">
        正文（粘贴全文，按「第N章」自动切分）
        <textarea v-model="text" rows="10" placeholder="把对标书全文粘贴到这里…" />
      </label>
      <div class="row">
        <span class="faint small mono">{{ text.length }} 字符</span>
        <span class="spacer" />
        <button class="primary" :disabled="importing" @click="doImport">
          {{ importing ? '拆解中…（模型可能较慢）' : '开始拆解' }}
        </button>
      </div>
    </div>

    <div class="layout">
      <aside class="blist">
        <div class="blist-head">
          <strong class="small">已拆书目</strong>
          <span class="badge">{{ list.length }}</span>
          <span class="spacer" />
          <button class="ghost sm" :disabled="loading" @click="loadList">刷新</button>
        </div>
        <div class="blist-scroll">
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!list.length" class="empty">还没有对标书，导入一本试试</div>
          <div
            v-for="b in list"
            :key="b.id"
            class="bitem"
            :class="{ active: currentId === b.id }"
            @click="openDetail(b.id)"
          >
            <div class="btitle">{{ b.title }}</div>
            <div class="row small faint">
              <span class="mono">{{ b.chapters }}</span> 章 ·
              <span class="mono">{{ b.words }}</span> 字
            </div>
            <div class="mono bid">{{ b.id }}</div>
          </div>
        </div>
      </aside>

      <section class="detail">
        <div v-if="loadingDetail" class="empty">读取产物中…</div>
        <div v-else-if="!detail" class="empty">从左侧选一本已拆书目查看产物</div>
        <template v-else>
          <div class="row wrap tabs">
            <button
              v-for="f in fileNames"
              :key="f"
              class="ghost sm"
              :class="{ primary: activeFile === f }"
              @click="activeFile = f"
            >
              {{ f }}
            </button>
            <span class="spacer" />
            <span class="mono faint small">{{ detail.id }}</span>
          </div>
          <div v-if="!fileNames.length" class="empty">该目录下没有产物文件</div>
          <pre v-else class="dump">{{ prettyContent }}</pre>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 1300px; }


.import-card { margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.file { max-width: 220px; font-size: 11px; padding: 3px; }

.layout { display: flex; gap: 12px; align-items: flex-start; }
.blist { width: 240px; flex-shrink: 0; background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius); display: flex; flex-direction: column; max-height: 74vh; }
.blist-head { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.blist-scroll { overflow-y: auto; padding: 4px; }
.bitem { padding: 7px 8px; border-radius: var(--radius); cursor: pointer; }
.bitem:hover { background: var(--bg-hover); }
.bitem.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.btitle { font-size: 13.5px; font-weight: 600; }
.bid { font-size: 10px; color: var(--text-faint); margin-top: 2px; }

.detail { flex: 1; min-width: 0; background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px; }
.tabs { gap: 6px; margin-bottom: 10px; }
.dump {
  margin: 0; padding: 10px 12px; background: var(--bg); border-radius: var(--radius);
  font-family: var(--mono); font-size: 12px; line-height: 1.7;
  color: var(--text-dim); white-space: pre-wrap; word-break: break-word;
  max-height: 64vh; overflow: auto;
}
</style>
