const {test,expect} = require('@playwright/test');
const {boot,ready,ask} = require('./fixtures');

test('a request that is not a question gets a neutral note, no verdict badge and no result', async ({page}) => {
  const state = await boot(page, {reply:{is_compliant:false,not_a_question:true,executed:false,result_status:'not_executed',
    sparql_query:null,compliance_reason:'This is not a question about the patient data.',timings_ms:{total:3},tokens_consumed:0}});
  await ready(page); await ask(page, 'hey');
  await expect(page.locator('.answer.is-note')).toContainText('This is not a question about the patient data.');
  await expect(page.locator('.badge-block, .badge-allow, .result-number, .result-chart')).toHaveCount(0);
  expect(state.askCalls).toBe(1);
});
