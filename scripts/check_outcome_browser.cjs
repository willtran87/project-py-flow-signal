const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(process.argv[2] || '.artifacts/outcome-review');
  const html = await fs.readFile(path.join(directory, 'report.html'), 'utf8');
  const data = JSON.parse(html.split('<script id="flow-data" type="application/json">')[1].split('</script>')[0]);
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{width:2560,height:1600},{width:1440,height:1000},{width:390,height:844}]) {
      const page = await browser.newPage({viewport});
      const errors=[],remote=[];
      page.on('pageerror', e=>errors.push(e.message));
      page.on('request', r=>{if(/^https?:/.test(r.url())) remote.push(r.url());});
      await page.goto(pathToFileURL(path.join(directory,'report.html')).href);
      await page.locator('#workflow-review summary').click();
      async function focus(name) {
        await page.locator('#search').fill(name);
        await page.locator('#results button').first().click();
        const node=data.nodes.find(n=>n.qualified_name===name);
        await page.locator('[data-node="'+node.id+'"]').click();
      }
      await focus('app.invoke');
      assert.match(await page.locator('#detail').innerText(),/Possible callback contexts/);
      await focus('worker.unobserved');
      assert.match(await page.locator('#detail').innerText(),/retained_unobserved/);
      await page.locator('.uncertainty-details summary').first().click();
      assert.match(await page.locator('.uncertainty-details').first().innerText(),/Next:/);
      await page.locator('.uncertainty-details button').first().focus();
      await page.keyboard.press('Enter');
      await page.screenshot({path:path.join(directory,'tasks-'+viewport.width+'.png'),fullPage:true});
      await focus('service.degraded');
      const handler=data.nodes.find(n=>n.kind==='handler'&&n.symbol==='service.py:degraded');
      await page.locator('[data-node="'+handler.id+'"]').click();
      assert.match(await page.locator('#detail').innerText(),/degraded recovery/);
      assert.match(await page.locator('#detail').innerText(),/WARNING/);
      const boxes=await page.locator('.graph-node').evaluateAll(nodes=>nodes.map(n=>{const b=n.getBoundingClientRect();return{x:b.x,y:b.y,r:b.right,b:b.bottom}}));
      for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){const a=boxes[i],b=boxes[j];assert.ok(a.r<=b.x||b.r<=a.x||a.b<=b.y||b.b<=a.y,'nodes overlap')}
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.join(directory,'outcomes-'+viewport.width+'.png'),fullPage:true});
      await page.goto(pathToFileURL(path.join(directory,'partial.html')).href);
      assert.match(await page.locator('#scan-status').innerText(),/Incomplete scan/);
      await page.locator('[data-node="app.py:cycle"]').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.locator('[data-node^="call:"]').first().click();
      await page.locator('.uncertainty-details summary').first().click();
      assert.match(await page.locator('.uncertainty-details').first().innerText(),/analysis budget/);
      await page.locator('.uncertainty-details').first().scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.join(directory,'partial-'+viewport.width+'.png'),fullPage:true});
      await page.locator('.uncertainty-details button').nth(1).focus();
      await page.locator('#detail').screenshot({path:path.join(directory,'partial-detail-'+viewport.width+'.png')});
      assert.deepEqual(errors,[]);assert.deepEqual(remote,[]);
      results.push({viewport,callbackEvidence:true,taskOwnership:true,outcomeAdvice:true,uncertaintyNavigation:true,partialCyclicReport:true,overflow:false,errors,remote});
      await page.close();
    }
  } finally {await browser.close();}
  await fs.writeFile(path.join(directory,'browser-validation.json'),JSON.stringify(results,null,2)+'\n');
  console.log(JSON.stringify(results,null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
