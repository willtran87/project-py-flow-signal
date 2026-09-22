// Run validate_typed_flow.py first. Browser checks are optional development tools.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/typed-flow');
  const html = await fs.readFile(path.join(directory, 'report.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const covered = data.nodes.find(n => n.coverage?.status === 'recognized');
  const rejected = data.nodes.find(n => n.coverage?.status === 'not_established');
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 2560, height: 1600 }, { width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], remote = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('request', r => { if (/^https?:/.test(r.url())) remote.push(r.url()); });
      await page.goto(pathToFileURL(path.join(directory, 'report.html')).href);
      async function inspect(node, query) {
        await page.locator('#search').fill(query);
        await page.locator('#results button').first().click();
        await page.locator(`[data-node="${node.id}"]`).focus();
        await page.keyboard.press('Enter');
      }
      await inspect(covered, 'typed_workflow.Client.fetch');
      assert.match(await page.locator('.coverage-decision').innerText(), /Recognized within the static model/);
      assert.match(await page.locator('.coverage-decision').innerText(), /All resolved caller routes/);
      assert.ok(await page.locator('.coverage-evidence.credited').count());
      await page.screenshot({ path: path.join(directory, `coverage-${viewport.width}.png`), fullPage: true });
      const owner = page.locator('.coverage-evidence').filter({ hasText: 'ERROR log is an unconditional' }).getByRole('button');
      await owner.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#detail-title').innerText(), 'run');
      assert.equal(await page.locator('.graph-node:focus').getAttribute('data-node'), 'typed_workflow.py:run');
      await inspect(rejected, 'typed_workflow.refresh');
      assert.match(await page.locator('.coverage-decision').innerText(), /Coverage not established/);
      assert.match(await page.locator('.coverage-decision').innerText(), /conditional or below WARNING/);
      assert.equal(await page.locator('.coverage-evidence.credited').count(), 0);
      assert.match(await page.locator('#detail').innerText(), /When:/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const boxes = await page.locator('.graph-node').evaluateAll(nodes => nodes.map(n => {
        const b = n.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom };
      }));
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        assert.ok(a.r <= b.x || b.r <= a.x || a.b <= b.y || b.b <= a.y, 'Graph nodes overlap');
      }
      await page.screenshot({ path: path.join(directory, `rejected-${viewport.width}.png`), fullPage: true });
      assert.deepEqual(errors, []); assert.deepEqual(remote, []);
      results.push({ viewport, coverage: 'passed', sourceNavigation: 'passed', keyboard: 'passed', overflow: false, errors, remote });
      await page.close();
    }
  } finally { await browser.close(); }
  await fs.writeFile(path.join(directory, 'browser-validation.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
