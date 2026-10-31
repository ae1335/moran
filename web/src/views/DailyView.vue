<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { useAppStore } from '../store'

const route = useRoute()
const store = useAppStore()
const dir = computed(() => route.params.book)

const loading = ref(false)
const panel = ref(null)
const exporting = ref(false)
const exportResult = ref(null)
const blockedInfo = ref(null)
const exportFormat = ref('txt')

const maxWords = computed(() => {
  const curve = panel.value?.words_curve || []
  return Math.max(1, ...curve.map((c) => c.words || 0))
})

const shortage = computed(() => !!panel.value?.shortage_warning)

// 缺陷 #121：索引对账结果在 Web 端完全没有出口。entity-deltas.jsonl 损坏、
// 正文改了没重索引，这些在 CLI 里已经会报警（#118/#119），但作者根本不开 CLI。
// 日更面板是每天必看的一页，把对账结果挂在这里，坏了就当场看见。
const indexHealth = ref(null)
const checkingIndex = ref(false)

const indexProblems = computed(() => {
  const v = indexHealth.value
  if (!v) return []
  const out = []
  if (v.error) out.push(v.error)
  if ((v.drifted || []).length) out.push(`正文已改未重索引：第 ${v.drifted.join("、")} 章`)
  if ((v.missing || []).length) out.push(`正文有、索引没有：第 ${v.missing.join("、")} 章`)
  if ((v.orphaned || []).length) out.push(`索引有、正文已删：第 ${v.orphaned.join("、")} 章`)
  if (v.entity_source) out.push(v.entity_source)
  // #127：embedding 没跑成时 hash 对账照样全对，只有 vector_source 会说真话
  if (v.vector_source) out.push(v.vector_source)
  return out
})

async function loadIndexHealth() {
  if (!dir.value) return
  checkingIndex.value = true
  try {
    indexHealth.value = await api.indexVerify(dir.value)
  } catch (e) {
    indexHealth.value = null
  } finally {
    checkingIndex.value = false
  }
}

async function rebuildIndex(full) {
  checkingIndex.value = true
  try {
    const r = await api.indexBuild(dir.value, full)
    if (r?.error) {
      store.toast(`索引重建失败：${r.error}`, 'err')
    } else if (r?.entity_source) {
      store.toast(`索引已重建，但数据源有问题：${r.entity_source}`, 'warn')
      // #127：向量缺失时「索引已重建 N 章」会把语义检索的假象盖住
    } else if (r?.vector_source) {
      store.toast(`索引已重建，但没有向量：${r.vector_source}`, 'warn')
    } else {
      store.toast(`索引已重建：${(r?.changed || []).length} 章`, 'ok')
    }
    await loadIndexHealth()
  } catch (e) {
    store.toast(`索引重建失败：${e.message}`, 'err')
  } finally {
    checkingIndex.value = false
  }
}

const stockRows = computed(() => {
  if (!panel.value) return []
  const s = panel.value.stock || {}
  const c = panel.value.stock_count || {}
  return [
    { key: 'draft', label: '初稿箱', chs: s.draft || [], count: c.draft ?? (s.draft || []).length, kind: 'warn' },
    { key: 'reviewed', label: '已审箱', chs: s.reviewed || [], count: c.reviewed ?? (s.reviewed || []).length, kind: 'info' },
    { key: 'final', label: '定稿箱（未发布）', chs: s.final || [], count: c.final ?? (s.final || []).length, kind: 'ok' },
    { key: 'published', label: '已发布', chs: s.published || [], count: c.published ?? (s.published || []).length, kind: 'info' },
  ]
})

async function load() {
  if (!dir.value) return
  loading.value = true
  try {
    panel.value = await api.dailyPanel(dir.value)
  } catch (e) {
    store.toast(`日更面板加载失败：${e.message}`, 'err')
    panel.value = null
  } finally {
    loading.value = false
  }
}

// 导出前预检（§11.1）：先让作者看到红线/字数/钩子问题，再决定是否导出
const precheck = ref(null)
const checking = ref(false)

async function doPrecheck() {
  checking.value = true
  try {
    precheck.value = await api.exportPrecheck(dir.value, panel.value?.platform)
    const s = precheck.value?.summary || {}
    if (precheck.value?.ready) {
      store.toast(`预检通过：${s.chapters} 章均达标`, 'ok')
    } else {
      store.toast(`预检发现 ${s.high || 0} 个高优问题，建议先处理`, 'err')
    }
  } catch (e) {
    store.toast(`预检失败：${e.message}`, 'err')
    precheck.value = null
  } finally {
    checking.value = false
  }
}

const publishing = ref(false)

async function togglePublish(ch, currentlyPublished) {
  if (publishing.value) return
  publishing.value = true
  try {
    const r = await api.publish(dir.value, [ch], !currentlyPublished)
    const skipped = r.skipped?.length ? `，跳过 ${r.skipped.length} 章` : ''
    store.toast(`${currentlyPublished ? '已撤销发布标记' : '已标记发布'}第 ${ch} 章${skipped}`, 'ok')
    // 缺陷 #105：服务端 mark_published 会回传断层告警（#70 加的 gaps/warning），
    // 但这里只认 skipped —— 作者点了「标记发布」看到的仍是成功提示，断层照样没人知道。
    if (r.warning) store.toast(`⚠ ${r.warning}`, 'warn')
    await load()
  } catch (e) {
    store.toast(`发布标记失败：${e.message}`, 'err')
  } finally {
    publishing.value = false
  }
}

async function doExport(allowRisk = false) {
  exporting.value = true
  if (!allowRisk) {
    exportResult.value = null
    blockedInfo.value = null
  }
  try {
    const r = await api.exportBook(dir.value, { mode: '合集', allow_risk: allowRisk, file_format: exportFormat.value })
    exportResult.value = r
    if (r.blocked) {
      blockedInfo.value = {
        reason: r.block_reason || '导出被拦截',
        risk: r.risk_ack_required,
        unresolved: r.unresolved_high || [],
      }
      store.toast('导出被拦截，需要确认风险', 'err')
    } else {
      blockedInfo.value = null
      store.toast(`已导出 ${r.files?.length || 0} 个文件到 ${r.out_dir || '导出目录'}`, 'ok')
      // 缺陷 #123：导出「成功」不等于「没问题」—— 正文里的套语 / 复读不会被拦下，
      // 只是被记进 unresolved_high。修复前这条只在「被拦截」时才被渲染，
      // 成功导出时作者一句都看不到，于是「发布前强制」依旧是没人知道。
      const unresolved = r.unresolved_high || []
      if (unresolved.length) {
        const cnt = unresolved.reduce((n, u) => n + (u.issues?.length || 0), 0)
        store.toast(`导出完成，但 ${unresolved.length} 章 / ${cnt} 条高优问题未处理（见导出报告）`, 'warn')
      }
    }
  } catch (e) {
    store.toast(`导出失败：${e.message}`, 'err')
  } finally {
    exporting.value = false
  }
}

onMounted(() => { load(); loadIndexHealth() })
watch(dir, () => { load(); loadIndexHealth() })
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1>日更面板</h1>
        <p class="dim small">
          存稿箱三级缓冲 · 断更预警 · 一键导出
        </p>
      </div>
      <div class="row">
        <button class="ghost sm" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
        <button class="ghost sm" :disabled="checking || !panel" @click="doPrecheck">
          {{ checking ? '预检中…' : '导出预检' }}
        </button>
        <select v-model="exportFormat" style="width: 86px" title="起点可选 DOCX；其他平台使用 TXT">
          <option value="txt">TXT</option>
          <option value="docx" :disabled="panel?.platform !== 'qidian'">DOCX</option>
        </select>
        <button class="primary" :disabled="exporting || !panel" @click="doExport(false)">
          {{ exporting ? '导出中…' : '导出合集' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!panel" class="empty">
      没有拿到日更数据。确认这本书目录存在（<code class="mono">{{ dir }}</code>），或后端是否启动。
    </div>

    <template v-else>
      <div v-if="shortage" class="banner">
        <strong>⚠ 断更预警</strong>
        <span>{{ panel.warning_text || '存稿不足 3 天，断更风险高' }}</span>
      </div>

      <!-- 缺陷 #121：索引对账结果必须有 Web 出口，否则坏了没人知道 -->
      <div v-if="indexHealth && indexProblems.length" class="banner warn-banner">
        <strong>⚠ 索引对账未通过</strong>
        <div class="idx-problems">
          <div v-for="(p, i) in indexProblems" :key="i" class="small">{{ p }}</div>
        </div>
        <span class="spacer" />
        <button class="ghost sm" :disabled="checkingIndex" @click="rebuildIndex(false)">
          {{ checkingIndex ? '重建中…' : '重建索引' }}
        </button>
      </div>
      <div v-else-if="indexHealth" class="index-ok faint small">
        <span class="badge ok">索引正常</span>
        <span>正文与索引一致</span>
        <span class="spacer" />
        <button class="ghost sm" :disabled="checkingIndex" @click="rebuildIndex(true)">
          {{ checkingIndex ? '重建中…' : '全量重建' }}
        </button>
      </div>

      <div class="kpi-grid">
        <div class="kpi card">
          <div class="kpi-label">存稿天数</div>
          <div class="kpi-value mono" :class="{ bad: shortage }">{{ panel.stock_days }}</div>
          <div class="kpi-sub faint small">目标 ≥ <span class="mono">3</span> 天</div>
        </div>
        <div class="kpi card">
          <div class="kpi-label">日更目标</div>
          <div class="kpi-value mono">{{ panel.daily_target }}</div>
          <div class="kpi-sub faint small">章 / 天</div>
        </div>
        <div class="kpi card">
          <div class="kpi-label">连更天数</div>
          <div class="kpi-value mono">{{ panel.streak_days }}</div>
          <div class="kpi-sub faint small">按 git 提交日计</div>
        </div>
        <div class="kpi card">
          <div class="kpi-label">总字数</div>
          <div class="kpi-value mono">{{ panel.total_words }}</div>
          <div class="kpi-sub faint small">当前进度 第 <span class="mono">{{ panel.current_chapter }}</span> 章</div>
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <div class="row">
            <strong class="small">今日队列</strong>
            <span class="badge info">{{ (panel.today_queue || []).length }}</span>
            <span class="spacer" />
            <span class="faint small">来自 planned / draft 章</span>
          </div>
          <div v-if="!(panel.today_queue || []).length" class="empty">队列为空，先去章纲页确认几章</div>
          <div v-else class="queue">
            <span v-for="ch in panel.today_queue" :key="ch" class="q-chip mono">#{{ ch }}</span>
          </div>
        </div>

        <div class="card">
          <div class="row">
            <strong class="small">存稿箱</strong>
            <span class="spacer" />
            <span class="faint small">可按章号直接排期</span>
          </div>
          <div class="stock">
            <div v-for="s in stockRows" :key="s.key" class="stock-row">
              <span class="badge" :class="s.kind">{{ s.label }}</span>
              <span class="mono stock-count">{{ s.count }}</span>
              <span class="spacer" />
              <span class="stock-chs mono faint small">
                <template v-if="s.chs.length">
                  <button
                    v-for="ch in s.chs"
                    :key="ch"
                    class="chapter-mark"
                    :class="{ actionable: s.key === 'final' || s.key === 'published' }"
                    :disabled="publishing || (s.key !== 'final' && s.key !== 'published')"
                    :title="s.key === 'final' ? '点击标记为已发布' : s.key === 'published' ? '点击撤销发布标记' : ''"
                    @click="togglePublish(ch, s.key === 'published')"
                  >{{ ch }}</button>
                </template>
                <template v-else>—</template>
              </span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="precheck" class="card">
        <div class="row">
          <strong class="small">导出预检</strong>
          <span class="spacer" />
          <span class="badge" :class="precheck.ready ? 'ok' : 'warn'">
            {{ precheck.ready ? '全部达标' : `${precheck.summary.high} 个高优问题` }}
          </span>
        </div>
        <p class="dim small" style="margin: 6px 0">
          平台 <code class="mono">{{ precheck.platform }}</code> ·
          {{ precheck.summary.chapters }} 章 ·
          达标 {{ precheck.summary.ok }} 章 ·
          中优 {{ precheck.summary.mid }} 条
        </p>
        <div class="precheck-list">
          <div v-for="c in precheck.checks" :key="c.ch" class="precheck-row">
            <span class="mono small">#{{ c.ch }}</span>
            <span class="small">{{ c.title }}</span>
            <span class="mono faint small">{{ c.words }} 字</span>
            <span class="spacer" />
            <span class="badge" :class="c.ok ? 'ok' : 'warn'">{{ c.ok ? '达标' : '待修' }}</span>
            <span v-for="(i, k) in [...c.high_issues, ...c.mid_issues]" :key="k" class="issue-tag">
              {{ i.category }}：{{ i.suggestion }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="panel.pacing?.available" class="card">
        <div class="row">
          <strong class="small">与拆书基线的节奏对比</strong>
          <span class="spacer" />
          <span class="faint small">平均章长：本书 <span class="mono">{{ panel.pacing.book_avg_words }}</span> 字</span>
        </div>
        <div v-for="b in panel.pacing.benchmarks" :key="b.id" class="benchmark-row">
          <span class="small">{{ b.id }}</span>
          <span class="mono faint small">对标 {{ b.avg_words }} 字/章</span>
          <span class="spacer" />
          <span class="badge" :class="Math.abs(panel.pacing.book_avg_words - b.avg_words) < 200 ? 'ok' : 'warn'">
            {{ panel.pacing.book_avg_words >= b.avg_words ? '+' : '' }}{{ Math.round(panel.pacing.book_avg_words - b.avg_words) }} 字
          </span>
        </div>
      </div>

      <div class="card curve-card">
        <div class="row">
          <strong class="small">字数曲线（最近 30 章）</strong>
          <span class="spacer" />
          <span class="faint small">峰值 <span class="mono">{{ maxWords }}</span> 字</span>
        </div>
        <div v-if="!(panel.words_curve || []).length" class="empty">还没有已写章节</div>
        <div v-else class="chart">
          <div
            v-for="c in panel.words_curve"
            :key="c.ch"
            class="bar-wrap"
            :title="`第 ${c.ch} 章：${c.words} 字`"
          >
            <div class="bar" :style="{ height: Math.max(2, (c.words / maxWords) * 100) + '%' }" />
            <span class="bar-label mono">{{ c.ch }}</span>
          </div>
        </div>
      </div>

      <div v-if="blockedInfo" class="block err-block">
        <strong class="small">导出被拦截</strong>
        <p class="small" style="margin: 6px 0">{{ blockedInfo.reason }}</p>
        <div v-if="blockedInfo.unresolved.length" class="small faint">
          高风险未决章：
          <span v-for="u in blockedInfo.unresolved" :key="u.ch" class="mono">#{{ u.ch }}({{ u.issues }}条) </span>
        </div>
        <div class="row" style="margin-top: 8px">
          <span class="spacer" />
          <button class="danger sm" :disabled="exporting" @click="doExport(true)">
            {{ exporting ? '导出中…' : '确认风险继续' }}
          </button>
        </div>
      </div>

      <div v-if="exportResult && !exportResult.blocked" class="card ok-card">
        <div class="row">
          <span class="badge ok">导出成功</span>
          <span class="small mono">{{ exportResult.out_dir }}</span>
        </div>
        <p class="small faint" style="margin: 6px 0 0">
          <span class="mono">{{ exportResult.chapters }}</span> 章 ·
          <span class="mono">{{ exportResult.total_words }}</span> 字 ·
          文件 <span class="mono">{{ (exportResult.files || []).length }}</span> 个
        </p>
        <div class="files mono small">
          <div v-for="f in exportResult.files || []" :key="f">{{ f }}</div>
        </div>
        <div v-if="(exportResult.unresolved_high || []).length" class="small" style="margin-top: 8px">
          <strong class="small">未处理的高优问题（不拦导出，但建议先修）</strong>
          <div v-for="u in exportResult.unresolved_high" :key="u.ch" class="faint small">
            <span class="mono">#{{ u.ch }}</span>
            <span v-for="(i, k) in u.issues || []" :key="k" class="issue-tag">
              {{ i.category }}：{{ i.suggestion }}
            </span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.warn-banner { border-color: var(--warn, #c9a227); flex-wrap: wrap; gap: 8px; }
.idx-problems { display: flex; flex-direction: column; gap: 3px; }
.index-ok { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
.chapter-mark {
  border: 0; background: transparent; color: inherit; padding: 0 3px; font: inherit;
}
.chapter-mark.actionable { color: var(--accent); cursor: pointer; text-decoration: underline; }
.chapter-mark:disabled { cursor: default; }
.benchmark-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.benchmark-row:last-child { border-bottom: 0; }
.page { max-width: 1200px; }
.precheck-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.precheck-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-2, rgba(255, 255, 255, 0.02));
}
.issue-tag {
  font-size: 11px;
  color: var(--warn, #e0a33e);
  border: 1px solid currentColor;
  border-radius: 4px;
  padding: 1px 6px;
  opacity: 0.9;
}



.banner {
  display: flex; align-items: center; gap: 10px;
  background: rgba(217, 83, 79, 0.12); border: 1px solid var(--err);
  border-left-width: 4px; border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 14px; color: #ff9d9a; font-size: 13px;
}

.kpi-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; margin-bottom: 14px; }
.kpi-label { font-size: 12px; color: var(--text-dim); }
.kpi-value { font-size: 26px; font-weight: 600; line-height: 1.3; }
.kpi-value.bad { color: var(--err); }
.kpi-sub { margin-top: 2px; }

.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }
@media (max-width: 860px) { .two-col { grid-template-columns: 1fr; } }

.queue { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.q-chip { background: var(--bg-elevated); border: 1px solid var(--border-strong); border-radius: 10px; padding: 1px 9px; font-size: 12px; }

.stock { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.stock-row { display: flex; align-items: center; gap: 8px; }
.stock-count { font-size: 16px; }
.stock-chs { max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }

.curve-card { margin-bottom: 14px; }
.chart { display: flex; align-items: flex-end; gap: 3px; height: 130px; margin-top: 12px; }
.bar-wrap { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; height: 100%; }
.bar { width: 100%; background: linear-gradient(180deg, var(--accent), rgba(201, 162, 39, 0.35)); border-radius: 2px 2px 0 0; min-height: 2px; }
.bar-label { font-size: 9px; color: var(--text-faint); margin-top: 3px; }

.block { border-radius: var(--radius); padding: 10px 12px; margin-bottom: 14px; border: 1px solid var(--border); border-left-width: 3px; }
.err-block { background: rgba(217, 83, 79, 0.08); border-color: rgba(217, 83, 79, 0.35); border-left-color: var(--err); }
.ok-card { border-left: 3px solid var(--ok); }
.files { margin-top: 6px; color: var(--text-dim); max-height: 120px; overflow: auto; }
</style>
