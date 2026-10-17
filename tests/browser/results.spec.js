const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result,snapshot,upload,readDownload} = require('./fixtures');

test('actual console renders count, category, wide, long, missing and duplicate shapes',async ({page}) => {
  const state = await boot(page); await ready(page); await ask(page);
  await expect(page.locator('.result-number')).toHaveText('12');
  await expect(page.locator('tbody td')).toHaveText('12');
  await expect(page.locator('.result-label')).toHaveCount(0);
  await expect(page.locator('tbody td')).toBeHidden();
  const exact=page.getByText('Show exact value',{exact:true});
  await exact.focus(); await exact.press('Enter');
  await expect(page.locator('tbody td')).toBeVisible();
  await exact.press('Space'); await expect(page.locator('tbody td')).toBeHidden();

  const shapes = [
    {roles:['category','patient_count'],columns:['code','patients'],rows:[{patients:'8',code:'00.01'}],chart:true},
    {roles:['category','patient_count','patient_count'],columns:['age','A','B'],rows:[{B:'9',age:'18–39',A:'6'}],chart:true},
    {roles:['category','category','patient_count'],columns:['age','cohort','patients'],rows:[{age:'young',cohort:'A',patients:'5'},{age:'older',cohort:'B',patients:'8'}],chart:true,missing:true},
    {roles:['category','category','patient_count'],columns:['age','cohort','patients'],rows:[{age:'young',cohort:'A',patients:'5'},{age:'young',cohort:'A',patients:'6'},{age:'young',cohort:'B',patients:'8'}],chart:false},
    {roles:['unknown'],columns:['code'],rows:[{code:'00.01'}],chart:false},
    {roles:['category','patient_count'],columns:['age','patients'],rows:[{age:'young',patients:null}],chart:true,missing:true},
  ];
  for (const shape of shapes) {
    await page.locator('#home-logo').click();
    state.reply = result(shape.roles,shape.rows,shape.columns); await ask(page);
    await expect(page.locator('tbody tr')).toHaveCount(shape.rows.length);
    await expect(page.locator('.result-exact-value')).toHaveCount(0);
    await expect(page.locator('.result-chart')).toHaveCount(shape.chart ? 1 : 0);
    if (shape.missing) await expect(page.locator('.result-chart')).toContainText('No value');
  }
});

test('withheld, forbidden, database errors and empty results have distinct UI',async ({page}) => {
  const state = await boot(page); await ready(page);
  const states = [
    [{is_compliant:true,executed:true,result_status:'withheld',rows:null,columns:null,column_metadata:[],suppression:{reason:'Small group hidden'}},'Small group hidden'],
    [{is_compliant:false,executed:false,result_status:'not_executed',sparql_query:null,compliance_reason:'Individual requests are blocked.'},'Individual requests are blocked.'],
    [{is_compliant:true,executed:false,result_status:'execution_error',execution_error:'Synthetic database unavailable'},'execution failed'],
  ];
  for (const [reply,message] of states) {
    state.reply = reply; await ask(page);
    await expect(page.locator('#out')).toContainText(message);
    await expect(page.locator('#out table')).toHaveCount(0);
    await expect(page.locator('.result-tools')).toHaveCount(0);
    await expect(page.locator('.reproducibility-card')).toHaveCount(0);
    await expect(page.locator('.privacy strong')).toBeVisible();
    await expect(page.locator('#out')).not.toContainText('database disconnected');
    await page.locator('#home-logo').click();
  }
  state.reply = result(['patient_count'],[]); await ask(page);
  await expect(page.locator('table')).toHaveCount(1); await expect(page.locator('tbody tr')).toHaveCount(0);
  await expect(page.locator('.result-table-wrap')).toContainText('0–0 of 0');
});

test('table pagination and truncation use explicit metadata',async ({page}) => {
  const state = await boot(page,{reply:result(['category','patient_count'],Array.from({length:200},(_,i) => ({group:`group ${i}`,patients:String(i + 5)})),['group','patients'])});
  await ready(page); await ask(page);
  await expect(page.locator('tbody tr')).toHaveCount(50);
  await expect(page.getByText('Results are truncated at 200 rows by the backend.')).toHaveCount(0);
  await page.getByRole('button',{name:'Next',exact:true}).click();
  await expect(page.locator('.result-table-wrap')).toContainText('51–100 of 200');
  state.reply.truncated = true; await page.locator('#home-logo').click(); await ask(page);
  await expect(page.getByText('Results are truncated at 200 rows by the backend.')).toBeVisible();
});

test('CSV downloads all returned rows with quoting and formula protection',async ({page},testInfo) => {
  const rows = Array.from({length:51},(_,i) => ({label:i ? `row ${i}` : '  =SUM(1,2)\nü',measure:'-3'}));
  await boot(page,{reply:result(['category','numeric_measure'],rows,['label','measure'])}); await ready(page); await ask(page);
  const downloading = page.waitForEvent('download');
  await page.getByRole('button',{name:'Download CSV',exact:true}).click();
  const file = await downloading; await file.saveAs(testInfo.outputPath('synthetic.csv'));
  const csv = await readDownload(file);
  expect(csv).toContain('"\'  =SUM(1,2)\nü",-3'); expect(csv).toContain('row 50,-3');
});

test('clipboard success and fallback failure provide feedback',async ({page}) => {
  await boot(page); await ready(page); await ask(page);
  await page.evaluate(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async (text) => {window.copiedTable = text;}}}));
  await page.getByRole('button',{name:'Copy table',exact:true}).click();
  await expect(page.locator('.toast').last()).toHaveText('Table copied');
  expect(await page.evaluate(() => window.copiedTable)).toBe('patients\r\n12');
  await page.evaluate(() => {
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async () => {throw Error('denied');}}});
    document.execCommand = () => false;
  });
  await page.getByRole('button',{name:'Copy table',exact:true}).click();
  await expect(page.locator('.toast').last()).toHaveText('Could not export this result.');
});

test('snapshot download and import round trip never executes until re-asked',async ({page},testInfo) => {
  const state = await boot(page); await ready(page); await ask(page);
  page.once('dialog',async (dialog) => {expect(dialog.message()).toContain('How many patients'); await dialog.accept();});
  const downloading = page.waitForEvent('download');
  await page.getByRole('button',{name:'Download snapshot',exact:true}).click();
  const file = await downloading; await file.saveAs(testInfo.outputPath('synthetic.snapshot.json'));
  const data = JSON.parse(await readDownload(file));
  expect(Object.keys(data)).toEqual(Object.keys(snapshot()));
  expect(data.rows).toEqual([['12']]); expect(data.sparql_query).toBeUndefined();
  await page.locator('#home-logo').click();
  await page.locator('#snapshot-import').setInputFiles(upload(data));
  await expect(page.getByText('Imported snapshot — not re-verified against live data')).toBeVisible();
  await expect(page.locator('.result-tools')).toHaveCount(0); expect(state.askCalls).toBe(1);
  await page.getByRole('button',{name:'Re-ask for a verified result'}).click();
  await expect.poll(() => state.askCalls).toBe(2);
});

test('malformed snapshots and invalid timestamps produce errors without execution',async ({page}) => {
  const state = await boot(page); await ready(page);
  for (const patch of [{captured_at:'2026-02-30T00:00:00Z'},{captured_at:'2026-10-17'},{column_metadata:[]},{rows:[[{id:'unsafe'}]]},{sparql_query:'excluded'}]) {
    await page.locator('#snapshot-import').setInputFiles(upload({...snapshot(),...patch}));
    await expect(page.locator('.toast').last()).toContainText('Cannot import snapshot');
    await expect(page.locator('.entry')).toHaveCount(0);
  }
  expect(state.askCalls).toBe(0);
});
