<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const store = useAppStore()
const router = useRouter()
const loading = ref(false)
const showCreate = ref(false)
const form = ref({
  title: '',
  genre: '玄幻-东方玄幻',
  platform: 'tomato',
  total_chapters: 1200,
  daily_chapters: 2,
  words_min: 2000,
  words_max: 2400,
  protagonist_name: '林澈',
})
const health = ref(null)

onMounted(async () => {
  await store.refreshShelf()
  try { health.value = await api.health() } catch { health.value = { ok: false } }
})

async function createBook() {
  if (!form.value.title.trim()) { store.toast('书名不能为空', 'err'); return }
  loading.value = true
  try {
    const r = await api.createBook({
      title: form.value.title.trim(),
      genre: form.value.genre,
      platform: form.value.platform,
      total_chapters: Number(form.value.total_chapters),
      daily_chapters: Number(form.value.daily_chapters),
      words_per_chapter: [Number(form.value.words_min), Number(form.value.words_max)],
      protagonist_name: form.value.protagonist_name,
    })
    await store.refreshShelf()
    showCreate.value = false
    form.value.title = ''
    store.toast(`已创建《${r.title}》`, 'ok')
    router.push(`/b/${encodeURIComponent(r.dir)}/outline`)
  } catch (e) {
    store.toast(`创建失败：${e.message}`, 'err')
  } finally {
    loading.value = false
  }
}

function open(dir) {
  router.push(`/b/${encodeURIComponent(dir)}/outline`)
}

function openPortfolio() {
  const first = store.shelf.novels[0]
  if (!first) return
  router.push(`/b/${encodeURIComponent(first.dir)}/portfolio`)
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>书架</h1>
        <p class="dim small">
          一本书 = 一个 git 仓库 · 纯文件真相源 · 随时可用任何编辑器打开
        </p>
      </div>
      <div class="row">
        <span v-if="health" class="badge" :class="health.ok ? 'ok' : 'err'">
          {{ health.ok ? '服务正常' : '服务异常' }}
        </span>
        <button class="primary" @click="showCreate = !showCreate">
          {{ showCreate ? '取消' : '+ 新建书' }}
        </button>
      </div>
    </div>

    <div v-if="showCreate" class="card create-card">
      <div class="grid">
        <label>书名<input v-model="form.title" placeholder="例如：凡人问道" @keyup.enter="createBook" /></label>
        <label>题材<input v-model="form.genre" placeholder="玄幻-东方玄幻" /></label>
        <label>
          目标平台
          <select v-model="form.platform">
            <option value="tomato">番茄小说</option>
            <option value="qidian">起点中文网</option>
            <option value="qimao">七猫中文网</option>
          </select>
        </label>
        <label>主角名<input v-model="form.protagonist_name" /></label>
        <label>总章数<input v-model.number="form.total_chapters" type="number" /></label>
        <label>日更章数<input v-model.number="form.daily_chapters" type="number" /></label>
        <label>单章字数下限<input v-model.number="form.words_min" type="number" /></label>
        <label>单章字数上限<input v-model.number="form.words_max" type="number" /></label>
      </div>
      <div class="row" style="margin-top: 12px">
        <span class="faint small">
          建书会自动生成：宪法 / 文风指纹 / 力量体系 / 地理 / 势力 / 时间线 / 伏笔池 / 主角卡 / 平台 profile / git 仓库
        </span>
        <span class="spacer" />
        <button class="primary" :disabled="loading" @click="createBook">
          {{ loading ? '创建中…' : '创建' }}
        </button>
      </div>
    </div>

    <div v-if="!store.shelf.novels.length" class="empty">
      还没有书。点右上角「+ 新建书」开始。
    </div>

    <div class="shelf-grid">
      <div v-for="n in store.shelf.novels" :key="n.dir" class="book-card" @click="open(n.dir)">
        <div class="book-title">{{ n.title }}</div>
        <div class="row small dim">
          <span>{{ n.genre }}</span>
          <span v-if="n.platform" class="badge">{{ n.platform }}</span>
          <span class="spacer" />
          <span class="badge" :class="n.status === 'drafting' ? 'info' : 'ok'">{{ n.status }}</span>
        </div>
        <div class="book-meta mono small faint">
          第 {{ n.current_chapter }} 章 · 更新于 {{ (n.updated || '').slice(0, 16).replace('T', ' ') }}
        </div>
        <div class="book-actions">
          <a
            class="ghost sm"
            :href="`/pwa/?book=${encodeURIComponent(n.dir)}`"
            target="_blank"
            rel="noopener"
            title="手机灵感速记、进度查看与远程夜审"
            @click.stop
          >PWA</a>
        </div>
      </div>
    </div>

    <div v-if="store.shelf.novels.length" class="card" style="margin-top: 20px">
      <div class="row">
        <strong class="small">共享入口</strong>
        <span class="spacer" />
        <button class="ghost sm" @click="router.push('/usage')">用量统计</button>
        <button class="ghost sm" @click="router.push('/providers')">模型路由</button>
        <button v-if="store.shelf.novels.length" class="ghost sm" @click="openPortfolio">作品集</button>
      </div>
      <p class="faint small" style="margin: 8px 0 0">
        工作区根：<code class="mono">{{ health?.root }}</code> ·
        跨书共享素材在 <code class="mono">workbench/</code>，配置在 <code class="mono">workbench-config/</code>
      </p>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 1200px; }

.create-card { margin-bottom: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); }
.shelf-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.book-card {
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px; cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.book-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.book-title { font-size: 16px; font-weight: 600; margin-bottom: 8px; }
.book-meta { margin-top: 6px; }
.book-actions { display: flex; justify-content: flex-end; margin-top: 4px; }
.book-actions a { text-decoration: none; }
</style>
