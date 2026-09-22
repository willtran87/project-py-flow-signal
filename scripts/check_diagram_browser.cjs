// Optional browser verification: requires Playwright, not a FlowSignal runtime dependency.
// Usage: node scripts/check_diagram_browser.cjs <checkout-report.html> <artifact-directory>
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const report = path.resolve(process.argv[2]);
  const artifacts = path.resolve(process.argv[3] || '.artifacts/visual');
  await fs.mkdir(artifacts, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
      const errors = [], remoteRequests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => { if (/^https?:/.test(request.url())) remoteRequests.push(request.url()); });
      await page.goto(pathToFileURL(report).href);
      await page.locator('.graph-node').first().waitFor();
      assert.equal(await page.locator('.graph-node').count(), 6);
      assert.match(await page.locator('#scan-status').innerText(), /Scan completed within its configured scope/);
      assert.match(await page.locator('#view-status').innerText(), /6 nodes shown/);
      assert.match(await page.locator('#detail').innerText(), /When:/);
      const geometry = await page.evaluate(() => {
        const cards = [...document.querySelectorAll('.graph-node')].map(node => {
          const rect = node.querySelector('.card').getBoundingClientRect();
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        });
        let overlaps = 0;
        for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
          const a = cards[i], b = cards[j];
          if (a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y) overlaps++;
        }
        return { documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth, overlaps };
      });
      assert.equal(geometry.overlaps, 0);
      assert.ok(geometry.documentWidth <= geometry.viewportWidth + 1, 'Unexpected page-level horizontal overflow');
      await page.screenshot({ path: path.join(artifacts, `diagram-${viewport.width}.png`), fullPage: true });

      await page.locator('[data-node="instrumented_checkout.py:checkout"]').focus();
      await page.keyboard.press('Enter');
      assert.match(await page.locator('#detail').innerText(), /INFO log/);
      await page.locator('[data-node="instrumented_checkout.py:fetch_stock"]').click();
      assert.match(await page.locator('#detail').innerText(), /recognized trace scope/);
      await page.locator('#search').fill('fetch_price');
      await page.locator('#results button').first().click();
      await page.locator('#callers').uncheck();
      assert.equal(await page.locator('.graph-node').count(), 2);
      assert.match(await page.locator('#graph-title').innerText(), /fetch_price/);
      await page.locator('#callers').check();
      assert.equal(await page.locator('.graph-node').count(), 6);
      const before = await page.locator('#graph').evaluate(node => node.getBoundingClientRect().width);
      await page.locator('#zoom-in').click();
      assert.ok(await page.locator('#graph').evaluate(node => node.getBoundingClientRect().width) > before);
      await page.locator('#fit').click();
      const downloadPromise = page.waitForEvent('download');
      await page.locator('#download').click();
      const download = await downloadPromise;
      await download.saveAs(path.join(artifacts, `diagram-${viewport.width}.svg`));
      assert.equal(download.suggestedFilename(), 'flowsignal-execution-path.svg');
      assert.deepEqual(errors, []);
      assert.deepEqual(remoteRequests, []);
      results.push({ viewport, ...geometry, nodes: 6, navigation: 'passed', svgExport: 'passed', networkRequests: 0, consoleErrors: 0 });
      await page.close();
    }
    await fs.writeFile(path.join(artifacts, 'verification.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
