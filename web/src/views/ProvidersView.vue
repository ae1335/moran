<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api/client'
import { useAppStore } from '../store'

const store = useAppStore()
const info = ref(null)
const health = ref({})
const probing = ref(false)
const reloading = ref(false)
const loading = ref(false)
const loadErr = ref('')

async function load() {
  loading.value = true
  loadErr.value = ''
  try {
    info.value = await api.providers()
  } catch (e) {
    loadErr.value = e.message || String(e)
    store.toast(`路由加载失败：${e.message}`, 'err')
  } finally {
    loading.value = false
  }
}

async function probe() {
  probing.value = true
  try {
    const r = await api.providersHealth()
    const m = {}
    for (const p of r.providers || []) m[p.provider] = p
    health.value = m
    store.toast('探活完成', 'ok')
  } catch (e) {
    store.toast(`探活失败：${e.message}`, 'err')
  } finally {
    probing.value = false
  }
}

async function reload() {
  reloading.value = true
  try {
    info.value = await api.reloadRouting()
    store.toast('配置已热重载', 'ok')
  } catch (e) {
    store.toast(`重载失败：${e.message}`, 'err')
  } finally {
    reloading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h1>模型路由</h1>
        <p class="dim small">订阅桥强制 127.0.0.1 · 429 熔断 10 分钟 · 链尾必须是官方 API 或本地</p>
      </div>
      <div class="row">
        <button class="ghost sm" :disabled="probing" @click="probe">{{ probing ? '探活中…' : '探活' }}</button>
        <button class="primary sm" :disabled="reloading" @click="reload">{{ reloading ? '重载中…' : '热重载配置' }}</button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="loadErr" class="empty">路由配置加载失败：{{ loadErr }}</div>
    <div v-else-if="!info || !(info.providers || []).length" class="empty">
      没有已注册的 provider，检查 config/providers.yaml
    </div>
    <template v-else>
      <div class="providers">
        <div v-for="p in info.providers" :key="p.name" class="card prov" :class="{ disabled: !p.enabled }">
          <div class="row">
            <strong>{{ p.name }}</strong>
            <span class="badge">{{ p.kind }}</span>
            <span v-if="p.is_local_bridge" class="badge info">本地桥</span>
            <span v-else class="badge gold">官方</span>
            <span class="spacer" />
            <span v-if="p.circuit_open" class="badge err">熔断 {{ p.circuit_remaining_s }}s</span>
            <span v-else-if="!p.enabled" class="badge">已禁用</span>
            <span v-else-if="health[p.name]" class="badge" :class="health[p.name].ok ? 'ok' : 'err'">
              {{ health[p.name].ok ? '健康' : '不可达' }}
            </span>
          </div>
          <div class="mono small faint url">{{ p.base_url || '（内置）' }}</div>
          <div class="row wrap models">
            <span v-for="m in p.models" :key="m" class="badge">{{ m }}</span>
          </div>
          <div v-if="p.note" class="small faint note">{{ p.note }}</div>
          <div v-if="health[p.name]?.detail" class="small faint">探活: {{ health[p.name].detail }}</div>
        </div>
      </div>

      <div class="card routes">
        <h3>任务路由链</h3>
        <div v-if="!Object.keys(info.routes || {}).length" class="empty">routing.yaml 里没有配置任何任务链路</div>
        <table v-else class="tbl">
          <thead><tr><th>任务</th><th>链路（按顺序 fallback）</th></tr></thead>
          <tbody>
            <tr v-for="(hops, task) in info.routes" :key="task">
              <td><span class="badge">{{ task }}</span></td>
              <td>
                <template v-for="(h, i) in hops" :key="i">
                  <span v-if="i" class="arrow">→</span>
                  <code class="mono hop">{{ h.provider }}:{{ h.model }}</code>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page { max-width: 1100px; }

.head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h3 { font-size: 13px; color: var(--text-dim); margin: 0 0 10px; font-weight: 500; }
.providers { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; margin-bottom: 14px; }
.prov.disabled { opacity: 0.55; }
.url { margin: 4px 0 6px; word-break: break-all; }
.models { gap: 4px; margin-bottom: 6px; }
.note { line-height: 1.5; }
.arrow { color: var(--text-faint); margin: 0 6px; }
.hop { background: var(--bg); padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; color: var(--text-faint); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--border); font-size: 12px; }
.tbl td { padding: 6px; border-bottom: 1px solid rgba(42,48,57,0.5); }
</style>
