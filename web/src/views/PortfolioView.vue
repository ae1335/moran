<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

// ---------------- 题材模板 ----------------
const templates = ref([])
const loadingTpl = ref(false)

async function loadTemplates() {
  loadingTpl.value = true
  try {
    const r = await api.templates(dir.value)
    templates.value = r.items || []
  } catch (e) {
    store.toast(`模板加载失败：${e.message}`, 'err')
    templates.value = []
  } finally {
    loadingTpl.value = false
  }
}

// ---------------- 设定集 ----------------
const includeChapters = ref(false)
const outlineLimit = ref(100)
const preview = ref(null)
const exporting = ref(false)
const exported = ref(null)

async function loadPreview() {
  if (!dir.value) return
  try {
    preview.value = await api.settingBook(dir.value, {
      includeChapters: includeChapters.value,
      outlineLimit: Number(outlineLimit.value) || 100,
    })
  } catch (e) {
    store.toast(`设定集预览失败：${e.message}`, 'err')
    preview.value = null
  }
}

async function exportBook() {
  exporting.value = true
  try {
    const r = await api.settingBookExport(dir.value, {
      includeChapters: includeChapters.value,
      outlineLimit: Number(outlineLimit.value) || 100,
    })
    exported.value = r
    store.toast(`设定集已生成：${r.files?.length || 0} 个文件`, 'ok')
  } catch (e) {
    store.toast(`设定集导出失败：${e.message}`, 'err')
    exported.value = null
  } finally {
    exporting.value = false
  }
}

// ---------------- 封面 ----------------
const coverWidth = ref(600)
const coverHeight = ref(800)
const cover = ref(null)
const coverPrompt = ref('')
const coverStamp = ref(Date.now())
const generatingCover = ref(false)
const coverErr = ref('')

const coverSrc = computed(() => {
  if (!cover.value?.path) return ''
  return `${api.coverUrl(dir.value)}?t=${coverStamp.value}`
})

async function loadCoverPrompt() {
  if (!dir.value) return
  try {
    const r = await api.coverPrompt(dir.value)
    coverPrompt.value = r.prompt || ''
  } catch {
    coverPrompt.value = ''
  }
}

async function generateCover() {
  generatingCover.value = true
  coverErr.value = ''
  try {
    const r = await api.coverGenerate(dir.value, {
      width: Number(coverWidth.value) || 600,
      height: Number(coverHeight.value) || 800,
    })
    cover.value = r
    coverStamp.value = Date.now()
    if (r.warnings?.length) store.toast(r.warnings.join('；'), 'warn')
    else store.toast('封面已生成', 'ok')
  } catch (e) {
    coverErr.value = e.message
    cover.value = null
    store.toast(`封面生成失败：${e.message}`, 'err')
  } finally {
    generatingCover.value = false
  }
}

async function copyPrompt() {
  if (!coverPrompt.value) return
  try {
    await navigator.clipboard.writeText(coverPrompt.value)
    store.toast('提示词已复制', 'ok')
  } catch {
    store.toast('复制失败：浏览器拒绝访问剪贴板', 'err')
  }
}

// 只读已有封面：进页面**不**触发生成（写文件必须有作者的明确动作）
async function loadExistingCover() {
  if (!dir.value) return
  try {
    const r = await api.covers(dir.value)
    const first = r.items?.[0]
    if (first) {
      cover.value = { path: first.path, width: coverWidth.value, height: coverHeight.value }
      coverStamp.value = Date.now()
    } else {
      cover.value = null
    }
  } catch {
    cover.value = null
  }
}

onMounted(async () => {
  await Promise.all([loadTemplates(), loadPreview(), loadCoverPrompt(), loadExistingCover()])
})

watch(dir, async () => {
  preview.value = null
  exported.value = null
  cover.value = null
  await Promise.all([loadTemplates(), loadPreview(), loadCoverPrompt(), loadExistingCover()])
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>作品集</h1>
        <p class="dim small">
          题材模板 · 设定集导出 · 封面生成 —— 都是离线可跑的本地产物
        </p>
      </div>
    </div>

    <!-- ============ 题材模板 ============ -->
    <div class="card">
      <div class="row">
        <strong class="small">题材模板</strong>
        <span class="badge">{{ templates.length }}</span>
        <span class="spacer" />
        <button class="ghost sm" :disabled="loadingTpl" @click="loadTemplates">
          {{ loadingTpl ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <p class="faint small" style="margin: 8px 0 0">
        拆书产物自动同步到工作区共享库，新建书时按题材自动套用节奏基线（开局模式 / 金手指 / 升级间隔 / 爽点间隔）。
      </p>
      <div v-if="!templates.length" class="empty">
        还没有模板。去「拆书」导入一本对标书，产物会自动进入共享模板库。
      </div>
      <div v-else class="tpl-list">
        <div v-for="t in templates" :key="t.id" class="tpl-row">
          <div class="tpl-head">
            <span class="mono tpl-id">{{ t.id }}</span>
            <span v-if="t.genre" class="badge info">{{ t.genre }}</span>
          </div>
          <div class="tpl-brief small dim">{{ t.brief || '（模板字段为空）' }}</div>
        </div>
      </div>
    </div>

    <!-- ============ 设定集 ============ -->
    <div class="card">
      <div class="row">
        <strong class="small">设定集</strong>
        <span v-if="preview" class="badge">{{ preview.sections?.length || 0 }} 节</span>
        <span v-if="preview" class="badge info mono">{{ preview.words }} 字</span>
        <span class="spacer" />
        <label class="row small" style="gap: 4px">
          <input v-model="includeChapters" type="checkbox" style="width: auto" @change="loadPreview" />
          <span class="faint">附正文全文</span>
        </label>
        <button class="ghost sm" @click="loadPreview">刷新预览</button>
        <button class="primary sm" :disabled="exporting" @click="exportBook">
          {{ exporting ? '生成中…' : '生成设定集' }}
        </button>
      </div>
      <p class="faint small" style="margin: 8px 0 0">
        汇总宪法 / 世界观 / 角色 / 线索 / 伏笔 / 时间线 / 资源 / 章纲 / 章节清单，
        产物进 <code class="mono">exports/{date}/</code>。
      </p>

      <div v-if="preview" class="sections">
        <span v-for="(s, i) in preview.sections" :key="s" class="sec-chip">
          <span class="faint">{{ i + 1 }}</span> {{ s }}
        </span>
      </div>

      <div v-if="exported" class="ok-block">
        <div class="small">已生成 → <code class="mono">{{ exported.out_dir }}</code></div>
        <div v-for="f in exported.files" :key="f" class="mono small dim" style="margin-top: 3px">{{ f }}</div>
        <div v-if="exported.warnings?.length" class="small warn-text" style="margin-top: 6px">
          {{ exported.warnings.join('；') }}
        </div>
      </div>
    </div>

    <!-- ============ 封面 ============ -->
    <div class="card">
      <div class="row">
        <strong class="small">封面</strong>
        <span v-if="cover" class="badge info mono">{{ cover.width }}×{{ cover.height }}</span>
        <span class="spacer" />
        <input v-model="coverWidth" type="number" min="200" max="2000" style="width: 84px" />
        <span class="faint">×</span>
        <input v-model="coverHeight" type="number" min="200" max="3000" style="width: 84px" />
        <button class="primary sm" :disabled="generatingCover" @click="generateCover">
          {{ generatingCover ? '生成中…' : '生成封面' }}
        </button>
      </div>

      <div class="cover-wrap">
        <div class="cover-box">
          <img v-if="coverSrc && !coverErr" :src="coverSrc" alt="封面预览" class="cover-img" />
          <div v-else class="cover-empty faint small">
            {{ coverErr ? `生成失败：${coverErr}` : '还没有封面，点右上「生成封面」' }}
          </div>
        </div>
        <div class="cover-side">
          <div class="small dim">程序化 SVG · 按题材配色 · 不含外部素材</div>
          <div class="small dim" style="margin-top: 8px">给外部生图工具的提示词：</div>
          <div class="prompt-box mono small">{{ coverPrompt || '（尚未生成）' }}</div>
          <div class="row" style="margin-top: 8px">
            <button class="ghost sm" :disabled="!coverPrompt" @click="copyPrompt">复制提示词</button>
          </div>
          <div v-if="cover?.path" class="small dim" style="margin-top: 10px">
            文件：<span class="mono">{{ cover.path }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 1200px; }

.tpl-list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.tpl-row { padding: 8px 10px; border: 1px solid var(--line); border-radius: 5px; }
.tpl-head { display: flex; align-items: center; gap: 8px; }
.tpl-id { font-size: 13px; color: var(--text); }
.tpl-brief { margin-top: 4px; line-height: 1.6; }

.sections { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.sec-chip {
  font-size: 12px;
  padding: 3px 9px;
  border: 1px solid var(--line);
  border-radius: 11px;
  color: var(--text-dim);
}
.ok-block {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(76, 175, 125, 0.35);
  border-radius: 6px;
  background: rgba(76, 175, 125, 0.06);
}
.warn-text { color: var(--warn, #d9a441); }

.cover-wrap { display: flex; gap: 20px; margin-top: 12px; align-items: flex-start; }
.cover-box {
  width: 240px;
  min-height: 320px;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
  background: var(--bg-panel);
  display: flex;
  align-items: center;
  justify-content: center;
}
.cover-img { width: 100%; height: auto; display: block; }
.cover-empty { padding: 20px; text-align: center; line-height: 1.7; }
.cover-side { flex: 1; min-width: 0; }
.prompt-box {
  margin-top: 4px;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--bg-panel);
  color: var(--text-dim);
  line-height: 1.7;
  word-break: break-all;
}
</style>
