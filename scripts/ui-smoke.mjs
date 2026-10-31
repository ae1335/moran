/**
 * UI 冒烟：真实浏览器打开工作台，逐页检查控制台错误 + 截图。
 * 用法：node scripts/ui-smoke.mjs [baseUrl]
 */
import { chromium } from '/home/yan/dsh-src/node_modules/.pnpm/playwright-core@1.61.1/node_modules/playwright-core/index.mjs';

const BASE = process.argv[2] || 'http://127.0.0.1:8791';
const OUT = process.argv[3] || 'docs/screenshots';

const browser = await chromium.launch({
  executablePath: '/home/yan/.cache/ms-playwright/chromium-1194/chrome-linux/chrome',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(`[console] ${m.text()}`); });
page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message}`));
page.on('requestfailed', (r) => {
  if (!r.url().includes('favicon')) errors.push(`[requestfailed] ${r.url()} ${r.failure()?.errorText}`);
});

const results = [];
async function shot(name, note) {
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  results.push({ name, url: page.url(), note });
}

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
console.log('landing url =', page.url());
console.log('title =', await page.title());

const bodyText = (await page.locator('body').innerText()).replace(/\n+/g, ' | ');
console.log('body =', bodyText.slice(0, 600));
await shot('01-shelf', '书架');

// 直接走 hash 路由遍历（不依赖点击，避免选择器脆弱）
const bookEl = await page.locator('text=凡人问道').first();
const hasBook = await bookEl.count();
console.log('found book card =', hasBook > 0);

const B = '/b/novel-%E3%80%8A%E5%87%A1%E4%BA%BA%E9%97%AE%E9%81%93%E3%80%8B' + '%2F';  // 占位，下面 replace 掉尾部
const R = (p) => '#/b/' + encodeURIComponent('novel-《凡人问道》') + p;

const routes = [
  ['#/shelf', '01-shelf', '书架'],
  [R('/write/1'), '02-write', '写正文'],
  [R('/review/1'), '03-review', '审稿'],
  [R('/curate/1'), '04-curate', '沉淀'],
  [R('/outline'), '05-outline', '章纲'],
  [R('/foreshadow'), '06-foreshadow', '伏笔'],
  [R('/daily'), '07-daily', '日更'],
  [R('/truth'), '08-truth', '真相源'],
  [R('/benchmark'), '09-benchmark', '拆书'],
  [R('/names'), '14-names', '起名'],
  [R('/portfolio'), '17-portfolio', '作品集'],
  [R('/feedback'), '10-feedback', '反馈'],
  ['#/usage', '11-usage', '用量'],
  ['#/providers', '12-providers', '路由'],
  ['#/bogus', '13-notfound', '404 兜底'],
];

const seen = new Map();
for (const [hash, name, note] of routes) {
  errors.length = 0;
  await page.goto(BASE + '/' + hash, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(900);
  const txt = (await page.locator('body').innerText()).replace(/\n+/g, ' | ');
  await shot(name, note);
  seen.set(name, { note, errors: [...errors], snippet: txt.slice(0, 200) });
  console.log(`\n=== ${note} (${hash}) ===`);
  console.log('  text:', txt.slice(0, 180));
  console.log('  errors:', errors.length ? errors.slice(0, 3).join(' || ') : 'none');
}

let chartFail = 0;

// ---- §7 第 8 条：图表是手写 CSS，无交互缩放/浮层（已知限制，钉死） ----
// 柱体 :title 是原生 tooltip，不是自绘浮层；这里断言「没有自绘交互层 + 没有 click/mouse 处理」。
// 将来若引入图表库或加了交互，本段会红，提醒回来改 §7 第 8 条。
{
  const daily = [...seen.keys()].find((k) => k.includes('daily'));
  const chartInfo = await (async () => {
    await page.goto(BASE + '/#/b/' + encodeURIComponent('novel-《凡人问道》') + '/daily', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(900);
    const bar = page.locator('.bar-wrap').first();
    if (!(await bar.count())) return { bars: 0 };
    return {
      bars: await page.locator('.bar-wrap').count(),
      hasTitle: !!(await bar.getAttribute('title')),
      barLabels: await page.locator('.bar-label').count(),
      // 自绘浮层通常带 tooltip/tip/legend/canvas/svg 之类节点
      overlayNodes: await page.locator('.tooltip, .tip, .legend, canvas, svg.chart').count(),
      // 柱体高度是否靠 inline style（手写 CSS 柱图的特征）
      inlineHeight: !!(await page.locator('.bar').first().getAttribute('style')),
    };
  })();
  console.log('\n---- §7 第 8 条：图表形态 ----');
  console.log('  ' + JSON.stringify(chartInfo));
  const chartChecks = [
    ['字数曲线渲染出柱体', chartInfo.bars > 0],
    ['柱体带原生 title 提示（非完全无提示）', chartInfo.hasTitle === true],
    ['无自绘浮层/canvas/legend 节点', chartInfo.overlayNodes === 0],
    ['柱高靠 inline style 计算（手写 CSS 柱图）', chartInfo.inlineHeight === true],
  ];
  for (const [n, ok] of chartChecks) console.log('  ' + (ok ? '✓' : '✗') + ' ' + n);
  if (chartChecks.some(([, ok]) => !ok)) chartFail = 1;
  if (!daily) console.log('  (注意: 路由表中没有 daily 页)');
}

const bad = [...seen.entries()].filter(([, v]) => v.errors.length);
console.log('\n---- 汇总 ----');
console.log('页面数:', seen.size, '有错误的页面:', bad.length);
if (bad.length) for (const [k, v] of bad) console.log(' ✗', k, v.errors.slice(0, 2));
console.log('图表形态断言:', chartFail ? '✗ 有失败' : '✓ 全部通过');

await browser.close();
process.exit(bad.length || chartFail ? 1 : 0);
