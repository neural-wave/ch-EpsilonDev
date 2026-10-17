const {test,expect}=require('@playwright/test');
const {boot,ready,ask,result}=require('./fixtures');

test('ordinary live questions render actual returned groups across topics and projection orders',async ({page}) => {
  const state=await boot(page); await ready(page);
  const cases=[
    {question:'How many patients have a billed diagnosis of diabetes compared with those without it, grouped by age group?',
      data:result(['patient_count','category','patient_count'],[{without:'37',band:'18–39',with:'19'},{without:'43',band:'40–64',with:'29'}],['without','band','with']),kind:'grouped',values:['37','19','43','29']},
    {question:'Compare cholera and no cholera patient counts by age group.',
      data:result(['patient_count','category','category'],[{n:'11',cohort:'with',band:'young'},{n:'17',cohort:'without',band:'young'},{n:'23',cohort:'with',band:'middle'},{n:'29',cohort:'without',band:'middle'},{n:'31',cohort:'with',band:'older'}],['n','cohort','band']),kind:'grouped',values:['11','17','23','29','31','No value']},
    {question:'How many patients have a billed diagnosis of asthma, grouped by sex?',
      data:result(['patient_count','category'],[{total:'53',sex:'Female'},{total:'61',sex:'Male'}],['total','sex']),kind:'bar',values:['53','61']},
  ];
  for (let i=0;i<cases.length;i++) {
    if (i) await page.locator('#home-logo').click();
    const item=cases[i]; state.reply=item.data; await ask(page,item.question);
    await expect(page.locator('.result-chart')).toBeVisible();
    const values=await page.locator('.chart-row').evaluateAll((nodes) => nodes.map((node) => node.lastElementChild.textContent));
    expect(values).toEqual(item.values);
    await expect(page.locator('tbody tr')).toHaveCount(item.data.rows.length);
    expect(await page.locator('tbody td').allTextContents()).toEqual(item.data.rows.flatMap((row) => item.data.columns.map((column) => row[column])));
    expect(state.questions[i]).toBe(item.question); expect(state.askCalls).toBe(i+1);
    await expect(page.getByText('Synthetic preview — no database execution',{exact:true})).toHaveCount(0);
  }
});

test('a single returned value cannot silently stand in for a requested comparison',async ({page}) => {
  const state=await boot(page); await ready(page);
  await ask(page,'How many patients have diabetes compared with those without it, grouped by age group?');
  await expect(page.locator('.result-comparison-notice')).toContainText('Only one value was returned');
  await expect(page.locator('.result-chart')).toHaveCount(0);
  await expect(page.locator('.result-number')).toHaveText('12');
  expect(state.askCalls).toBe(1);
});

test('mock mode refuses to replace an arbitrary comparison with its diabetes count fixture',async ({page}) => {
  const state=await boot(page,{mock:true}); await ready(page);
  await ask(page,'How many patients have a billed diagnosis of type 2 diabetes compared with those without it, grouped by age group?');
  await expect(page.locator('#out')).toContainText('Preview mode cannot execute arbitrary comparison or grouped questions');
  await expect(page.locator('.result-number, .result-chart, .result-tools, table')).toHaveCount(0);
  await expect(page.locator('#out')).not.toContainText('412 patients');
  expect(state.askCalls).toBe(0);
});
