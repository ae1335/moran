/**
 * PWA / 夜审 / 索引漂移 端到端冒烟（真实浏览器 + 真实服务端）
 *
 * 覆盖 docs/VERIFICATION.md 里自认的两个缺口：
 *   - 「PWA 离线队列未做真机验证」 -> Chromium 真实断网/联网切换 + IndexedDB 队列
 *   - 「定时任务未实跑」           -> 真打 POST /jobs/nightly 并核对章纲状态流转
 * 外加 §8.5 索引 hash 对账：造正文漂移 -> verify 抓出 -> build 修复 -> verify 归零
 *
 * 用法：node scripts/pwa-smoke.mjs            # 隔离模式（默认，推荐）
 *       node scripts/pwa-smoke.mjs <baseUrl>  # 挂到已启动的服务上
 *
 * 隔离模式把 ws/ 整体复制到 .runtime-test/pwa-smoke-ws 再起临时服务，
 * 演示书的 git 历史与真相源**零污染**；结束时删除临时目录并校验
 * 真实 ws/ 的 HEAD 与工作区状态和开跑前逐字一致。
 * 末行输出 RESULT=PASS | RESULT=FAIL，退出码 0/1。
 */
import { chromium } from '/home/yan/dsh-src/node_modules/.pnpm/playwright-core@1.61.1/node_modules/playwright-core/index.mjs';
import { spawn, execFileSync } from 'node:child_process';
import { cpSync, rmSync, existsSync, readFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';

const CHROME = '/home/yan/.cache/ms-playwright/chromium-1194/chrome-linux/chrome';
const BOOK = 'novel-《凡人问道》';
const REAL_BOOK_DIR = path.resolve('ws/novels', BOOK);
const TMP_WS = path.resolve('.runtime-test/pwa-smoke-ws');
const SHOT = 'docs/screenshots';

const attachUrl = process.argv[2] || '';
const hermetic = !attachUrl;
const PORT = Number(process.env.PWA_SMOKE_PORT || 8799);
const BASE = (attachUrl || 'http://127.0.0.1:' + PORT).replace(/\/$/, '');
const ROOT = hermetic ? TMP_WS : path.resolve('ws');
const BOOK_DIR = path.join(ROOT, 'novels', BOOK);

const steps = [];
const fails = [];
function ok(name, detail) {
  steps.push({ name, pass: true });
  console.log('  \u2713 ' + name + (detail ? ' \u2014 ' + detail : ''));
}
function bad(name, detail) {
  steps.push({ name, pass: false });
  fails.push(name + (detail ? ' \u2014 ' + detail : ''));
  console.log('  \u2717 ' + name + (detail ? ' \u2014 ' + detail : ''));
}
function check(name, cond, detail) {
  if (cond) ok(name, detail); else bad(name, detail);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 端口是否已被占用。
 * 缺陷 #51 的教训：如果这里不复用探测而直接起服务，绑定失败只会在日志里留一行
 * Errno 98，而后续 /api/health 会命中**残留服务**并返回 ok，整轮断言打在错误对象上。
 */
async function portInUse(port) {
  const net = await import('node:net');
  return await new Promise((resolve) => {
    const s = net.createServer();
    s.once('error', (e) => resolve(e.code === 'EADDRINUSE'));
    s.once('listening', () => s.close(() => resolve(false)));
    s.listen(port, '127.0.0.1');
  });
}

async function api(p, opts) {
  const r = await fetch(BASE + p, opts);
  const text = await r.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) { /* 非 JSON */ }
  return { status: r.status, json: json, text: text };
}
const B = encodeURIComponent(BOOK);
const urls = {
  verify: '/api/books/' + B + '/index/verify',
  build: '/api/books/' + B + '/index/build',
  nightly: '/api/books/' + B + '/jobs/nightly',
  outline1: '/api/books/' + B + '/outline/1',
};

function gitState(dir) {
  try {
    const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir, encoding: 'utf8' }).trim();
    const st = execFileSync('git', ['status', '--porcelain'], { cwd: dir, encoding: 'utf8' });
    return head + '|' + st;
  } catch (e) {
    return '<git-unavailable>';
  }
}

let child = null;
const realBefore = gitState(REAL_BOOK_DIR);

console.log('=== PWA / 夜审 / 索引漂移 端到端冒烟 ===');
console.log('mode = ' + (hermetic ? 'isolated (临时 ws 副本)' : 'attach') + ' | base = ' + BASE);

try {
  if (hermetic) {
    rmSync(TMP_WS, { recursive: true, force: true });
    mkdirSync(path.dirname(TMP_WS), { recursive: true });
    cpSync(path.resolve('ws'), TMP_WS, { recursive: true });
    console.log('临时工作区 -> ' + TMP_WS);

    // 端口预检（缺陷 #51）：残留的 serve 进程会让下面的健康检查命中**别人的服务**，
    // 于是整轮断言打在错误对象上还报 PASS。宁可在这里红，也不要假绿。
    if (await portInUse(PORT)) {
      throw new Error(
        '端口 ' + PORT + ' 已被占用，拒绝继续：健康检查会命中别人的服务，导致本次验证测错对象。\n' +
        '  换端口：PWA_SMOKE_PORT=' + (PORT + 10) + ' node scripts/pwa-smoke.mjs');
    }
    child = spawn('python3', ['-m', 'server.cli', '--root', TMP_WS, 'serve', '--port', String(PORT)], {
      cwd: path.resolve('.'),
      env: Object.assign({}, process.env, { PYTHONPATH: 'vendor/pylib:.' }),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let log = '';
    child.stdout.on('data', (d) => { log += d.toString(); });
    child.stderr.on('data', (d) => { log += d.toString(); });
    let up = false;
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      if (child.exitCode !== null) break;
      const h = await api('/api/health').catch(() => null);
      if (h && h.status === 200) { up = true; break; }
    }
    if (!up) throw new Error('临时服务未起来（port ' + PORT + '）:\n' + log.slice(-800));
    // 服务身份校验：health 回显的 root 必须就是刚才复制的临时 ws。
    const health = await api('/api/health');
    const gotRoot = health.json && health.json.root;
    const wantRoot = path.resolve(TMP_WS);
    if (gotRoot !== wantRoot) {
      throw new Error('服务身份不符：health.root=' + gotRoot + '，期望 ' + wantRoot +
        '\n  说明 :' + PORT + ' 上的服务不是本次启动的，继续跑只会得到假绿。');
    }
    console.log('临时服务已就绪 :' + PORT + '（root 已校验）');
  }

  // ==================================================== 1. 索引基线
  console.log('\n--- 1/6 索引 hash 对账（基线）---');
  const v0 = await api(urls.verify);
  check('GET /index/verify 返回 200', v0.status === 200, 'status=' + v0.status);
  check('基线索引无漂移', !!(v0.json && v0.json.ok === true && (v0.json.drifted || []).length === 0),
    'ok=' + (v0.json && v0.json.ok) + ' drifted=' + JSON.stringify(v0.json && v0.json.drifted));

  // ==================================================== 2. 造漂移 -> 修复
  console.log('\n--- 2/6 制造正文漂移 -> verify 抓出 -> build 修复 ---');
  const ch8 = path.join(BOOK_DIR, 'chapters/ch-0008.md');
  const origCh8 = readFileSync(ch8);
  const fs = await import('node:fs');
  fs.writeFileSync(ch8, Buffer.concat([origCh8, Buffer.from('\n<!-- drift-probe -->\n')]));

  const v1 = await api(urls.verify);
  check('改正文后 verify 必须报漂移', !!(v1.json && v1.json.ok === false && (v1.json.drifted || []).indexOf(8) >= 0),
    'ok=' + (v1.json && v1.json.ok) + ' drifted=' + JSON.stringify(v1.json && v1.json.drifted));

  const b1 = await api(urls.build, { method: 'POST' });
  check('POST /index/build 成功', b1.status === 200 && !(b1.json && b1.json.error),
    'status=' + b1.status + ' changed=' + JSON.stringify(b1.json && b1.json.changed));

  const v2 = await api(urls.verify);
  check('重建后漂移归零', !!(v2.json && v2.json.ok === true && (v2.json.drifted || []).length === 0),
    'ok=' + (v2.json && v2.json.ok) + ' drifted=' + JSON.stringify(v2.json && v2.json.drifted));

  fs.writeFileSync(ch8, origCh8);

  // ==================================================== 3. 夜审真跑
  console.log('\n--- 3/6 夜审批处理（POST /jobs/nightly）---');
  // 缺陷 #71 之后状态机也挂在通用写入口上，跳迁会被 409。
  // 演示书的 ch-0001 是 final，必须按迁移表逐级回退：final → reviewed → drafted。
  const putR = await api(urls.outline1, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status: 'reviewed' }),
  });
  check('把 ch-0001 从 final 回退到 reviewed',
    !!(putR.status === 200 && putR.json && putR.json.status === 'reviewed'),
    'status=' + putR.status + ' body=' + JSON.stringify(putR.json && putR.json.status));
  const put = await api(urls.outline1, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status: 'drafted' }),
  });
  check('把 ch-0001 置为 drafted', !!(put.status === 200 && put.json && put.json.status === 'drafted'),
    'status=' + (put.json && put.json.status));
  // 顺手钉住 #71：跳迁必须被拒
  const jump = await api(urls.outline1, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status: 'published' }),
  });
  check('drafted → published 跳迁被 409 拒绝', jump.status === 409, 'status=' + jump.status);

  const night = await api(urls.nightly, { method: 'POST' });
  check('POST /jobs/nightly 返回 200', night.status === 200, 'status=' + night.status);
  const reviewed = (night.json && night.json.reviewed) || [];
  check('夜审真的审了 ch-0001', reviewed.length === 1 && reviewed[0].ch === 1 && !reviewed[0].error,
    JSON.stringify(reviewed));
  check('夜审内联返回 index_verify 且无漂移', !!(night.json && night.json.index_verify && night.json.index_verify.ok),
    JSON.stringify(night.json && night.json.index_verify && night.json.index_verify.drifted));
  // 缺陷 #110：顶层 ok / warnings 是新的成败口径，端到端必须一起盯
  check('夜审响应顶层带 ok=true 且无告警', night.json && night.json.ok === true
    && (!night.json.warnings || night.json.warnings.length === 0),
    JSON.stringify(night.json && night.json.warnings));

  const o1 = await api(urls.outline1);
  check('审稿后章纲自动升为 reviewed', !!(o1.json && o1.json.status === 'reviewed'),
    'status=' + (o1.json && o1.json.status));

  // ==================================================== 4. PWA 离线队列
  console.log('\n--- 4/6 PWA 离线存稿 -> 联网自动同步 ---');
  const browser = await chromium.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const ctx = await browser.newContext({ viewport: { width: 430, height: 932 } });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const http404 = [];
  let offlineWindow = false;
  // 缺陷 #125：离线窗口内页面自己发出的 /api 请求必然 net::ERR_INTERNET_DISCONNECTED，
  // Chrome 会把它记成一条 console error。这是**被测行为本身**（不该发），不是 JS 缺陷，
  // 却让「全程无 JS 报错」这一条在离线步骤必红 —— 整条链路被一个口径错误判成 FAIL。
  function isOfflineNoise(t) {
    return offlineWindow && /net::ERR_INTERNET_DISCONNECTED|Failed to fetch|ERR_NETWORK_CHANGED|ERR_NAME_NOT_RESOLVED/.test(t);
  }
  page.on('console', function (m) {
    if (m.type() === 'error' && !isOfflineNoise(m.text())) consoleErrors.push(m.text());
  });
  page.on('pageerror', function (e) { consoleErrors.push('[pageerror] ' + e.message); });
  page.on('response', function (r) {
    if (r.status() === 404 && !offlineWindow) http404.push(r.url());
  });

  const NOTE = '矿洞深处的诵经声其实是阵眼（PWA 冒烟 ' + Date.now() + '）';
  await page.goto(BASE + '/pwa/?book=' + encodeURIComponent(BOOK), { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(function () {
    const el = document.getElementById('pendingCount');
    return el && el.textContent === '0';
  }, null, { timeout: 20000 });
  ok('PWA 外壳加载（待同步 = 0）');
  const savedBook = await page.evaluate(function () { return localStorage.getItem('wb.book'); });
  check('?book= 已写入 localStorage', savedBook === BOOK, String(savedBook));

  // 回归防线：首屏加载期间掉线，状态灯必须跟着变。
  // 曾因 DOMContentLoaded 里先 await loadProgress() 再绑 offline 监听，
  // 导致首屏窗口期内掉线事件被丢弃、状态灯一直停在「在线」。
  offlineWindow = true;
  await ctx.setOffline(true);
  await page.waitForFunction(function () {
    const el = document.getElementById('status');
    return el && el.textContent.indexOf('离线') >= 0;
  }, null, { timeout: 8000 }).catch(function () {});
  const offlineState = await page.evaluate(function () {
    return { onLine: navigator.onLine, text: document.getElementById('status').textContent };
  });
  check('断网后 navigator.onLine 变 false', offlineState.onLine === false, JSON.stringify(offlineState));
  check('断网后状态灯显示离线（首屏竞态回归）', offlineState.text.indexOf('离线') >= 0, offlineState.text);

  await page.fill('#text', NOTE);
  await page.selectOption('#kind', 'foreshadow');
  await page.click('#save');
  await page.waitForFunction(function () {
    return document.getElementById('pendingCount').textContent === '1';
  }, null, { timeout: 15000 });
  ok('离线写入 IndexedDB（待同步 = 1）');

  const inspPath = path.join(BOOK_DIR, 'materials/inspiration.md');
  check('离线期间服务端未被写入', !existsSync(inspPath));

  mkdirSync(SHOT, { recursive: true });
  await page.screenshot({ path: SHOT + '/18-pwa-offline.png', fullPage: false });
  console.log('  截图 -> ' + SHOT + '/18-pwa-offline.png');

  await ctx.setOffline(false);
  offlineWindow = false;
  await page.waitForFunction(function () {
    return document.getElementById('pendingCount').textContent === '0';
  }, null, { timeout: 25000 });
  ok('联网后自动同步（待同步归零）');
  check('恢复联网后状态灯回到在线', (await page.locator('#status').innerText()).indexOf('在线') >= 0);

  check('灵感已落盘 materials/inspiration.md',
    existsSync(inspPath) && readFileSync(inspPath, 'utf8').indexOf(NOTE) >= 0);
  check('落盘带伏笔标记', existsSync(inspPath) && readFileSync(inspPath, 'utf8').indexOf('[伏笔]') >= 0);
  const syncedText = await page.locator('#synced').innerText();
  check('已同步列表出现该条', syncedText.indexOf('诵经声其实是阵眼') >= 0);
  await page.screenshot({ path: SHOT + '/18-pwa.png', fullPage: false });
  console.log('  截图 -> ' + SHOT + '/18-pwa.png');

  // ==================================================== 5. 进度面板 + 夜审按钮
  console.log('\n--- 5/6 PWA 进度面板与远程夜审按钮 ---');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(function () {
    const t = document.getElementById('progress');
    return t && t.innerText.indexOf('凡人问道') >= 0;
  }, null, { timeout: 20000 });
  const progress = await page.locator('#progress').innerText();
  check('进度面板读到书信息', progress.indexOf('凡人问道') >= 0 && progress.indexOf('章') >= 0);
  check('进度面板显示三级存稿箱', /待审/.test(progress) && /已审/.test(progress) && /定稿未发/.test(progress));
  check('进度面板显示已发布计数', /已发布/.test(progress), progress.replace(/\n/g, ' / ').slice(0, 140));

  const dialogs = [];
  page.on('dialog', async function (d) { dialogs.push(d.message()); await d.accept(); });
  await page.click('#nightly');
  await sleep(2500);
  check('PWA 夜审按钮返回提示', dialogs.some(function (m) { return m.indexOf('夜审已触发') >= 0; }),
    dialogs.join(' | ') || '(无弹窗)');

  // 图标：浏览器无 <link rel=icon> 时会自动请求 /favicon.ico，
  // 这里同时校验显式 svg 与 ico 回退，避免每次打开都留一条 404 报错。
  const iconRes = await api('/pwa/favicon.svg');
  check('PWA 图标 favicon.svg 可访问', iconRes.status === 200, 'status=' + iconRes.status);
  const icoRes = await api('/favicon.ico');
  check('/favicon.ico 不再 404', icoRes.status === 200, 'status=' + icoRes.status);
  const manifestRes = await api('/pwa/manifest.webmanifest');
  const manifestIcons = (manifestRes.json && manifestRes.json.icons) || [];
  check('manifest 已声明图标（可安装）', manifestIcons.length > 0,
    'icons=' + JSON.stringify(manifestIcons.map(function (i) { return i.src; })));

  check('全程无 404 资源', http404.length === 0, http404.slice(0, 3).join(' | '));
  check('PWA 全程无 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' || '));

  await browser.close();
} catch (e) {
  bad('脚本异常', String(e && e.stack ? e.stack.split('\n').slice(0, 3).join(' <- ') : e));
} finally {
  if (child && child.exitCode === null) {
    child.kill('SIGTERM');
    await sleep(800);
    if (child.exitCode === null) child.kill('SIGKILL');
  }

  if (hermetic) {
    // 真实演示书必须零污染
    const realAfter = gitState(REAL_BOOK_DIR);
    check('真实 ws 演示书 git 零污染', realAfter === realBefore,
      realAfter === realBefore ? 'HEAD/工作区一致' : 'BEFORE=' + JSON.stringify(realBefore) + ' AFTER=' + JSON.stringify(realAfter));
    rmSync(TMP_WS, { recursive: true, force: true });
    check('临时工作区已清理', !existsSync(TMP_WS));
  } else {
    check('attach 模式不做隔离校验', true, '跑在 ' + ROOT);
  }
}

console.log('\n---- 汇总 ----');
console.log('检查项: ' + steps.length + ' | 通过: ' + steps.filter(function (s) { return s.pass; }).length + ' | 失败: ' + fails.length);
for (const f of fails) console.log(' \u2717 ' + f);
console.log(fails.length ? 'RESULT=FAIL' : 'RESULT=PASS');
process.exit(fails.length ? 1 : 0);
