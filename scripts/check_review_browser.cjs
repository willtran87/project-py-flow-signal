// Optional browser verification after validate_review_workflow.py.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/review-workflow');
  const html = await fs.readFile(path.join(directory, 'report.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const handler = data.nodes.find(n => n.kind === 'handler' && n.existing.some(s => s.kind === 'diagnostic'));
  const dismissed = Object.values(data.findings).find(f => f.review_status === 'dismissed');
  const findingNode = data.nodes.find(n => n.findings.includes(dismissed.id));
  assert.ok(data.edges.some(e => e.execution === 'implicit_property' && e.label.startsWith('property getter')));
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], remote = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('request', r => { if (/^https?:/.test(r.url())) remote.push(r.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'report.html')).href);
      assert.match(await page.locator('#baseline-summary').innerText(), /1 new.*1 unchanged.*1 resolved.*0 unverified.*1 dismissed/);
      assert.equal(await page.locator('#signals').innerText(), '1');
      const node = page.locator(`[data-node="${handler.id}"]`);
      await node.focus(); await page.keyboard.press('Enter');
      assert.match(await page.locator('#detail').innerText(), /Workflow diagnostic report/);
      assert.match(await page.locator('#detail').innerText(), /configured resolved API contract/);
      await page.screenshot({ path: path.join(directory, `reporter-${viewport.width}.png`), fullPage: true });
      await page.locator('#search').fill('app.legacy_health');
      await page.locator('#results button').first().click();
      await page.locator(`[data-node="${findingNode.id}"]`).focus();
      await page.keyboard.press('Enter');
      assert.match(await page.locator('#detail').innerText(), /unchanged.*dismissed/s);
      assert.match(await page.locator('#detail').innerText(), /Health probe failures are already handled by the deployment monitor/);
      assert.match(await page.locator('#detail').innerText(), /When:/);
      await page.locator('#baseline-review details').evaluate(n => n.open = true);
      assert.match(await page.locator('#baseline-changes').innerText(), /resolved: FS005.*retired_probe/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const boxes = await page.locator('.graph-node').evaluateAll(nodes => nodes.map(n => {
        const b = n.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom };
      }));
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        assert.ok(a.r <= b.x || b.r <= a.x || a.b <= b.y || b.b <= a.y, 'Graph nodes overlap');
      }
      await page.screenshot({ path: path.join(directory, `review-${viewport.width}.png`), fullPage: true });
      assert.deepEqual(errors, []); assert.deepEqual(remote, []);
      results.push({ viewport, reporters: 'passed', propertyEdge: 'passed', baseline: 'passed', dismissal: 'passed', keyboard: 'passed', overflow: false, errors, remote });
      await page.close();
    }
  } finally { await browser.close(); }
  await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
