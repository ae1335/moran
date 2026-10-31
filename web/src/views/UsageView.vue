<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api/client'
import { useAppStore } from '../store'

const store = useAppStore()
const days = ref(7)
const data = ref(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await api.usage(days.value)
  } catch (e) {
    store.toast(`用量加载失败：${e.message}`, 'err')
  } finally {
    loading.value = false
  }
}

const maxDayTokens = computed(() => {
  if (!data.value) return 1
  return Math.max(1, ...Object.values(data.value.by_day || {}).map((d) => d.tokens || 0))
})

function barWidth(tokens) {
  return `${Math.max(2, (tokens / maxDayTokens.value) * 100)}%`
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h1>用量与成本</h1>
        <p class="dim small">订阅桥调用 ¥0 增量 · 官方 API 按 token 计费</p>
      </div>
      <div class="row">
        <button v-for="d in [7, 30, 90]" :key="d" class="ghost sm" :class="{ primary: days === d }"
                @click="days = d; load()">{{ d }} 天</button>
        <button class="ghost sm" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="loading && !data" class="empty">加载中…</div>
    <div v-else-if="!data" class="empty">没有数据</div>
    <div v-else-if="!data.total.calls" class="empty">
      这 {{ data.days }} 天内没有任何模型调用记录
    </div>

    <template v-else>
      <div class="stats-grid">
        <div class="card stat">
          <div class="label">调用次数</div>
          <div class="value mono">{{ data.total.calls }}</div>
        </div>
        <div class="card stat">
          <div class="label">总 tokens</div>
          <div class="value mono">{{ data.total.tokens.toLocaleString() }}</div>
        </div>
        <div class="card stat">
          <div class="label">成本</div>
          <div class="value mono">¥{{ data.total.cost.toFixed(4) }}</div>
        </div>
        <div class="card stat">
          <div class="label">错误</div>
          <div class="value mono" :class="{ err: data.total.errors > 0 }">{{ data.total.errors }}</div>
        </div>
        <div class="card stat">
          <div class="label">订阅池占比</div>
          <div class="value mono">{{ (data.subscription_share * 100).toFixed(0) }}%</div>
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <h3>按任务</h3>
          <table class="tbl">
            <thead><tr><th>任务</th><th>调用</th><th>tokens</th><th>成本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in data.by_task" :key="k">
                <td>{{ k }}</td>
                <td class="mono">{{ v.calls }}</td>
                <td class="mono">{{ v.tokens.toLocaleString() }}</td>
                <td class="mono">¥{{ v.cost.toFixed(4) }}</td>
              </tr>
              <tr v-if="!Object.keys(data.by_task).length"><td colspan="4" class="faint">（无）</td></tr>
            </tbody>
          </table>
        </div>

        <div class="card">
          <h3>按供应商</h3>
          <table class="tbl">
            <thead><tr><th>供应商</th><th>调用</th><th>tokens</th><th>成本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in data.by_provider" :key="k">
                <td>{{ k }}</td>
                <td class="mono">{{ v.calls }}</td>
                <td class="mono">{{ v.tokens.toLocaleString() }}</td>
                <td class="mono">¥{{ v.cost.toFixed(4) }}</td>
              </tr>
              <tr v-if="!Object.keys(data.by_provider).length"><td colspan="4" class="faint">（无）</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h3>每日 token 消耗</h3>
        <div v-if="!Object.keys(data.by_day).length" class="faint small">（无）</div>
        <div class="bars">
          <div v-for="(v, k) in data.by_day" :key="k" class="bar-row">
            <span class="mono small date">{{ k }}</span>
            <div class="bar-track"><div class="bar-fill" :style="{ width: barWidth(v.tokens) }"></div></div>
            <span class="mono small num">{{ v.tokens.toLocaleString() }}</span>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>
          最近调用
          <span class="badge">{{ (data.recent || []).length }}</span>
        </h3>
        <div v-if="!(data.recent || []).length" class="empty">无记录</div>
        <table v-else class="tbl recent">
          <thead>
            <tr>
              <th>时间</th>
              <th>任务</th>
              <th>provider:model</th>
              <th class="r">tokens</th>
              <th class="r">耗时</th>
              <th class="r">成本</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(c, i) in data.recent.slice(0, 25)" :key="i" :class="{ bad: !c.ok }">
              <td class="mono small">{{ (c.ts || '').replace('T', ' ').slice(5, 19) }}</td>
              <td class="mono small">{{ c.task }}</td>
              <td class="mono small faint">{{ c.provider }}:{{ c.model }}</td>
              <td class="mono r">{{ c.total_tokens }}</td>
              <td class="mono r">{{ c.latency_ms }}ms</td>
              <td class="mono r">¥{{ Number(c.cost_yuan || 0).toFixed(4) }}</td>
              <td>
                <span v-if="c.ok" class="badge ok">ok</span>
                <span v-else class="badge err" :title="c.error">fail</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="data.incidents?.length" class="card">
        <h3>桥故障记录</h3>
        <table class="tbl">
          <thead><tr><th>时间</th><th>供应商</th><th>类型</th></tr></thead>
          <tbody>
            <tr v-for="(it, i) in data.incidents" :key="i">
              <td class="mono small">{{ it.ts }}</td>
              <td>{{ it.provider }}</td>
              <td><span class="badge warn">{{ it.kind }}</span></td>
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
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 14px; }
.stat .label { font-size: 12px; color: var(--text-faint); }
.stat .value { font-size: 22px; margin-top: 4px; }
.stat .value.err { color: var(--err); }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; color: var(--text-faint); font-weight: 500; padding: 3px 6px; border-bottom: 1px solid var(--border); font-size: 12px; }
.tbl td { padding: 4px 6px; border-bottom: 1px solid rgba(42,48,57,0.5); }
.bars { display: flex; flex-direction: column; gap: 4px; }
.bar-row { display: flex; align-items: center; gap: 8px; }
.date { width: 92px; color: var(--text-faint); }
.bar-track { flex: 1; height: 14px; background: var(--bg); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), #8a6f14); }
.num { width: 82px; text-align: right; }
.tbl .r { text-align: right; }
.recent th, .recent td { padding: 3px 6px; }
tr.bad { background: rgba(217, 83, 79, 0.07); }
h3 .badge { margin-left: 6px; }
@media (max-width: 760px) { .two-col { grid-template-columns: 1fr; } }
</style>
