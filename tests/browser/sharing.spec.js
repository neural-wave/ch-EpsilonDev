const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result,snapshot,disabledPolicy} = require('./fixtures');

test('sharing is opt-in and another browser opens the same answer without a query',async ({page,browser,baseURL}) => {
  const state = await boot(page,{reply:{...result(),share_token:'d'.repeat(43)}}); await ready(page); await ask(page);
  let publications = 0;
  const shareId = 's'.repeat(43);
  await page.route('**/demo/shares',async (route) => {
    publications++;
    expect(route.request().postDataJSON()).toEqual({share_token:'d'.repeat(43)});
    await route.fulfill({json:{share_id:shareId,expires_at:'2026-10-24T12:00:00Z'}});
  });
  expect(publications).toBe(0);
  page.once('dialog',(dialog) => dialog.dismiss());
  await page.getByRole('button',{name:'Create shareable link',exact:true}).click();
  expect(publications).toBe(0);
  page.once('dialog',async (dialog) => {
    expect(dialog.message()).toContain('Anyone with the link');
    expect(dialog.message()).toContain('How many patients');
    await dialog.accept();
  });
  await page.getByRole('button',{name:'Create shareable link',exact:true}).click();
  const input = page.locator('.share-link input'); await expect(input).toBeVisible();
  const url = await input.inputValue();
  expect(new URL(url).search).toBe('?share=' + shareId); expect(url).not.toContain('patients');
  expect(publications).toBe(1);
  const context = await browser.newContext({baseURL});
  try {
    const colleague = await context.newPage(); let queries = 0;
    await colleague.route('**/demo/config',(route) => route.fulfill({json:disabledPolicy}));
    await colleague.route('**/demo/ask',async (route) => {queries++; await route.fulfill({json:result()});});
    await colleague.route('**/demo/shares/' + shareId,(route) => route.fulfill({json:{snapshot:snapshot(),expires_at:'2026-10-24T12:00:00Z'}}));
    await colleague.goto(url); await ready(colleague);
    await expect(colleague.locator('.result-number')).toHaveText('12');
    await expect(colleague.locator('tbody td')).toHaveText('12');
    await expect(colleague.locator('#out')).toContainText('Shared snapshot — not a fresh database run');
    expect(queries).toBe(0);
    await expect(colleague.getByRole('button',{name:'Create shareable link',exact:true})).toHaveCount(0);
    await colleague.getByRole('button',{name:'Ask again for current results'}).click();
    await expect.poll(() => queries).toBe(1);
  } finally {await context.close();}
  expect(state.askCalls).toBe(1);
});

test('expired or policy-incompatible links show no rows and do not auto-execute',async ({page}) => {
  const state = await boot(page); await ready(page);
  const id = 's'.repeat(43);
  for (const status of [404,409]) {
    await page.route('**/demo/shares/' + id,(route) => route.fulfill({status,json:{error:status === 404 ? 'This shared result is unavailable or expired.' : 'Privacy policy changed. Re-ask for a current result.'}}));
    await page.goto('/console/cli/?intro=0&share=' + id); await ready(page);
    await expect(page.locator('#out')).toContainText(status === 404 ? 'expired' : 'Privacy policy changed');
    await expect(page.locator('table')).toHaveCount(0);
    expect(state.askCalls).toBe(0);
    await page.unroute('**/demo/shares/' + id);
  }
});

test('share failures preserve the released answer',async ({page}) => {
  await boot(page,{reply:{...result(),share_token:'d'.repeat(43)}}); await ready(page); await ask(page);
  await page.route('**/demo/shares',(route) => route.fulfill({status:503,json:{error:'Sharing is temporarily unavailable.'}}));
  page.once('dialog',(dialog) => dialog.accept());
  await page.getByRole('button',{name:'Create shareable link',exact:true}).click();
  await expect(page.locator('.toast').last()).toHaveText('Sharing is temporarily unavailable.');
  await expect(page.locator('.result-number')).toHaveText('12');
});
