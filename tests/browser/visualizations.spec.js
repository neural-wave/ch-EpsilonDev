const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result} = require('./fixtures');

const cases = [
  {name:'category bars',label:'Preview example: patients by sex (synthetic numbers)',
    rows:[['Female','48'],['Male','36'],['Other / unknown','12']],values:[48,36,12],series:1},
  {name:'wide grouped bars',label:'Preview example: two cohorts by age group (synthetic numbers)',
    rows:[['18–39','24','36'],['40–64','41','52'],['65+','18','29']],values:[24,36,41,52,18,29],series:2},
  {name:'long-form grouped bars with a missing pair',label:'Preview example: two cohorts by sex (long-form synthetic numbers)',
    rows:[['Female','Group A','32'],['Female','Group B','48'],['Male','Group A','24'],['Male','Group B','36'],['Other / unknown','Group A','12']],values:[32,48,24,36,12,null],series:2},
];

for (const theme of ['light','dark']) for (const example of cases) {
  test(`${example.name}: synthetic chart matches exact table in ${theme} theme`,async ({page},testInfo) => {
    const state = await boot(page,{mock:true}); await ready(page);
    let executions = 0;
    page.on('request',(request) => {if (['/demo/ask','/nl-to-sparql'].includes(new URL(request.url()).pathname)) executions++;});
    if (theme === 'dark') await page.locator('#theme-btn').click();
    await page.getByRole('button',{name:example.label,exact:true}).click();
    await expect(page.getByText('Synthetic preview — no database execution',{exact:true})).toBeVisible();
    const chart = page.getByRole('group',{name:'Result chart',exact:true}); await expect(chart).toBeVisible();
    await expect(chart.locator('.chart-group')).toHaveCount(3);
    await expect(chart.locator('.chart-row')).toHaveCount(example.values.length);
    const max = Math.max(...example.values.filter((v) => v !== null));
    for (let i=0;i<example.values.length;i++) {
      const value=example.values[i], row=chart.locator('.chart-row').nth(i);
      await expect(row.locator('span').last()).toHaveText(value === null ? 'No value' : String(value));
      const width = await row.locator('.chart-bar').evaluate((node) => parseFloat(node.style.width));
      expect(width).toBeCloseTo(value === null ? 0 : value/max*100,3);
    }
    const table = page.locator('.result-table-wrap');
    await expect(table.locator('tbody tr')).toHaveCount(example.rows.length);
    expect(await table.locator('tbody td').allTextContents()).toEqual(example.rows.flat());
    expect(await page.locator('.result-sensitive').evaluate((node) => {
      const chart=node.querySelector('.result-chart'),table=node.querySelector('.result-table-wrap');
      return Boolean(chart.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    await expect(page.locator('.result-tools, .reproducibility-copy')).toHaveCount(0);
    expect(executions).toBe(0); expect(state.askCalls).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1)).toBe(true);
    await chart.scrollIntoViewIfNeeded();
    await page.screenshot({path:testInfo.outputPath('visualization.png'),fullPage:true});
  });
}

test('export actions are accessible icon-only controls and keyboard copy still works',async ({page}) => {
  await boot(page,{reply:{...result(),reproducibility:{source:'synthetic',question:'Synthetic layout test',executed_at:null,dataset:{name:null,version:null,description:null}}}}); await ready(page); await ask(page);
  for (const name of ['Download CSV','Copy table','Download snapshot']) {
    const button=page.getByRole('button',{name,exact:true});
    await expect(button).toBeVisible(); await expect(button).toHaveAttribute('title',name);
    await expect(button.locator('svg[aria-hidden="true"]')).toHaveCount(1);
    expect(await button.textContent()).toBe('');
    const size=await button.boundingBox(); expect(size.width).toBeCloseTo(44,2); expect(size.height).toBeCloseTo(44,2);
  }
  const names=['Download CSV','Copy table','Download snapshot','Copy reproducibility record'];
  const boxes=[];
  for (const name of names) boxes.push(await page.getByRole('button',{name,exact:true}).boundingBox());
  const sensitive=await page.locator('.result-sensitive').boundingBox();
  for (let i=0;i<boxes.length;i++) {
    expect(Math.abs(boxes[i].y-sensitive.y)).toBeLessThan(2);
    if (i) expect(boxes[i].x).toBeGreaterThanOrEqual(boxes[i-1].x+boxes[i-1].width);
    expect(boxes[i].x).toBeGreaterThanOrEqual(sensitive.x);
    expect(boxes[i].x+boxes[i].width).toBeLessThanOrEqual(sensitive.x+sensitive.width+1);
  }
  const table=await page.locator('.result-table-wrap').boundingBox();
  expect(table.y).toBeGreaterThan(boxes[0].y+boxes[0].height);
  await page.evaluate(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async (text) => {window.iconCopied=text;}}}));
  const copy=page.getByRole('button',{name:'Copy table',exact:true}); await copy.focus(); await copy.press('Enter');
  expect(await page.evaluate(() => window.iconCopied)).toBe('patients\r\n12');
});
