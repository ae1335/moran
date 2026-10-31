<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, generateStream } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const dir = computed(() => route.params.book)

const chapters = ref([])
const current = ref(null)      // { ch, text, words, outline, versions }
const streaming = ref(false)
const streamText = ref('')
const assembling = ref(false)
const assembly = ref(null)
const showAssembly = ref(false)
const saving = ref(false)
const dirty = ref(false)
// 缺陷 #75：已发布章服务端会拒绝保存（409），这里提前置灰并给出同一句提示，
// 免得作者写完一屏才发现保存不了（服务端报错只会是 toast 一闪而过）。
const published = computed(() => current.value?.outline?.status === 'published')
const editText = ref('')
let handle = null

async function loadList() {
  try {
    chapters.value = await store.loadChapters(dir.value)
  } catch (e) {
    store.toast(`章节列表加载失败：${e.message}`, 'err')
  }
}

async function open(ch) {
  try {
    const d = await api.chapter(dir.value, ch)
    current.value = d
    editText.value = d.text || ''
    dirty.value = false
    assembly.value = null
    showAssembly.value = false
    router.replace(`/b/${encodeURIComponent(dir.value)}/write/${ch}`)
  } catch (e) {
    store.toast(`打开第 ${ch} 章失败：${e.message}`, 'err')
  }
}

function startStream() {
  if (!current.value) return
  streaming.value = true
  streamText.value = ''
  const ch = current.value.ch
  handle = generateStream(
    dir.value,
    ch,
    {
      onStart: () => { streamText.value = '' },
      onDelta: (t) => { streamText.value += t; scrollToBottom() },
      onDone: async (p) => {
        streaming.value = false
        store.toast(`第 ${ch} 章生成完成，${p.usage?.words ?? '?'} 字`, 'ok')
        // 缺陷 #122：流式生成的告警（字数偏少 / 检索降级）原先在 SSE done 事件里
        // 根本没有字段可带，作者点完生成出来一章 9 个字却一句提示都没有。
        const ws = p.warnings || []
        for (const w of ws) store.toast(`第 ${ch} 章生成告警：${w}`, 'warn')
        await loadList()
        await open(ch)
      },
      onError: (msg) => {
        streaming.value = false
        store.toast(`生成失败：${msg}`, 'err')
      },
    },
  )
}

function stopStream() {
  handle?.abort()
  streaming.value = false
  store.toast('已中断（章粒度幂等，重生成会从章首重来）')
}

const streamBox = ref(null)
function scrollToBottom() {
  nextTick(() => {
    if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight
  })
}

async function save() {
  if (!current.value) return
  if (published.value) {
    store.toast(`第 ${current.value.ch} 章已发布；如需修改请先撤销发布标记`, 'err')
    return
  }
  saving.value = true
  try {
    await api.putChapter(dir.value, current.value.ch, editText.value, false)
    dirty.value = false
    store.toast('已保存', 'ok')
    await loadList()
    current.value.text = editText.value
  } catch (e) {
    store.toast(`保存失败：${e.message}`, 'err')
  } finally {
    saving.value = false
  }
}

async function showAssembled() {
  if (!current.value) return
  assembling.value = true
  try {
    assembly.value = await api.assemble(dir.value, current.value.ch)
    showAssembly.value = true
  } catch (e) {
    store.toast(`装配失败：${e.message}`, 'err')
  } finally {
    assembling.value = false
  }
}

async function adopt(v) {
  if (published.value) {
    store.toast(`第 ${current.value.ch} 章已发布；如需切换版本请先撤销发布标记`, 'err')
    return
  }
  try {
    await api.adoptVersion(dir.value, current.value.ch, v)
    store.toast(`已切换为 v${v}`, 'ok')
    await open(current.value.ch)
  } catch (e) {
    store.toast(`切换失败：${e.message}`, 'err')
  }
}

async function score(v, s) {
  try {
    await api.scoreVersion(dir.value, current.value.ch, v, s, '')
    store.toast(`v${v} 评分 ${s}`, 'ok')
    await open(current.value.ch)
  } catch (e) {
    store.toast(`评分失败：${e.message}`, 'err')
  }
}

// 缺陷 #123：反 AI 味管线在 Web 端从来没有入口 —— client.js 有 polish 封装，
// 但整个 web/src 没有一个调用点，ReviewView 里那句「或跑反 AI 味润色」指不到任何按钮。
// 这里补上：先只读检查（不改盘），作者看过 diff 再决定要不要落盘。
const polishing = ref(false)
const applyingPolish = ref(false)
const polishResult = ref(null)

async function runPolish() {
  if (!current.value) return
  polishing.value = true
  polishResult.value = null
  try {
    const r = await api.polish(dir.value, current.value.ch, false, false)
    polishResult.value = r
    // 告警必须逐条弹出来：模型层挂掉时服务端只回 warnings（规则层结果已保留），
    // 前端不消费就等于作者以为「润色过」，实际只跑了规则层。
    for (const w of r.warnings || []) store.toast('反 AI 味润色告警：' + w, 'warn')
    const n = r.issues?.length || 0
    store.toast(n ? '反 AI 味检查：检出 ' + n + ' 条问题' : '反 AI 味检查：未检出问题', n ? 'warn' : 'ok')
  } catch (e) {
    store.toast('反 AI 味检查失败：' + e.message, 'err')
  } finally {
    polishing.value = false
  }
}

async function applyPolish() {
  if (!current.value) return
  if (published.value) {
    store.toast('第 ' + current.value.ch + ' 章已发布；如需润色请先撤销发布标记', 'err')
    return
  }
  if (!window.confirm('反 AI 味润色会改写本章正文（旧版会留档，可切回），确定应用？')) return
  applyingPolish.value = true
  try {
    const r = await api.polish(dir.value, current.value.ch, false, true)
    for (const w of r.warnings || []) store.toast('反 AI 味润色告警：' + w, 'warn')
    store.toast(
      r.applied ? '反 AI 味润色已应用（第 ' + current.value.ch + ' 章）' : '反 AI 味润色没有改动正文',
      r.applied ? 'ok' : 'warn',
    )
    polishResult.value = null
    await open(current.value.ch)
  } catch (e) {
    store.toast('反 AI 味润色失败：' + e.message, 'err')
  } finally {
    applyingPolish.value = false
  }
}
const charCount = computed(() => {
  const t = editText.value || ''
  const cjk = (t.match(/[\u4e00-\u9fff]/g) || []).length
  const words = (t.match(/[A-Za-z0-9]+/g) || []).length
  return cjk + words
})

onMounted(async () => {
  await loadList()
  const ch = Number(route.params.ch)
  if (ch) open(ch)
  else if (chapters.value.length) open(chapters.value[chapters.value.length - 1].ch)
})

watch(() => route.params.ch, (v) => {
  if (!v) return
  const n = Number(v)
  if (current.value?.ch !== n) open(n)
})
</script>

<template>
  <div class="write-layout">
    <!-- 左：章节列表 -->
    <aside class="ch-list">
      <div class="ch-head">
        <span class="small dim">章节</span>
        <span class="spacer" />
        <span class="badge">{{ chapters.length }}</span>
      </div>
      <div class="ch-scroll">
        <div
          v-for="c in chapters"
          :key="c.ch"
          class="ch-item"
          :class="{ active: current?.ch === c.ch }"
          @click="open(c.ch)"
        >
          <div class="row">
            <span class="mono small">{{ String(c.ch).padStart(4, '0') }}</span>
            <span class="title">{{ c.title || '（未命名）' }}</span>
          </div>
          <div class="row small faint">
            <span class="mono">{{ c.words }} 字</span>
            <span v-if="c.versions" class="badge">{{ c.versions }}v</span>
            <span v-if="c.has_review" class="badge info">审</span>
            <span class="spacer" />
            <span class="badge" :class="{
              ok: c.status === 'final',
              info: c.status === 'reviewed' || c.status === 'drafted',
              warn: c.status === 'planned' || c.status === 'draft',
            }">{{ c.status }}</span>
          </div>
        </div>
        <div v-if="!chapters.length" class="empty">还没有章节<br />先去章纲工作台生成</div>
      </div>
    </aside>

    <!-- 中：正文 -->
    <section class="editor">
      <div v-if="!current" class="empty">从左侧选一章，或在章纲工作台生成</div>
      <template v-else>
        <div class="editor-head">
          <div class="row">
            <strong>第 {{ current.ch }} 章</strong>
            <span class="dim">{{ current.outline?.title }}</span>
            <span class="badge mono">{{ charCount }} 字</span>
            <span v-if="current.outline?.target_words" class="faint small mono">
              目标 {{ current.outline.target_words[0] }}–{{ current.outline.target_words[1] }}
            </span>
          </div>
          <div class="row">
            <button class="ghost sm" :disabled="assembling" @click="showAssembled">
              {{ assembling ? '装配中…' : '看装配上下文' }}
            </button>
            <button
              class="ghost sm"
              :disabled="polishing || !current.text"
              @click="runPolish"
              title="检查本章的套语 / 排比 / 节奏 / 跨章复读（先不改正文）"
            >
              {{ polishing ? '检查中…' : '反 AI 味润色' }}
            </button>
            <button class="ghost sm" :disabled="!dirty || saving || published" @click="save">
              {{ published ? '已发布·不可改' : saving ? '保存中…' : dirty ? '保存*' : '已保存' }}
            </button>
            <button v-if="!streaming" class="primary sm" @click="startStream">
              {{ current.text ? '重新生成' : '生成正文' }}
            </button>
            <button v-else class="danger sm" @click="stopStream">中断</button>
          </div>
        </div>

        <div v-if="streaming" class="stream-box" ref="streamBox">
          <div class="prose streaming">{{ streamText || '等待模型响应…' }}</div>
          <div class="cursor">▊</div>
        </div>

        <textarea
          v-else
          v-model="editText"
          class="editor-area"
          spellcheck="false"
          @input="dirty = true"
        />

        <!-- 反 AI 味润色结果（缺陷 #123）：先看问题与 diff，作者点头才落盘 -->
        <div v-if="polishResult" class="polish-box">
          <div class="row">
            <strong class="small">反 AI 味检查结果</strong>
            <span class="spacer" />
            <span class="badge mono">{{ polishResult.original_words }} 字</span>
            <button class="ghost sm" @click="polishResult = null">关闭</button>
          </div>
          <div v-if="polishResult.issues?.length" class="polish-issues">
            <div v-for="(i, k) in polishResult.issues" :key="k" class="small">
              <span class="badge">{{ i.kind }}</span>
              <span class="mono">{{ i.original }}</span>
              <span class="faint">→ {{ i.suggestion }}</span>
            </div>
          </div>
          <div v-else class="small faint">未检出套语 / 排比 / 节奏 / 复读问题。</div>
          <pre v-if="polishResult.diff" class="diff mono">{{ polishResult.diff }}</pre>
          <div class="row" style="margin-top: 6px">
            <button
              class="primary sm"
              :disabled="applyingPolish || published || !polishResult.diff"
              @click="applyPolish"
            >
              {{ published ? '已发布·不可改' : applyingPolish ? '应用中…' : '应用到正文' }}
            </button>
            <span class="faint small">应用后旧版会留档，可随时切回。</span>
          </div>
        </div>

        <!-- 版本 -->
        <div v-if="current.versions?.length" class="versions">
          <span class="small dim">历史版本</span>
          <div v-for="v in current.versions" :key="v.version" class="ver-row">
            <span class="mono small">v{{ v.version }}</span>
            <span class="faint small">{{ v.model || '未知模型' }}</span>
            <span class="mono small">{{ v.words }} 字</span>
            <span v-if="v.score" class="badge gold">{{ '★'.repeat(v.score) }}</span>
            <span class="spacer" />
            <button class="ghost sm" :disabled="published" @click="adopt(v.version)">{{ published ? '已发布·不可改' : '设为正文' }}</button>
            <button class="ghost sm" @click="score(v.version, 3)">3★</button>
            <button class="ghost sm" @click="score(v.version, 5)">5★</button>
          </div>
        </div>
      </template>
    </section>

    <!-- 右：装配上下文 -->
    <aside v-if="showAssembly && assembly" class="assembly">
      <div class="row">
        <strong class="small">装配上下文</strong>
        <span class="spacer" />
        <span class="badge mono">{{ assembly.total_tokens }} / {{ assembly.budget }}</span>
        <button class="ghost sm" @click="showAssembly = false">×</button>
      </div>
      <div class="layer-list">
        <div v-for="(l, k) in assembly.layers" :key="k" class="layer">
          <div class="row">
            <span class="badge" :class="l.trimmed ? 'warn' : ''">{{ k }}</span>
            <span class="small">{{ l.title }}</span>
            <span class="spacer" />
            <span class="mono small dim">{{ l.tokens }}</span>
          </div>
        </div>
      </div>
      <div v-if="assembly.warnings?.length" class="warn-box">
        <div v-for="(w, i) in assembly.warnings" :key="i" class="small">⚠ {{ w }}</div>
      </div>
      <div v-if="assembly.alerts?.length" class="err-box">
        <div v-for="(a, i) in assembly.alerts" :key="i" class="small">
          {{ a.level === 'overdue' ? '⛔' : '⏳' }} {{ a.id }} {{ a.desc }}
        </div>
      </div>
      <div class="prompt-preview">
        <div class="small dim">System Prompt</div>
        <pre>{{ assembly.system_prompt }}</pre>
        <div class="small dim">User Prompt</div>
        <pre>{{ assembly.user_prompt }}</pre>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.write-layout { display: flex; height: 100%; min-height: 0; }

.ch-list {
  width: 250px; flex-shrink: 0; border-right: 1px solid var(--border);
  background: var(--bg-panel); display: flex; flex-direction: column;
}
.ch-head { display: flex; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.ch-scroll { flex: 1; overflow-y: auto; padding: 4px; }
.ch-item { padding: 7px 8px; border-radius: var(--radius); cursor: pointer; margin-bottom: 2px; }
.ch-item:hover { background: var(--bg-hover); }
.ch-item.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.ch-item .title { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.editor { flex: 1; display: flex; flex-direction: column; min-width: 0; padding: 12px 16px; }
.editor-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; flex-wrap: wrap; gap: 8px; }
.editor-area {
  flex: 1; min-height: 0; font-family: var(--serif); font-size: 16px;
  line-height: 1.95; padding: 16px 20px; background: var(--bg-panel);
}
.stream-box {
  flex: 1; min-height: 0; overflow-y: auto; padding: 16px 20px;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius);
  position: relative;
}
.streaming { white-space: pre-wrap; }
.cursor { display: inline-block; color: var(--accent); animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

.polish-box {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
}
.polish-issues {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.polish-box pre.diff {
  margin-top: 6px;
  max-height: 220px;
  overflow-y: auto;
  white-space: pre-wrap;
}
.versions { margin-top: 10px; display: flex; flex-direction: column; gap: 3px; }
.ver-row { display: flex; align-items: center; gap: 10px; padding: 3px 6px; border-radius: 4px; }
.ver-row:hover { background: var(--bg-panel); }

.assembly {
  width: 440px; flex-shrink: 0; border-left: 1px solid var(--border);
  background: var(--bg-panel); padding: 10px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 8px;
}
.layer-list { display: flex; flex-direction: column; gap: 2px; }
.layer { padding: 3px 0; }
.warn-box { background: rgba(217, 164, 65, 0.1); border-left: 2px solid var(--warn); padding: 6px 8px; }
.err-box { background: rgba(217, 83, 79, 0.1); border-left: 2px solid var(--err); padding: 6px 8px; }
.prompt-preview { display: flex; flex-direction: column; gap: 4px; }
.prompt-preview pre {
  background: var(--bg); padding: 8px; border-radius: 4px;
  font-size: 11px; line-height: 1.5; white-space: pre-wrap;
  max-height: 260px; overflow-y: auto; margin: 0;
  font-family: var(--mono); color: var(--text-dim);
}
</style>
