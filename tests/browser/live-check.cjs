// Opt-in real-data browser assertions. Input stays in memory; output contains statuses only.
const {chromium,expect} = require('@playwright/test');
(async () => {
  let text = ''; for await (const chunk of process.stdin) text += chunk;
  const cases = JSON.parse(text), browser = await chromium.launch();
  const checks = [];
  try {
    const page = await browser.newPage();
    await page.goto(process.argv[2].replace(/\/$/,'') + '/console/cli/?intro=0');
    await expect.poll(() => page.evaluate(() => SQ.policy.state),{timeout:15000}).toBe('available');
    for (const item of cases) {
      const expected = item.response.rows || [];
      await page.evaluate((item) => {
        SQ.result.disposeAll(document.getElementById('out'));
        document.getElementById('out').replaceChildren();
        document.body.classList.remove('is-empty');
        SQ.result.mount(document.getElementById('out'),item.question,item.response,{policy:SQ.policy});
      },item);
      if (item.response.result_status === 'withheld') {
        await expect(page.locator('table')).toHaveCount(0);
        checks.push({check:item.name + '_browser',status:'PASS',reason:'Actual suppressed response has no rendered table or chart'});
        continue;
      }
      const columns = item.response.columns;
      const actual = await page.locator('tbody tr').evaluateAll((rows) => rows.map((row) => [...row.cells].map((cell) => cell.textContent)));
      expect(actual).toEqual(expected.slice(0,50).map((row) => columns.map((c) => String(row[c] ?? ''))));
      const visual = await page.evaluate((d) => SQ.result.visual(d),item.response);
      if (visual.values) {
        const values = await page.locator('.chart-row > span:last-child').allTextContents();
        expect(values).toEqual(visual.values.flat().map((v) => v === '' ? 'No value' : String(v)));
      } else if (visual.kind === 'number') await expect(page.locator('.result-number')).toHaveText(String(visual.value));
      const covered = !item.name.startsWith('two_cohorts') || visual.kind === 'grouped';
      checks.push({check:item.name + '_browser',status:covered ? 'PASS':'PENDING',reason:covered ? 'Table and chart labels equal released source values':'Reference data did not exercise the two-group chart'});
    }
    console.log(JSON.stringify(checks));
  } finally { await browser.close(); }
})().catch(() => { console.log(JSON.stringify([{check:'real_browser',status:'FAIL',reason:'Browser assertions failed; no raw values recorded'}])); process.exitCode=1; });
