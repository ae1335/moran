/**
 * 灵感速记 PWA（§13.3）
 *
 * 架构硬约束：PWA 只连主服务 /api/*，LAN 内访问，不直连任何模型桥。
 * 离线策略：写 IndexedDB → 联网后批量推送到服务端（materials/inspiration.md）。
 */
const DB_NAME = 'novel-workbench-pwa'
const STORE = 'notes'
const API_BASE_KEY = 'wb.apiBase'
const BOOK_KEY = 'wb.book'

let db = null;

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORE)) {
        const s = d.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
        s.createIndex('synced', 'synced');
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx(mode = 'readonly') {
  return db.transaction(STORE, mode).objectStore(STORE);
}

function addNote(note) {
  return new Promise((resolve, reject) => {
    const r = tx('readwrite').add(note);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

function allNotes() {
  return new Promise((resolve, reject) => {
    const r = tx().getAll();
    r.onsuccess = () => resolve(r.result || []);
    r.onerror = () => reject(r.error);
  });
}

function markSynced(ids) {
  return new Promise((resolve, reject) => {
    const s = tx('readwrite');
    let left = ids.length;
    if (!left) return resolve();
    ids.forEach((id) => {
      const g = s.get(id);
      g.onsuccess = () => {
        const v = g.result;
        if (v) { v.synced = true; s.put(v); }
        if (--left === 0) resolve();
      };
      g.onerror = () => { if (--left === 0) resolve(); };
    });
  });
}

// ---------------------------------------------------------------- 配置

function apiBase() {
  const saved = localStorage.getItem(API_BASE_KEY);
  if (saved) return saved.replace(/\/$/, '');
  // 默认：同源（PWA 由主服务托管时）
  return location.origin;
}

function currentBook() {
  return localStorage.getItem(BOOK_KEY) || '';
}

async function callApi(path, opts = {}) {
  const res = await fetch(apiBase() + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------- 渲染

function fmtTime(ts) {
  const d = new Date(ts);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

const KIND_LABEL = { idea: '灵感', hook: '钩子', setting: '设定', foreshadow: '伏笔', dialogue: '台词' };

async function render() {
  const notes = await allNotes();
  const pending = notes.filter((n) => !n.synced);
  const synced = notes.filter((n) => n.synced).slice(-30).reverse();

  const pEl = document.getElementById('pending');
  const sEl = document.getElementById('synced');
  document.getElementById('pendingCount').textContent = pending.length;

  pEl.innerHTML = pending.length
    ? pending.map((n) => `<li><span class="kind">${KIND_LABEL[n.kind] || n.kind}</span><span class="body">${escapeHtml(n.text)}</span><span class="ts">${fmtTime(n.ts)}</span></li>`).join('')
    : '<li style="opacity:.5;border-style:dashed">没有待同步内容</li>';

  sEl.innerHTML = synced.length
    ? synced.map((n) => `<li class="synced"><span class="kind">${KIND_LABEL[n.kind] || n.kind}</span><span class="body">${escapeHtml(n.text)}</span><span class="ts">${fmtTime(n.ts)}</span></li>`).join('')
    : '<li style="opacity:.5;border-style:dashed">还没有同步记录</li>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------- 网络状态

function setStatus() {
  const el = document.getElementById('status');
  if (navigator.onLine) {
    el.textContent = '在线';
    el.className = 'status online';
  } else {
    el.textContent = '离线';
    el.className = 'status offline';
  }
}

// ---------------------------------------------------------------- 同步

async function syncAll() {
  if (!db) return; // 首屏 IndexedDB 尚未就绪（online 事件可能早于 openDB 完成）
  const book = currentBook();
  if (!book) {
    alert('还没有选择书。请先在工作台打开一本书，再回到这里（或手动设置 localStorage["wb.book"]）。');
    return;
  }
  const notes = (await allNotes()).filter((n) => !n.synced);
  if (!notes.length) return;

  // 服务端没有专门的「灵感」接口，走 truth 之外的材料文件：用 feedback/stats 不合适，
  // 这里用 PUT truth 之外的方式 —— 直接调 /api/books/{book}/truth 写 materials 不行（白名单外）。
  // 因此走一条轻量通道：把灵感作为「设定提案」写入 materials/inspiration.md 的追加（用 truth 接口不适用），
  // 我们用一个专用端点：POST /api/books/{book}/inspiration（见 server/api/routes.py）。
  const payload = notes.map((n) => ({ text: n.text, kind: n.kind, ts: n.ts }));
  try {
    await callApi(`/api/books/${encodeURIComponent(book)}/inspiration`, {
      method: 'POST',
      body: JSON.stringify({ notes: payload }),
    });
    await markSynced(notes.map((n) => n.id));
    await render();
  } catch (e) {
    console.warn('sync failed', e);
    alert('同步失败：' + e.message + '\n内容仍在本地，未丢失。');
  }
}

async function loadProgress() {
  const book = currentBook();
  const el = document.getElementById('progress');
  if (!book) {
    el.innerHTML = '未选择书目。在工作台书架页打开一本书后，本页会自动同步书目。';
    return;
  }
  try {
    const [detail, panel] = await Promise.all([
      callApi(`/api/books/${encodeURIComponent(book)}`),
      callApi(`/api/books/${encodeURIComponent(book)}/daily-panel`),
    ]);
    el.innerHTML = `
      <div><b>${escapeHtml(detail.meta.title)}</b> · 第 ${detail.meta.current_chapter} 章</div>
      <div>存稿 <b>${panel.stock_count.draft}</b> 待审 / <b>${panel.stock_count.reviewed}</b> 已审 / <b>${panel.stock_count.final}</b> 定稿未发</div>
      <div>已发布 <b>${panel.stock_count.published || 0}</b> 章 · 可支撑 <b>${panel.stock_days}</b> 天（日更 ${panel.daily_target} 章）</div>
      <div>总字数 <b>${panel.total_words.toLocaleString()}</b></div>
      ${panel.shortage_warning ? `<div style="color:#ff8b88">⚠ ${escapeHtml(panel.warning_text || '存稿不足 3 天')}</div>` : ''}
    `;
  } catch (e) {
    el.textContent = '无法读取进度（服务端未连接）：' + e.message;
  }
}

async function triggerNightly() {
  const book = currentBook();
  if (!book) return alert('未选择书目');
  try {
    const r = await callApi(`/api/books/${encodeURIComponent(book)}/jobs/nightly`, { method: 'POST' });
    // 缺陷 #110：r.reviewed 是「成功+失败」的总条数，只看它会把失败当成成功。
    // 服务端已在响应顶层给出 ok / warnings，这里必须照实说。
    const failed = (r.reviewed || []).filter((e) => e && e.error);
    let msg = `夜审已触发：${(r.reviewed || []).length} 章`;
    if (failed.length) {
      msg += `\n⚠ ${failed.length} 章失败：` + failed.map((e) => `第${e.ch}章 ${e.error}`).join('；');
    }
    for (const w of r.warnings || []) {
      if (!failed.length || !w.startsWith('夜审有')) msg += `\n⚠ ${w}`;
    }
    // 缺陷 #129：夜审返回了 index / index_verify 两个对象，它们的 vector_source
    // 是 embedding 没跑成时唯一能说话的字段 —— 但这里从来没读过，
    // 索引悄悄退回纯 BM25，作者点了「夜审」还以为一切正常。
    const iv = r.index_verify || {};
    if (iv.vector_source && !r.warnings?.includes(iv.vector_source)) msg += `\n⚠ 索引对账：${iv.vector_source}`;
    const idx = r.index || {};
    if (idx.vector_source && !r.warnings?.includes(idx.vector_source) && idx.vector_source !== iv.vector_source) msg += `\n⚠ 索引重建：${idx.vector_source}`;
    alert(msg);
    await loadProgress();
  } catch (e) {
    alert('触发失败：' + e.message);
  }
}

// ---------------------------------------------------------------- 入口

window.addEventListener('DOMContentLoaded', async () => {
  // 状态灯与网络事件必须在任何 await 之前绑定。
  // 否则首屏 await openDB()/render()/loadProgress() 期间掉线，offline 事件没有监听者，
  // 状态灯会一直停在「在线」——用户看不到「这份存稿还只在本地」这个关键信号。
  setStatus();
  window.addEventListener('online', () => { setStatus(); syncAll(); });
  window.addEventListener('offline', setStatus);

  // 缺陷 #124：按钮的点击处理同样必须在任何 await 之前绑。
  // 原先它们挂在 `await loadProgress()` 之后 —— 首屏那两个 /api 请求没回来之前，
  // 点「存一条」是**完全没反应的**：按钮看得见、点得动、一个字都不存。
  // 作者只会以为自己没点对。和上面 offline 监听是同一个首屏竞态的两端。
  document.getElementById('save').addEventListener('click', async () => {
    const ta = document.getElementById('text');
    const text = ta.value.trim();
    if (!text) return;
    // 同样因为绑得早：作者可能在 openDB() 还没回来时就点下去，
    // 这里必须自己兜住，不能把一笔灵感扔进未处理的 Promise 异常里。
    if (!db) db = await openDB();
    await addNote({
      text,
      kind: document.getElementById('kind').value,
      ts: Date.now(),
      synced: false,
    });
    ta.value = '';
    await render();
    if (navigator.onLine) syncAll();
  });

  document.getElementById('sync').addEventListener('click', syncAll);
  document.getElementById('nightly').addEventListener('click', triggerNightly);

  db = await openDB();
  await render();
  if (navigator.onLine) syncAll(); // 补上可能早于 openDB 到达的 online 事件
  await loadProgress();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  }
});

// 允许从主站带参进入：?book=<dir>
const params = new URLSearchParams(location.search);
if (params.get('book')) {
  localStorage.setItem(BOOK_KEY, params.get('book'));
}
