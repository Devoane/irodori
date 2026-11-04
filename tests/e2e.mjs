// UI test against a running server:  node tests/e2e.mjs <outdir> <model> [extra files...]
import { chromium } from 'playwright';
import fs from 'node:fs';
const [out, ...files] = process.argv.slice(2);
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 }, acceptDownloads: true });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));
const log = (...a) => console.log(...a);
const idle = () => page.waitForFunction(() => !document.querySelector('#status .spin'), null, { timeout: 900000 });
const done = async () => {
  await page.waitForFunction(() => document.querySelector('#resultsTitle').textContent.startsWith('Wyniki'), null, { timeout: 900000 });
  await idle();
};
const rows = () => page.$$eval('.rowcard:not([hidden])', (els) => els.map((e) => `${e.querySelector('h3').textContent} | ${e.querySelector('.m').textContent} | ${e.querySelector('.time')?.textContent.trim() || '-'}`));

await page.goto('http://127.0.0.1:8765/');
await page.evaluate(() => localStorage.clear());
await page.reload();
await page.screenshot({ path: `${out}/01-start.png`, fullPage: true });
const openGroups = () => page.evaluate(() => document.querySelectorAll('details.grp').forEach((d) => { d.open = true; }));
await openGroups();
await page.fill('#s_max_triangles', '200000');
await page.screenshot({ path: `${out}/02-advanced.png`, fullPage: true });
await page.setInputFiles('#file', files);
await page.waitForTimeout(500);
log('deps:', await page.$$eval('#depList li', (l) => l.map((x) => `${x.className}:${x.textContent}`)), 'analyze enabled:', await page.isEnabled('#btnAnalyze'));
if (!(await page.isEnabled('#btnAnalyze'))) await page.check('#depAllow');
await page.click('#btnAnalyze');
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/03-progress.png` });
await done();
await page.waitForTimeout(1500);
log('status:', await page.textContent('#status'), '| warnings:', await page.textContent('#warnings'));
log('rows:', await rows());
log('diag:', await page.$$eval('#diag dt', (d) => d.map((x) => `${x.textContent}=${x.nextElementSibling.textContent}`)));
await page.locator('#results').screenshot({ path: `${out}/04-results.png` });
await page.locator('#canvasWrap').screenshot({ path: `${out}/05-predicted.png` });
await page.click('#viewMode [data-v=slicer]');
await page.waitForTimeout(300);
await page.locator('#canvasWrap').screenshot({ path: `${out}/05b-slicer.png` });
await page.click('#viewMode [data-v=predicted]');

// another row
const n = (await rows()).length;
if (n > 1) { await page.click('.rowcard[data-i="1"] .chip'); await page.waitForTimeout(800); log('selected:', await page.getAttribute('.rowcard.sel', 'data-i')); }

// A/B/C neutral alternative
const alt = await page.$('.alts button:not(.on)');
if (alt) {
  const before = await alt.evaluate((b) => b.closest('.rowcard').querySelector('.m').textContent);
  await alt.click(); await idle(); await page.waitForTimeout(500);
  log('alt swap:', before, '->', await page.$eval('.rowcard.sel .m', (e) => e.textContent), await page.$eval('.rowcard.sel .alts .on', (b) => b.title));
}

// edit a swatch colour in place, then reset
await page.$eval('.rowcard[data-i="0"] input[type=color]', (i) => { i.value = '#ff8800'; i.dispatchEvent(new Event('change', { bubbles: true })); });
await idle(); await page.waitForTimeout(500);
log('edited row 0:', await page.$eval('.rowcard[data-i="0"]', (c) => c.querySelector('.hexl span').textContent + ' ' + c.querySelector('.m').textContent), 'rows', (await rows()).length);
await page.click('.rowcard[data-i="0"] [data-reset-row]'); await idle(); await page.waitForTimeout(500);
log('reset row 0:', await page.$eval('.rowcard[data-i="0"] .hexl span', (e) => e.textContent));

// use my colours
const slots = await page.$$('#mineSlots input.hex');
const mine = ['#00AEEF', '#EC008C', '#FFF200', '#FFFFFF', '#000000', '#888888', '#FF0000', '#00FF00'];
for (let i = 0; i < slots.length; i++) await slots[i].fill(mine[i]);
await page.waitForTimeout(1500); await idle(); await page.waitForTimeout(800);
log('mine row:', (await rows()).slice(-1));

// focus brush + re-analyze
await page.click('#brushToggle');
const box = await page.locator('#canvasWrap canvas').boundingBox();
const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
await page.mouse.move(cx - 30, cy); await page.mouse.down();
for (let i = 0; i <= 8; i++) await page.mouse.move(cx - 30 + i * 8, cy);
await page.mouse.up();
log('focus:', await page.textContent('#focusInfo'), '| reanalyze visible:', await page.isVisible('#btnReanalyze'));
await page.click('#brushToggle');
await page.click('#btnReanalyze');
await page.waitForTimeout(800);
await done();
log('rows after focus:', await rows());

// pose and source adjustments (instant preview)
await openGroups();
await page.fill('#s_rot_x', '90');
await page.fill('#s_z_offset', '-5');
await page.waitForTimeout(500);
await page.locator('#canvasWrap').screenshot({ path: `${out}/06-rot-sink.png` });
log('rot state:', await page.textContent('#rotState'), '|', await page.textContent('#zState'), '| reanalyze needed:', await page.isVisible('#btnReanalyze'));
await page.click('#rotAuto'); await page.click('#zAuto');
await page.selectOption('#s_rainbow', '1');
await page.click('#viewMode [data-v=source]');
await page.waitForTimeout(500);
await page.locator('#canvasWrap').screenshot({ path: `${out}/07-rainbow-preview.png` });
log('rainbow -> TIP:', await page.inputValue('#s_take_interior'), '| reanalyze needed:', await page.isVisible('#btnReanalyze'));
await page.selectOption('#s_rainbow', '0');

// Add a Base: pick a style, click again for the next size, re-analyze
await page.click('#baseList [data-base=pebble]');
await page.click('#baseList [data-base=pebble]');
log('base chip:', await page.textContent('#baseList .on'), '| reanalyze needed:', await page.isVisible('#btnReanalyze'));
await page.click('#btnReanalyze');
await page.waitForTimeout(800);
await done();
await page.waitForTimeout(1500);
log('rows with base:', (await rows()).slice(0, 2), '| diag base:', await page.$eval('#diag', (d) => [...d.querySelectorAll('dt')].find((x) => x.textContent === 'Podstawka')?.nextElementSibling.textContent));
await page.click('#viewMode [data-v=source]');
await page.waitForTimeout(1500);
await page.locator('#canvasWrap').screenshot({ path: `${out}/08-base-source.png` });
await page.click('#viewMode [data-v=predicted]');
await page.waitForTimeout(500);
await page.locator('#canvasWrap').screenshot({ path: `${out}/09-base-predicted.png` });

// 3MF download of the selected row
const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 300000 }), page.click('.rowcard.sel [data-act="3mf"]')]);
const path = `${out}/${dl.suggestedFilename()}`;
await dl.saveAs(path);
log('3mf:', dl.suggestedFilename(), fs.statSync(path).size, 'B');

log('console errors:', errors.length ? errors : 'none');
await browser.close();
