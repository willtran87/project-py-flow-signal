// Run validate_product_features.py first. No network is needed by the report.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/product-features');
  const html = await fs.readFile(path.join(directory, 'report.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], remote = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('request', r => { if (/^https?:/.test(r.url())) remote.push(r.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'report.html')).href);
      for (const [status, query, role] of [['recognized', 'demo.parse_reported', 'REPORTING OWNER'], ['not_established', 'demo.parse_unreported', 'REPORTING BARRIER']]) {
        const boundary = data.nodes.find(n => n.coverage?.status === status);
        await page.locator('#search').fill(query);
        await page.locator('#results button').first().click();
        await page.locator(`[data-node="${boundary.id}"]`).focus();
        await page.keyboard.press('Enter');
        await page.locator('#show-reporting-path').focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#clear-path').isVisible(), true);
        const ids = await page.locator('.graph-node').evaluateAll(ns => ns.map(n => n.getAttribute('data-node')).sort());
        assert.deepEqual(ids, [...boundary.coverage_path.nodes].sort());
        const edges = await page.locator('[data-edge]').evaluateAll(es => es.map(e => e.getAttribute('data-edge')).sort());
        assert.deepEqual(edges, [...boundary.coverage_path.edges].sort());
        assert.match(await page.locator('#graph').textContent(), new RegExp(role));
        assert.match(await page.locator('#path-status').innerText(), /not an exhaustive execution graph/);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        const boxes = await page.locator('.graph-node').evaluateAll(ns => ns.map(n => {
          const b = n.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom };
        }));
        for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i], b = boxes[j];
          assert.ok(a.r <= b.x || b.r <= a.x || a.b <= b.y || b.b <= a.y, 'Graph nodes overlap');
        }
        await page.screenshot({ path: path.join(directory, `${status}-${viewport.width}.png`), fullPage: true });
        await page.locator('#clear-path').click();
      }
      assert.match(await page.locator('#runtime-review').innerText(), /observed only: 1/);
      await page.locator('#search').fill('demo.exercise');
      await page.locator('#results button').first().click();
      assert.equal(await page.locator('[data-edge^="runtime:"]').count(), 0);
      await page.locator('#runtime-overlay').check();
      assert.equal(await page.locator('[data-edge^="runtime:"]').count(), 6);
      await page.locator('#runtime-overlay').uncheck();
      assert.equal(await page.locator('[data-edge^="runtime:"]').count(), 0);
      assert.deepEqual(errors, []); assert.deepEqual(remote, []);
      results.push({ viewport, reportingOwner: 'passed', barrier: 'passed', runtimeOverlay: 'passed', exactEvidenceEdges: 'passed', keyboard: 'passed', overflow: false });
      await page.close();
    }
  } finally { await browser.close(); }
  await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
