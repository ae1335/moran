/**
 * 导出预检交互验证：真实点击「导出预检」按钮，检查面板渲染 + 控制台无错误。
 * 用法：node scripts/click-precheck.mjs [baseUrl]
 */
import { chromium } from '/home/yan/dsh-src/node_modules/.pnpm/playwright-core@1.61.1/node_modules/playwright-core/index.mjs';

const BASE = process.argv[2] || 'http://127.0.0.1:8791';
const dir = encodeURIComponent('novel-《凡人问道》');

const browser = await chromium.launch({
  executablePath: '/home/yan/.cache/ms-playwright/chromium-1194/chrome-linux/chrome',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errs = [];
page.on('console', (m) => m.type() === 'error' && errs.push(m.text()));
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

await page.goto(`${BASE}/#/b/${dir}/daily`, { waitUntil: 'networkidle' });
await page.getByRole('button', { name: '导出预检' }).click();
await page.waitForTimeout(1500);

const text = await page.locator('.precheck-list').innerText().catch(() => '(未渲染)');
console.log('预检面板:\n' + text.slice(0, 500));
await page.screenshot({ path: 'docs/screenshots/07-daily.png', fullPage: true });
console.log('errors:', errs.length ? errs : 'none');
await browser.close();
