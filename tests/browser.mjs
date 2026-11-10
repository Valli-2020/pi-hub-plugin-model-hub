// Drives the Models tab in Chromium. Started by harness.py.
import { createRequire } from 'module';
if (!process.env.PLAYWRIGHT_ROOT) { console.error('Set PLAYWRIGHT_ROOT to a folder with node_modules/playwright'); process.exit(2); }
const require = createRequire(process.env.PLAYWRIGHT_ROOT + '/');
const { chromium } = require('playwright');
const { HUB, ADMIN, VIEWER, AGENT_PID, SHOTS } = process.env;
let passed = 0;
function check(name, cond, detail) {
  if (!cond) { console.error('FAIL:', name, detail === undefined ? '' : detail); process.exit(1); }
  passed++; console.log('ok  ', name);
}
const browser = await chromium.launch();
const errors = [];

async function open(token, opts) {
  const ctx = await browser.newContext(opts);
  await ctx.addInitScript(t => { try { localStorage.setItem('pi_hub_token', t); } catch (e) { /* sandboxed frame */ } }, token);
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/401|Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(HUB + '/');
  await page.getByText('Models', { exact: true }).first().click();
  const frame = page.frameLocator('iframe.plg-frame');
  await frame.locator('.row').first().waitFor({ timeout: 15000 });
  return { ctx, page, frame };
}
const row = (frame, name) => frame.locator('.row').filter({ has: frame.locator('.name', { hasText: name }) }).first();
async function overflow(frame) {
  return frame.locator('body').evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

// Admin
const a = await open(ADMIN, { viewport: { width: 1280, height: 900 }, colorScheme: 'dark' });
await a.frame.locator('.tile').first().waitFor();
check('five monitor tiles', await a.frame.locator('.tile').count() === 5);
for (const n of ['llama3.1:8b', 'embeddinggemma:latest', 'ComfyUI', 'S2 Pro TTS'])
  check('listed: ' + n, await row(a.frame, n).count() === 1);
const chipText = await a.frame.locator('.chips').innerText();
for (const t of ['All', 'Text', 'Image', 'Video', 'Audio', 'Embedding'])
  check('type filter: ' + t, chipText.includes(t), chipText);

await a.frame.locator('.btn.chip', { hasText: 'Image' }).click();
await a.frame.locator('.row').first().waitFor();
check('filter narrows the list', await a.frame.locator('.row').count() === 1);
await a.frame.locator('.btn.chip', { hasText: 'All' }).click();

// Start a text model
await row(a.frame, 'llama3.1').locator('.btn.go').click();
await row(a.frame, 'llama3.1').locator('.btn.stop').waitFor({ timeout: 10000 });
check('model started from the button', (await row(a.frame, 'llama3.1').innerText()).includes('on GPU'));
await row(a.frame, 'llama3.1').locator('.badge', { hasText: 'Ready' }).waitFor({ timeout: 8000 });
check('loaded model shows the Ready badge', true);
check('unloaded model shows Not loaded', (await row(a.frame, 'embeddinggemma').locator('.badge').innerText()) === 'Not loaded');

// Engine needs a second click, and says why
await row(a.frame, 'ComfyUI').locator('.btn.go').click();
const armedBtn = row(a.frame, 'ComfyUI').locator('.btn.armed');
await armedBtn.waitFor();
check('first click explains what stops', (await armedBtn.innerText()).includes('llama3.1'));
await armedBtn.click();
await row(a.frame, 'ComfyUI').locator('.btn.stop').waitFor({ timeout: 12000 });
await a.frame.locator('.line', { hasText: 'ComfyUI holds the GPU' }).first().waitFor({ timeout: 8000 });
check('GPU owner is shown', true);
await row(a.frame, 'llama3.1').locator('.row-state', { hasText: 'Ollama is stopped' }).waitFor({ timeout: 8000 });
check('ollama models marked while it is stopped', true);
if (SHOTS) await a.page.screenshot({ path: SHOTS + '/admin-dark.png', fullPage: true });

await row(a.frame, 'ComfyUI').locator('.btn.stop').click();
await row(a.frame, 'ComfyUI').locator('.btn.go').waitFor({ timeout: 12000 });
await row(a.frame, 'llama3.1').locator('.badge', { hasText: 'Not loaded' }).waitFor({ timeout: 8000 });
check('stopping the engine brings ollama back', true);

// Download
await a.frame.locator('.form input').first().fill('qwen3:4b');
await a.frame.locator('.btn.primary', { hasText: 'Download' }).click();
await a.frame.locator('.job', { hasText: 'qwen3:4b' }).first().waitFor({ timeout: 8000 });
check('download appears in the list', true);
await a.frame.locator('.job.j-done', { hasText: 'qwen3:4b' }).waitFor({ timeout: 12000 });
check('download finishes', true);

await a.frame.locator('.panel select').first().selectOption('file');
check('file mode shows a folder picker', await a.frame.locator('.panel select').nth(1).isVisible());
check('folder picker lists targets', (await a.frame.locator('.panel select').nth(1).innerText()).includes('Video engine'));
await a.frame.locator('.form input').first().fill('http://bad.example/x');
await a.frame.locator('.btn.primary', { hasText: 'Download' }).click();
await a.frame.locator('.alert', { hasText: 'must start with https' }).waitFor({ timeout: 6000 });
check('server error reaches the user', true);

// Add and remove an engine
await a.frame.locator('details.panel summary').click();
const inputs = a.frame.locator('details.panel input');
await inputs.nth(0).fill('Whisper STT');
await inputs.nth(1).fill('speech');
await inputs.nth(2).fill('whisper');
await a.frame.locator('details.panel .btn.primary').click();
await row(a.frame, 'Whisper STT').waitFor({ timeout: 8000 });
check('added engine shows up', true);
check('new type gets its own filter', (await a.frame.locator('.chips').innerText()).includes('Speech'));
await row(a.frame, 'Whisper STT').locator('.btn.quiet').click();
await row(a.frame, 'Whisper STT').locator('.btn.armed').click();
await a.frame.locator('.name', { hasText: 'Whisper STT' }).waitFor({ state: 'detached', timeout: 8000 });
check('engine removed after two clicks', true);
check('no horizontal scroll on desktop', await overflow(a.frame) <= 0);

// Viewer
const v = await open(VIEWER, { viewport: { width: 1000, height: 800 }, colorScheme: 'light' });
await v.frame.locator('.tile').first().waitFor();
await v.page.waitForTimeout(800);
check('viewer has no action buttons', await v.frame.locator('.row-actions').count() === 0);
check('viewer has no download or add panels', await v.frame.locator('.panel:visible').count() === 0);
if (SHOTS) await v.page.screenshot({ path: SHOTS + '/viewer-light.png', fullPage: true });
await v.ctx.close();

// Phone width
const p = await open(ADMIN, { viewport: { width: 390, height: 844 }, colorScheme: 'light' });
await p.frame.locator('.tile').first().waitFor();
await p.page.waitForTimeout(600);
check('no horizontal scroll at phone width', await overflow(p.frame) <= 0, await overflow(p.frame));
if (SHOTS) await p.page.screenshot({ path: SHOTS + '/phone.png', fullPage: true });
await p.ctx.close();

// Agent gone
if (AGENT_PID) {
  process.kill(Number(AGENT_PID));
  await a.frame.locator('.offline h2').waitFor({ timeout: 12000 });
  check('offline view appears', (await a.frame.locator('.offline').innerText()).includes('not reachable'));
  check('wake button offered', await a.frame.locator('.offline .btn.go').count() === 1);
  if (SHOTS) await a.page.screenshot({ path: SHOTS + '/offline.png' });
}
check('no script errors in the page', errors.length === 0, errors);
await browser.close();
console.log(`\nAll ${passed} browser checks passed`);
