const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result,sidebar,snapshot,upload,readDownload} = require('./fixtures');
const question = '  How many patients match café <b>wording</b>?  ';
const query = 'SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s a <urn:Clinical> } }';
function record(patch = {}) {
  return {source:'database',question,executed_at:'2026-10-17T17:00:00+00:00',dataset:{name:'Synthetic fixture dataset',version:'release-1',description:'Browser fixtures only'},sparql_query:query,...patch};
}
function answer(provenance = record()) {return {...result(),sparql_query:query,reproducibility:provenance};}
const copyButton = (page) => page.locator('.reproducibility-copy');
async function capture(page) {
  await page.evaluate(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async (text) => {window.recordCopied = text;}}}));
}
async function copied(page) {
  await copyButton(page).click();
  return page.evaluate(() => window.recordCopied);
}

test('only an accessible corner icon is visible and keyboard copying uses the exact query-free format',async ({page}) => {
  const state = await boot(page,{reply:answer()}); await ready(page); await ask(page,question); await capture(page);
  expect(state.questions).toEqual([question]);
  const button = copyButton(page); await expect(button).toBeVisible();
  await expect(button).toHaveAttribute('aria-label','Copy reproducibility record');
  await expect(button.locator('svg')).toHaveCount(1);
  expect(await button.textContent()).toBe('');
  await expect(page.locator('.reproducibility-card, .reproducibility-question, .reproducibility-query')).toHaveCount(0);
  expect(await page.locator('.result-sensitive').textContent()).not.toContain('release-1');
  const corner = await button.evaluate((node) => {const r=node.getBoundingClientRect(),p=node.parentElement.getBoundingClientRect();return Math.abs(r.right-p.right)<2 && Math.abs(r.top-p.top)<2;});
  expect(corner).toBe(true);
  await button.focus(); await button.press('Enter');
  expect(await page.evaluate(() => window.recordCopied)).toBe(`Database execution\n\n- Source: Database execution\n- Question: ${question}\n- Result: 12\n- Executed at: 2026-10-17T17:00:00+00:00 (UTC)\n- Dataset: Synthetic fixture dataset\n- Dataset version: release-1\n- Dataset description: Browser fixtures only`);
  expect(await page.evaluate(() => window.recordCopied)).not.toContain(query);
  await expect(page.locator('.reproducibility-copy img, .reproducibility-copy b')).toHaveCount(0);
});

test('multiline question is copied verbatim without injecting visible markup',async ({page}) => {
  await boot(page,{reply:answer(record({question:'\n <img src=x onerror="window.injected=true"> café\n'}))}); await ready(page); await ask(page); await capture(page);
  expect(await copied(page)).toContain('- Question: \n <img src=x onerror="window.injected=true"> café\n');
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  await expect(page.locator('.result-sensitive img')).toHaveCount(0);
});

test('policy transitions hide and disable the icon until a compatible policy returns',async ({page}) => {
  const state = await boot(page,{reply:answer()}); await ready(page); await ask(page);
  const button = copyButton(page); let release;
  const waiting = new Promise((resolve) => {release = resolve;});
  state.configHandler = async (route) => {await waiting; await route.fulfill({status:503,json:{error:'Unavailable'}});};
  await page.evaluate(() => {window.refresh = SQ.policy.load();});
  await expect(button).toBeHidden(); await expect(button).toBeDisabled();
  release(); await page.evaluate(() => window.refresh); await expect(button).toBeHidden();
  state.configHandler = null; await page.locator('#retry-privacy-policy').click(); await ready(page);
  await expect(button).toBeVisible(); await expect(button).toBeEnabled();
  state.config = {...state.config,demo_min_group_size:5}; await page.evaluate(() => SQ.policy.load());
  await expect(button).toBeHidden(); await expect(button).toBeDisabled();
});

test('history copies original provenance while legacy entries cannot invent a record',async ({page}) => {
  const state = await boot(page,{reply:answer()}); await ready(page); await ask(page,question);
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('qg-console-history'))[0]);
  expect(saved.res.sparql_query).toBeUndefined(); expect(saved.res.reproducibility.sparql_query).toBeUndefined();
  state.reply = answer(record({executed_at:'2026-10-18T08:00:00+00:00',dataset:{name:'New fixture',version:'release-2',description:'Changed'}}));
  await page.reload(); await ready(page); await sidebar(page); await page.locator('.hist-btn').click(); await capture(page);
  const text = await copied(page);
  expect(text).toContain('release-1'); expect(text).not.toContain('release-2'); expect(text).toContain('2026-10-17T17:00:00+00:00'); expect(state.askCalls).toBe(1);
  await page.evaluate(() => {const items=JSON.parse(localStorage.getItem('qg-console-history'));delete items[0].res.reproducibility;localStorage.setItem('qg-console-history',JSON.stringify(items));});
  await page.reload(); await ready(page); await sidebar(page); await page.locator('.hist-btn').click();
  await expect(copyButton(page)).toHaveCount(0); await expect(page.locator('.reproducibility-unavailable')).toBeVisible(); expect(state.askCalls).toBe(1);
});

test('missing dataset is explained only in copied text and snapshots/shares exclude provenance',async ({page},testInfo) => {
  const state = await boot(page,{reply:answer(record({dataset:{name:null,version:null,description:null}}))}); await ready(page); await ask(page); await capture(page);
  expect(await copied(page)).toContain('Dataset version unavailable; reproducibility record is incomplete.');
  await expect(page.locator('.reproducibility-warning, .reproducibility-card')).toHaveCount(0);
  page.once('dialog',(dialog) => dialog.accept()); const downloading = page.waitForEvent('download');
  await page.getByRole('button',{name:'Download snapshot',exact:true}).click();
  const file=await downloading; await file.saveAs(testInfo.outputPath('synthetic-no-provenance.json'));
  const exported=JSON.parse(await readDownload(file)); expect(exported.reproducibility).toBeUndefined(); expect(exported.sparql_query).toBeUndefined();
  await page.locator('#home-logo').click(); await page.locator('#snapshot-import').setInputFiles(upload(exported));
  await expect(copyButton(page)).toHaveCount(0); await expect(page.locator('.reproducibility-unavailable')).toBeVisible();
  const id='s'.repeat(43); await page.route('**/demo/shares/'+id,(route) => route.fulfill({json:{snapshot:snapshot(),expires_at:'2026-10-24T12:00:00Z'}}));
  await page.goto('/console/cli/?intro=0&share='+id); await ready(page);
  await expect(copyButton(page)).toHaveCount(0); expect(state.askCalls).toBe(1);
});

test('preview icon copies the requested format with actual illustrative result and no invented timestamp',async ({page}) => {
  await boot(page,{mock:true}); await ready(page);
  const q=await page.evaluate(() => SQ.fixtures.FIXTURES.find((f) => f.id === 'c-diabetes').text);
  await ask(page,q); await capture(page);
  expect(await copied(page)).toBe(`Preview mode (no database)\n\n- Source: Synthetic preview, not for citation\n- Question: ${q}\n- Result: 412 (illustrative number)\n- Executed at: Not applicable\n- Dataset: None, demonstration only`);
  await expect(page.locator('.reproducibility-card')).toHaveCount(0);
  await expect(copyButton(page)).toHaveAttribute('title',/not for citation/);
});

for (const theme of ['light','dark']) {
  test(`corner icon stays in ${theme} viewport with a long private record`,async ({page},testInfo) => {
    await boot(page,{reply:answer(record({question:'x'.repeat(4000)}))}); await ready(page); await ask(page);
    if (theme === 'dark') await page.locator('#theme-btn').click();
    await expect(copyButton(page)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await copyButton(page).scrollIntoViewIfNeeded(); await page.screenshot({path:testInfo.outputPath(`reproducibility-${theme}.png`)});
  });
}

test('user commands cannot retrieve developer reports, context or query diagnostics',async ({page}) => {
  let developerCalls = 0;
  await boot(page,{reply:answer()}); await ready(page);
  page.on('request',(request) => { if (new URL(request.url()).pathname.startsWith('/agents')) developerCalls++; });
  await ask(page);
  for (const command of ['explain','status','json','bench']) await ask(page,command);
  await expect(page.getByText('Developer diagnostics are not available in the user console.',{exact:true})).toHaveCount(4);
  expect(developerCalls).toBe(0);
  await expect(page.locator('.reproducibility-query, [data-copy-query]')).toHaveCount(0);
});

test('mock explanation uses the available width instead of a narrow grid column',async ({page}) => {
  await boot(page,{mock:true}); await ready(page);
  const q = await page.evaluate(() => SQ.fixtures.FIXTURES.find((f) => f.id === 'c-diabetes').text);
  await ask(page,q);
  const explanation = page.locator('.result-sensitive p.counted');
  await expect(explanation).toBeVisible();
  expect(await explanation.evaluate((node) => getComputedStyle(node).display)).toBe('block');
  expect(await explanation.evaluate((node) => node.getBoundingClientRect().width > node.parentElement.getBoundingClientRect().width * .8)).toBe(true);
});
