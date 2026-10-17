const {expect} = require('@playwright/test');
const disabledPolicy = {demo_min_group_size:null,row_limit:200,policy_version:1};
function result(roles = ['patient_count'], rows = [{patients:'12'}], columns = ['patients']) {
  return {is_compliant:true,executed:true,result_status:'released',columns,rows,
    column_metadata:columns.map((name,i) => ({name,role:roles[i],kind:roles[i] === 'patient_count' ? 'distinct_patient_count' : 'group_key'})),
    suppression:null,truncated:false,row_limit:200,timings_ms:{total:10},tokens_consumed:20};
}
function snapshot() {
  const d = result();
  return {format_version:1,question:'Saved synthetic question',columns:d.columns,rows:[['12']],column_metadata:d.column_metadata,
    captured_at:'2026-10-17T12:00:00Z',truncated:false,row_limit:200,suppression:null};
}
const upload = (data = snapshot()) => ({name:'synthetic.snapshot.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(data))});
async function boot(page, options = {}) {
  const state = {config:disabledPolicy,reply:result(),configCalls:0,askCalls:0,questions:[],...options};
  await page.route('**/demo/config',async (route) => {
    state.configCalls++;
    if (state.configHandler) return state.configHandler(route);
    return route.fulfill({json:state.config});
  });
  await page.route('**/demo/ask',async (route) => {state.askCalls++; state.questions.push(route.request().postDataJSON().text); await route.fulfill({json:state.reply});});
  await page.goto('/console/cli/?intro=0' + (options.mock ? '&mock=1' : ''));
  return state;
}
async function ready(page) {
  await expect.poll(() => page.evaluate(() => SQ.policy.state)).toBe('available');
}
async function ask(page, question = 'How many patients have a billed diagnosis?') {
  await page.locator('#cmd').fill(question);
  await page.locator('#prompt-form').getByRole('button',{name:'Send',exact:true}).click();
  await expect(page.locator('#cmd')).toBeEnabled();
}
async function sidebar(page) {
  const toggle = page.locator('#side-toggle');
  if (await toggle.isVisible() && await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
}
async function readDownload(download) {
  const chunks = []; const stream = await download.createReadStream();
  for await (const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}
module.exports = {disabledPolicy,result,snapshot,upload,boot,ready,ask,sidebar,readDownload};
