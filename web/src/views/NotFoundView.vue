<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const book = computed(() => route.params.book || '')
const attempted = computed(() => route.fullPath)
</script>

<template>
  <div class="nf">
    <div class="code mono">404</div>
    <h2>没有这个页面</h2>
    <div class="path mono faint">{{ attempted }}</div>
    <div class="row" style="margin-top: 16px">
      <button class="primary sm" @click="router.push('/shelf')">回书架</button>
      <button v-if="book" class="ghost sm" @click="router.push(`/b/${encodeURIComponent(book)}/outline`)">
        去这本书的章纲
      </button>
    </div>
    <p class="faint small" style="margin-top: 18px">
      常见原因：手动改了地址栏、书目录名含特殊字符没编码、或链接来自旧版本。<br />
      注意 <code class="mono">/api</code> 下的未知路径一律返回 404 JSON，不会命中本页。
    </p>
  </div>
</template>

<style scoped>
.nf {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 100%; min-height: 0; padding: 40px; text-align: center;
}
.code { font-size: 64px; color: var(--accent); opacity: 0.35; line-height: 1; }
h2 { margin: 8px 0 4px; font-size: 18px; font-weight: 500; }
.path { word-break: break-all; max-width: 520px; }
code { background: var(--bg-panel); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
</style>
