// Optional Playwright verification of the real self-scan, after scripts/dogfood.py.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/dogfood');
  const html = await fs.readFile(path.join(directory, 'self.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const boundary = data.nodes.find(node => node.kind === 'boundary' && node.symbol === 'flowsignal/cli.py:write_report');
  assert.ok(boundary, 'Self-scan did not include the report-publication boundary');
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], requests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'self.html')).href);
      await page.locator('.graph-node').first().waitFor();
      assert.match(await page.locator('#graph-title').innerText(), /flowsignal.cli.main/);
      assert.equal(await page.locator('#findings').innerText(), String(data.summary.findings));
      assert.ok(await page.locator('.graph-node').count() <= data.max_nodes);
      await page.locator('#search').fill('flowsignal.cli.write_report');
      await page.locator('#results button').first().click();
      await page.locator('#callers').uncheck();
      const selected = page.locator(`[data-node="${boundary.id}"]`);
      await selected.focus();
      await page.keyboard.press('Enter');
      assert.match(await page.locator('#detail').innerText(), /FS005/);
      assert.match(await page.locator('#detail').innerText(), /When:/);
      assert.match(await page.locator('#detail-location').innerText(), /flowsignal\/cli.py/);
      await page.locator('#scope').evaluate(node => node.open = true);
      assert.match(await page.locator('#scope-content').innerText(), /context_manager_propagation/);
      await page.locator('#scope').evaluate(node => node.open = false);
      await page.locator('#fit').click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(overflow, false);
      await page.screenshot({ path: path.join(directory, `browser-${viewport.width}.png`), fullPage: true });
      const downloading = page.waitForEvent('download');
      await page.locator('#download').click();
      const download = await downloading;
      await download.saveAs(path.join(directory, `browser-${viewport.width}.svg`));
      assert.deepEqual(errors, []);
      assert.deepEqual(requests, []);
      results.push({ viewport, findings: data.summary.findings, selection: 'passed', diagnostics: 'visible', svgExport: 'passed', pageOverflow: false, browserErrors: errors, remoteRequests: requests });
      await page.close();
    }
    await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify(results, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
