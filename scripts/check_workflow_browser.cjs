const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/workflow-review');
  const html = await fs.readFile(path.join(directory, 'report.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], remote = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => { if (/^https?:/.test(request.url())) remote.push(request.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'report.html')).href);
      for (const state of ['new', 'expired', 'uncovered', 'unresolved']) {
        await page.locator('#queue-state').selectOption(state);
        const expected = data.review_queue.items.filter(r => r.states.includes(state)).length;
        assert.match(await page.locator('#queue-count').innerText(), new RegExp('^' + expected + ' matching'));
        assert.equal(await page.locator('#queue-items article').count(), Math.min(25, expected));
      }
      await page.locator('#queue-state').selectOption('');
      await page.locator('#queue-entry').selectOption('demo.py:exercise');
      assert.equal(await page.locator('#queue-items article').count(), data.review_queue.items.filter(r => r.entrypoints.includes('demo.py:exercise')).length);
      await page.locator('#queue-entry').selectOption('');
      await page.locator('#queue-group').selectOption('owner');
      assert.match(await page.locator('#queue-items').innerText(), /demo.py:branching/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(directory, `queue-${viewport.width}.png`), fullPage: true });
      await page.locator('#queue-state').selectOption('uncovered');
      await page.locator('#queue-items button').first().focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#clear-path').isVisible(), true);
      assert.match(await page.locator('.coverage-decision').innerText(), /Coverage not established/);
      await page.locator('#workflow-review summary').click();
      await page.locator('#search').fill('demo.branching');
      await page.locator('#results button').first().click();
      const handler = data.nodes.find(n => n.kind === 'handler' && n.symbol === 'demo.py:branching');
      await page.locator(`[data-node="${handler.id}"]`).focus();
      await page.keyboard.press('Enter');
      assert.match(await page.locator('#detail').innerText(), /Modeled handler exits/);
      assert.match(await page.locator('#detail').innerText(), /if true/);
      assert.match(await page.locator('#detail').innerText(), /if false/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const boxes = await page.locator('.graph-node').evaluateAll(nodes => nodes.map(n => { const b = n.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom }; }));
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j]; assert.ok(a.r <= b.x || b.r <= a.x || a.b <= b.y || b.b <= a.y, 'Graph nodes overlap');
      }
      await page.screenshot({ path: path.join(directory, `paths-${viewport.width}.png`), fullPage: true });
      assert.match(await page.locator('#runtime-review').innerText(), /Run: second/);
      assert.match(await page.locator('#runtime-review').innerText(), /Compared with first/);
      assert.deepEqual(errors, []); assert.deepEqual(remote, []);
      results.push({ viewport, filters: 'passed', entrypoint: 'passed', ownerGrouping: 'passed', pathNavigation: 'passed', branchEvidence: 'passed', keyboard: 'passed', overflow: false, errors, remote });
      await page.close();
    }
  } finally { await browser.close(); }
  await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
