// Optional Playwright/Chromium verification after validate_supervision.py.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/supervised-review');
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], remote = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('request', r => { if (/^https?:/.test(r.url())) remote.push(r.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'report.html')).href);
      await page.locator('#gap-summary').focus(); await page.keyboard.press('Enter');
      assert.match(await page.locator('#gap-summary').innerText(), /4 calls.*3 reasons/);
      assert.equal(await page.locator('.gap-card').count(), 4);
      await page.locator('#gap-reason').selectOption('unknown_receiver');
      assert.equal(await page.locator('.gap-card').count(), 2);
      await page.locator('#gap-entrypoint').selectOption('uncertain_workflow.py:run');
      assert.equal(await page.locator('.gap-card').count(), 1);
      await page.locator('#gap-entrypoint').selectOption('__none__');
      assert.equal(await page.locator('.gap-card').count(), 1);
      assert.match(await page.locator('.gap-card').innerText(), /none found through known edges/);
      await page.locator('#gap-entrypoint').selectOption('');
      await page.locator('#gap-reason').selectOption('callback_parameter');
      const source = page.locator('.gap-card button');
      await source.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#detail-title').innerText(), 'Workflow.execute');
      assert.match(await page.locator('#detail').innerText(), /callback parameter/);
      assert.equal(await page.locator('.graph-node:focus').getAttribute('data-node'), 'uncertain_workflow.py:Workflow.execute');
      await page.locator('#gap-reason').selectOption('');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const boxes = await page.locator('.graph-node').evaluateAll(nodes => nodes.map(n => {
        const b = n.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom };
      }));
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        assert.ok(a.r <= b.x || b.r <= a.x || a.b <= b.y || b.b <= a.y, 'Graph nodes overlap');
      }
      await page.screenshot({ path: path.join(directory, `uncertainty-${viewport.width}.png`), fullPage: true });
      assert.deepEqual(errors, []); assert.deepEqual(remote, []);
      results.push({ viewport, reasons: 'passed', entrypointFilter: 'passed', sourceNavigation: 'passed', keyboard: 'passed', overflow: false, errors, remote });
      await page.close();
    }
  } finally { await browser.close(); }
  await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
