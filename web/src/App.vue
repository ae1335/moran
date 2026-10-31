<script setup>
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api/client'
import { useAppStore } from './store'

const route = useRoute()
const router = useRouter()
const store = useAppStore()

const bookDir = computed(() => route.params.book || null)
const navItems = computed(() => {
  if (!bookDir.value) return []
  const b = encodeURIComponent(bookDir.value)
  return [
    { to: `/b/${b}/outline`, label: '章纲', icon: '▤' },
    { to: `/b/${b}/write`, label: '写作', icon: '✎' },
    { to: `/b/${b}/review`, label: '审稿', icon: '⚑' },
    { to: `/b/${b}/curate`, label: '沉淀', icon: '◈' },
    { to: `/b/${b}/foreshadow`, label: '伏笔', icon: '⌁' },
    { to: `/b/${b}/daily`, label: '日更', icon: '▦' },
    { to: `/b/${b}/truth`, label: '真相源', icon: '❖' },
    { to: `/b/${b}/benchmark`, label: '拆书', icon: '⚖' },
    { to: `/b/${b}/names`, label: '起名', icon: '✦' },
    { to: `/b/${b}/portfolio`, label: '作品集', icon: '▣' },
    { to: `/b/${b}/feedback`, label: '反馈', icon: '◔' },
  ]
})

watch(
  () => route.params.book,
  async (dir) => {
    if (!dir) { store.currentBook = null; return }
    if (store.currentBook?.dir === dir) return
    try {
      const d = await api.book(dir)
      store.currentBook = { ...d, dir }
    } catch (e) {
      store.toast(`加载书籍失败：${e.message}`, 'err')
    }
  },
  { immediate: true }
)

async function refreshShelf() {
  try { store.shelf = await api.shelf() } catch (e) { /* 后端未起时静默 */ }
}
refreshShelf()

function goShelf() { router.push('/shelf') }
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand" @click="goShelf">
        <span class="mark">墨</span>
        <span class="name">网文工作台</span>
      </div>

      <div v-if="bookDir" class="book-switch">
        <span class="dim small">当前书</span>
        <strong>{{ store.currentBook?.meta?.title || bookDir }}</strong>
        <span v-if="store.currentBook?.meta" class="badge">{{ store.currentBook.meta.genre }}</span>
        <span v-if="store.currentBook?.meta" class="badge info">{{ store.currentBook.meta.platform }}</span>
      </div>

      <nav class="topnav">
        <router-link to="/shelf" :class="{ active: route.name === 'shelf' }">书架</router-link>
        <router-link to="/usage" :class="{ active: route.name === 'usage' }">用量</router-link>
        <router-link to="/providers" :class="{ active: route.name === 'providers' }">模型</router-link>
      </nav>
    </header>

    <div class="body">
      <aside v-if="navItems.length" class="sidebar">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path.startsWith(item.to) }"
        >
          <span class="icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </router-link>
      </aside>

      <main class="content">
        <router-view v-if="$route.matched.length" />
      </main>
    </div>

    <transition-group name="toast" tag="div" class="toasts">
      <div v-for="t in store.toasts" :key="t.id" class="toast" :class="t.kind">{{ t.text }}</div>
    </transition-group>
  </div>
</template>

<style scoped>
.shell { display: flex; flex-direction: column; height: 100%; }

.topbar {
  display: flex; align-items: center; gap: 18px;
  height: 46px; padding: 0 14px;
  background: var(--bg-panel); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.mark {
  width: 24px; height: 24px; display: grid; place-items: center;
  background: var(--accent); color: #1a1508; border-radius: 4px;
  font-weight: 700; font-size: 14px;
}
.name { font-weight: 600; letter-spacing: 0.02em; }
.book-switch { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.topnav { display: flex; gap: 4px; margin-left: auto; }
.topnav a {
  padding: 5px 12px; border-radius: var(--radius); color: var(--text-dim);
  font-size: 13px;
}
.topnav a:hover { background: var(--bg-hover); text-decoration: none; }
.topnav a.active { background: var(--bg-elevated); color: var(--accent); }

.body { display: flex; flex: 1; min-height: 0; }

.sidebar {
  width: 96px; flex-shrink: 0;
  background: var(--bg-panel); border-right: 1px solid var(--border);
  padding: 8px 6px; display: flex; flex-direction: column; gap: 2px;
}
.nav-item {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  padding: 8px 4px; border-radius: var(--radius);
  color: var(--text-dim); font-size: 12px; line-height: 1.3;
}
.nav-item .icon { font-size: 15px; }
.nav-item:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }
.nav-item.active { background: var(--accent-soft); color: var(--accent); }

.content { flex: 1; min-width: 0; overflow: auto; }

.toasts {
  position: fixed; bottom: 18px; right: 18px;
  display: flex; flex-direction: column; gap: 8px; z-index: 999;
}
.toast {
  padding: 9px 14px; border-radius: var(--radius);
  background: var(--bg-elevated); border: 1px solid var(--border-strong);
  box-shadow: 0 6px 22px rgba(0, 0, 0, 0.5); font-size: 13px; max-width: 380px;
}
.toast.err { border-color: var(--err); color: #ffb3b1; }
.toast.ok { border-color: var(--ok); color: #a8e6c4; }
.toast.warn { border-color: var(--warn); color: #e8d48b; }
.toast-enter-active, .toast-leave-active { transition: all 0.2s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(8px); }
</style>
