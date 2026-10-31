<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const loadingFiles = ref(false)
const files = ref([])
const filter = ref('')

// 当前文件
const current = ref(null) // { rel, path }
const content = ref('')
const original = ref('')
const loadingFile = ref(false)
const saving = ref(false)

// git
const loadingLog = ref(false)
const log = ref([])
const tags = ref([])
const showRev = ref(null)
const histContent = ref('')
const loadingHist = ref(false)

const dirty = computed(() => content.value !== original.value)

const shownFiles = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const list = files.value
  if (!q) return list
  return list.filter((f) => f.path.toLowerCase().includes(q))
})

/** truthList 返回的 path 是「相对书根」，如 truth/constitution.md；truthRead 要的是相对 truth/ 的路径 */
function relToTruthPath(rel) {
  return rel.startsWith('truth/') ? rel.slice('truth/'.length) : rel
}

async function loadFiles() {
  if (!dir.value) return
  loadingFiles.value = true
  try {
    const r = await api.truthList(dir.value)
    files.value = (r.files || []).map((f) => ({
      ...f,
      rel: f.path,
      short: f.path.startsWith('truth/') ? f.path.slice('truth/'.length) : f.path,
    }))
  } catch (e) {
    store.toast(`真相源列表加载失败：${e.message}`, 'err')
    files.value = []
  } finally {
    loadingFiles.value = false
  }
}

async function loadLog() {
  if (!dir.value) return
  loadingLog.value = true
  try {
    const r = await api.gitLog(dir.value, 30)
    log.value = r.log || []
    tags.value = r.tags || []
  } catch (e) {
    store.toast(`提交历史加载失败：${e.message}`, 'err')
    log.value = []
    tags.value = []
  } finally {
    loadingLog.value = false
  }
}

async function openFile(f) {
  if (dirty.value && !window.confirm('当前文件有未保存改动，确定切换？')) return
  current.value = f
  showRev.value = null
  histContent.value = ''
  loadingFile.value = true
  try {
    const r = await api.truthRead(dir.value, relToTruthPath(f.rel))
    content.value = r.content || ''
    original.value = content.value
  } catch (e) {
    store.toast(`读取 ${f.short} 失败：${e.message}`, 'err')
    content.value = ''
    original.value = ''
  } finally {
    loadingFile.value = false
  }
}

async function save() {
  if (!current.value) return
  saving.value = true
  try {
    const r = await api.truthWrite(dir.value, relToTruthPath(current.value.rel), content.value, true)
    original.value = content.value
    // 缺陷 #107：git 提交失败时不能照说「已提交 git」——
    // 服务端把 commit 结果放在响应体里（commit.ok / commit.error），说谎等于
    // 让作者以为真相源进了版本库，时光机上却查不到。
    const git = r.commit
    // 缺陷 #126：提交成功但打标失败（残留 lock / 只读挂载）时 tag 是空的，
    // 时光机按 ch/0042 索引，查不到就等于这一章没进时光机——必须说出来。
    const tagTail = git && git.ok && git.tag_error ? `，⚠ 打标失败（时光机查不到该章）：${git.tag_error}` : ''
    const tail = !git ? '' : git.ok ? '，已提交 git' + tagTail : `，⚠ git 未提交：${git.error || '未知原因'}`
    store.toast(`已保存 ${current.value.short}（${r.bytes} 字节${tail}）`, git && (!git.ok || git.tag_error) ? 'warn' : 'ok')
    await loadLog()
  } catch (e) {
    store.toast(`保存失败：${e.message}`, 'err')
  } finally {
    saving.value = false
  }
}

async function viewAtCommit(c) {
  if (!current.value) {
    store.toast('先选一个文件再看历史版本', 'err')
    return
  }
  showRev.value = c
  loadingHist.value = true
  histContent.value = ''
  try {
    const r = await api.gitShow(dir.value, c.sha, current.value.rel)
    histContent.value = r.content || ''
  } catch (e) {
    store.toast(`读取 ${c.short} 版本失败：${e.message}`, 'err')
    histContent.value = ''
  } finally {
    loadingHist.value = false
  }
}

function restoreHistory() {
  if (histContent.value === '') return
  content.value = histContent.value
  store.toast('已把历史版本载入编辑器，记得点「保存」', 'ok')
}

onMounted(async () => {
  await Promise.all([loadFiles(), loadLog()])
  // 打开页面就自动选中第一个文件：否则右侧永远是「从左侧选一个文件开始编辑」，
  // 第一次进来的人会以为加载失败（实测体感问题）。
  if (!current.value && files.value.length) {
    const preferred =
      files.value.find((f) => f.short === 'constitution.md') || files.value[0]
    openFile(preferred)
  }
})
watch(dir, async () => {
  current.value = null
  content.value = ''
  original.value = ''
  await Promise.all([loadFiles(), loadLog()])
})
</script>

<template>
  <div class="truth-layout">
    <aside class="files">
      <div class="files-head">
        <strong class="small">真相源</strong>
        <span class="badge">{{ files.length }}</span>
      </div>
      <div class="files-search">
        <input v-model="filter" placeholder="过滤文件名…" class="small" />
      </div>
      <div class="files-scroll">
        <div v-if="loadingFiles" class="empty">加载中…</div>
        <div v-else-if="!files.length" class="empty">truth/ 下没有文件</div>
        <div
          v-for="f in shownFiles"
          :key="f.rel"
          class="file-item"
          :class="{ active: current?.rel === f.rel }"
          @click="openFile(f)"
        >
          <span class="fname">{{ f.short }}</span>
          <span class="fsize mono faint">{{ f.size }}</span>
        </div>
      </div>
    </aside>

    <section class="editor-pane">
      <div class="pane-head">
        <span v-if="current" class="mono small">{{ current.rel }}</span>
        <span v-else class="faint small">未选择文件</span>
        <span v-if="dirty" class="badge warn">未保存</span>
        <span class="spacer" />
        <span v-if="current" class="faint small mono">{{ content.length }} 字符</span>
        <button class="ghost sm" :disabled="!current || !dirty" @click="content = original">撤销改动</button>
        <button class="primary sm" :disabled="!current || saving || !dirty" @click="save">
          {{ saving ? '保存中…' : '保存并提交' }}
        </button>
      </div>

      <div v-if="loadingFile" class="empty">读取中…</div>
      <div v-else-if="!current" class="empty">从左侧选一个文件开始编辑</div>
      <textarea
        v-else
        v-model="content"
        class="editor mono"
        spellcheck="false"
        placeholder="文件内容…"
      />
    </section>

    <aside class="history">
      <div class="hist-head">
        <strong class="small">提交历史</strong>
        <span class="badge">{{ log.length }}</span>
        <span class="spacer" />
        <button class="ghost sm" :disabled="loadingLog" @click="loadLog">刷新</button>
      </div>
      <div v-if="tags.length" class="tags">
        <span v-for="t in tags" :key="t" class="badge gold mono">{{ t }}</span>
      </div>
      <div class="hist-scroll">
        <div v-if="loadingLog" class="empty">加载中…</div>
        <div v-else-if="!log.length" class="empty">没有 git 提交记录</div>
        <div
          v-for="c in log"
          :key="c.sha"
          class="commit"
          :class="{ active: showRev?.sha === c.sha }"
          @click="viewAtCommit(c)"
        >
          <div class="row">
            <code class="mono sha">{{ c.short }}</code>
            <span class="spacer" />
            <span class="faint small">{{ (c.date || '').slice(0, 16).replace('T', ' ') }}</span>
          </div>
          <div class="msg">{{ c.message }}</div>
        </div>
      </div>

      <div v-if="showRev" class="hist-view">
        <div class="row">
          <span class="small">版本 <code class="mono">{{ showRev.short }}</code></span>
          <span class="spacer" />
          <button class="ghost sm" :disabled="loadingHist || !histContent" @click="restoreHistory">载入编辑器</button>
        </div>
        <div v-if="loadingHist" class="empty">读取中…</div>
        <pre v-else class="hist-pre mono">{{ histContent || '（该提交下没有这个文件）' }}</pre>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.truth-layout { display: flex; height: 100%; min-height: 0; }

.files { width: 220px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--bg-panel); display: flex; flex-direction: column; min-height: 0; }
.files-head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.files-search { padding: 6px 8px; }
.files-scroll { flex: 1; overflow-y: auto; padding: 2px 4px 8px; }
.file-item { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: var(--radius); cursor: pointer; }
.file-item:hover { background: var(--bg-hover); }
.file-item.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.fname { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.fsize { font-size: 10px; }

.editor-pane { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.pane-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--border); background: var(--bg-panel); }
.editor {
  flex: 1; min-height: 0; border: none; border-radius: 0;
  background: var(--bg); padding: 12px 14px;
  font-size: 12.5px; line-height: 1.75; resize: none;
}
.editor:focus { border: none; }

.history { width: 320px; flex-shrink: 0; border-left: 1px solid var(--border); background: var(--bg-panel); display: flex; flex-direction: column; min-height: 0; }
.hist-head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.tags { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
.hist-scroll { flex: 1; overflow-y: auto; padding: 4px; min-height: 80px; }
.commit { padding: 6px 8px; border-radius: var(--radius); cursor: pointer; }
.commit:hover { background: var(--bg-hover); }
.commit.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.sha { color: var(--accent); font-size: 11px; }
.msg { font-size: 12.5px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.hist-view { border-top: 1px solid var(--border); max-height: 45%; display: flex; flex-direction: column; min-height: 0; }
.hist-view .row { padding: 6px 10px; }
.hist-pre {
  flex: 1; overflow: auto; margin: 0; padding: 8px 12px;
  font-size: 11.5px; line-height: 1.7; color: var(--text-dim);
  background: var(--bg); white-space: pre-wrap; word-break: break-word;
}
</style>
