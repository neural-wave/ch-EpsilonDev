const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result,upload,disabledPolicy,sidebar} = require('./fixtures');

test('loading and failure gate imports; retry accepts a disabled policy',async ({page}) => {
  let release;
  const waiting = new Promise((resolve) => {release = resolve;});
  const state = await boot(page,{configHandler:async (route) => {await waiting; await route.fulfill({status:503,json:{error:'unavailable'}});}});
  await expect(page.locator('#snapshot-import')).toBeDisabled();
  await expect(page.locator('#privacy-policy')).toHaveText('Loading privacy policy…');
  release();
  await expect(page.locator('#privacy-policy')).toHaveText('Privacy policy unavailable');
  state.configHandler = null;
  await page.locator('#retry-privacy-policy').click();
  await ready(page);
  await expect(page.locator('#snapshot-import')).toBeEnabled();
  await expect(page.locator('#retry-privacy-policy')).toBeHidden();
});

for (const failure of ['json','schema','network','timeout']) {
  test(`configuration ${failure} fails closed`,async ({page}) => {
    await boot(page,{configHandler:async (route) => {
      if (failure === 'json') return route.fulfill({body:'not json',contentType:'application/json'});
      if (failure === 'schema') return route.fulfill({json:{...disabledPolicy,demo_min_group_size:true}});
      if (failure === 'network') return route.abort('failed');
      return new Promise(() => {});
    }});
    await expect(page.locator('#privacy-policy')).toHaveText('Privacy policy unavailable',{timeout:12000});
    await expect(page.locator('#snapshot-import')).toBeDisabled();
  });
}

test('mock configuration makes no request',async ({page}) => {
  const state = await boot(page,{mock:true}); await ready(page);
  expect(state.configCalls).toBe(0);
});

test('both import gates and renderer gate reject without creating cards',async ({page}) => {
  const state = await boot(page); await ready(page);
  state.configHandler = (route) => route.fulfill({status:503,json:{error:'unavailable'}});
  await page.evaluate(() => SQ.policy.load());
  const preRead = await page.evaluate(() => {
    let reads = 0;
    const input = document.getElementById('snapshot-import');
    Object.defineProperty(input,'files',{configurable:true,value:[{text() {reads++; return Promise.resolve('{}');}}]});
    input.dispatchEvent(new Event('change'));
    return reads;
  });
  expect(preRead).toBe(0); await expect(page.locator('.entry')).toHaveCount(0);
  await expect(page.locator('.toast').last()).toHaveText('Privacy policy unavailable');
  const gate = await page.evaluate(() => {
    try { SQ.result.mount(document.getElementById('out'),'q',{imported:true},{policy:SQ.policy}); return false; }
    catch (e) { return e.message === 'Privacy policy unavailable'; }
  });
  expect(gate).toBe(true); await expect(page.locator('.released-result')).toHaveCount(0);
  state.configHandler = null; await page.evaluate(() => SQ.policy.load());
  await page.evaluate(() => {
    window.fileReadStarted = false;
    const input = document.getElementById('snapshot-import');
    Object.defineProperty(input,'files',{configurable:true,value:[{
      size:1,text() {window.fileReadStarted = true; return new Promise((resolve) => {window.finishFile = resolve;});}
    }]});
    input.dispatchEvent(new Event('change'));
  });
  expect(await page.evaluate(() => window.fileReadStarted)).toBe(true);
  state.configHandler = (route) => route.fulfill({status:503,json:{error:'unavailable'}});
  await page.evaluate(() => SQ.policy.load());
  const saved = require('./fixtures').snapshot();
  await page.evaluate((saved) => window.finishFile(JSON.stringify(saved)),saved);
  await expect(page.locator('.entry')).toHaveCount(0);
  expect(state.askCalls).toBe(0);
});

test('existing results hide during reload, fail closed, and restore only if compatible',async ({page}) => {
  const state = await boot(page); await ready(page); await ask(page);
  const sensitive = page.locator('.result-sensitive'); await expect(sensitive).toBeVisible();
  let release;
  const waiting = new Promise((resolve) => {release = resolve;});
  state.configHandler = async (route) => {await waiting; return route.fulfill({status:503,json:{error:'unavailable'}});};
  await page.evaluate(() => {window.reloading = SQ.policy.load();});
  await expect(sensitive).toBeHidden(); await expect(sensitive).toHaveAttribute('inert','');
  for (const button of await sensitive.locator('button').all()) await expect(button).toBeDisabled();
  release(); await page.evaluate(() => window.reloading);
  await expect(page.locator('.result-policy-blocked')).toHaveText('Privacy policy unavailable');
  state.configHandler = null; await page.locator('#retry-privacy-policy').click(); await ready(page);
  await expect(sensitive).toBeVisible();
  state.config = {...disabledPolicy,demo_min_group_size:5};
  await page.evaluate(() => SQ.policy.load());
  await expect(sensitive).toBeHidden();
  await expect(page.locator('.result-policy-blocked')).toContainText('Re-ask');
});

test('history requires exact active policy and never executes automatically',async ({page}) => {
  const active = {...disabledPolicy,demo_min_group_size:5};
  const d = result(); d.suppression = {enabled:true,threshold:5,policy_version:1,status:'released',reason:null};
  const state = await boot(page,{config:active,reply:d}); await ready(page); await ask(page);
  await page.locator('#home-logo').click(); await sidebar(page);
  await page.locator('.hist-btn').click();
  await expect(page.locator('.result-sensitive')).toBeVisible(); expect(state.askCalls).toBe(1);
  state.config = {...active,demo_min_group_size:6};
  await page.evaluate(() => SQ.policy.load()); await page.locator('#home-logo').click(); await sidebar(page);
  await page.locator('.hist-btn').click();
  await expect(page.locator('#out')).toContainText('Re-ask before displaying cached numbers');
  await expect(page.locator('#out table')).toHaveCount(0); expect(state.askCalls).toBe(1);
});

test('removal disposes policy subscriptions and imports stay non-exportable after retry',async ({page}) => {
  const state = await boot(page); await ready(page);
  await page.evaluate(() => {
    const subscribe = SQ.policy.subscribe;
    window.activeResultSubscriptions = 0;
    SQ.policy.subscribe = (callback) => {
      window.activeResultSubscriptions++;
      const stop = subscribe(callback);
      return () => {window.activeResultSubscriptions--; stop();};
    };
  });
  await page.locator('#snapshot-import').setInputFiles(upload());
  expect(await page.evaluate(() => window.activeResultSubscriptions)).toBe(1);
  await expect(page.locator('.result-sensitive')).toBeVisible();
  await expect(page.locator('.result-tools')).toHaveCount(0);
  state.configHandler = (route) => route.fulfill({status:503,json:{error:'unavailable'}});
  await page.evaluate(() => SQ.policy.load()); await expect(page.locator('.result-sensitive')).toBeHidden();
  state.configHandler = null; await page.locator('#retry-privacy-policy').click(); await ready(page);
  await expect(page.locator('.result-sensitive')).toBeVisible(); await expect(page.locator('.result-tools')).toHaveCount(0);
  await page.locator('#home-logo').click(); await expect(page.locator('.released-result')).toHaveCount(0);
  expect(await page.evaluate(() => window.activeResultSubscriptions)).toBe(0);
  await page.evaluate((reply) => {
    const instance = SQ.result.mount(document.getElementById('out'),'q',reply,{policy:SQ.policy});
    instance.element.remove();
  },result());
  await expect.poll(() => page.evaluate(() => window.activeResultSubscriptions)).toBe(0);
  expect(state.askCalls).toBe(0);
});

test('mock result prose is also hidden when policy becomes unavailable',async ({page}) => {
  const state = await boot(page,{mock:true}); await ready(page);
  await ask(page,'How many patients have a billed diagnosis of type 2 diabetes?');
  // Unknown mock wording may fail retrieval; use the exact authored fixture when needed.
  if (!await page.locator('.result-sensitive').count()) {
    const question = await page.evaluate(() => SQ.fixtures.FIXTURES.find((f) => f.id === 'c-diabetes').text);
    await ask(page,question);
  }
  await expect(page.locator('.result-sensitive').last()).toBeVisible();
  state.configHandler = (route) => route.fulfill({status:503,json:{error:'unavailable'}});
  await page.evaluate(() => SQ.policy.load());
  for (const sensitive of await page.locator('.result-sensitive').all()) await expect(sensitive).toBeHidden();
  expect(state.askCalls).toBe(0);
});
