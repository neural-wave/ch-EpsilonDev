const {test} = require('node:test');
const assert = require('node:assert/strict');
const R = require('../../frontend/js/result.js');
const policy = {demo_min_group_size:null, policy_version:1};
const available = {state:'available',config:policy};
const make = (roles, rows, columns = roles.map((_,i) => `c${i}`)) => ({columns, rows, column_metadata:roles.map((role,i) => ({name:columns[i],role,kind:role === 'patient_count' ? 'distinct_patient_count' : 'unknown'})), result_status:'released', suppression:null, truncated:false,row_limit:200});
test('dictionary normalization and strict numbers', () => {
  assert.deepEqual(R.normalize(make(['category','patient_count'],[{c0:'00.01',c1:null},{c1:'5'}])), [['00.01',''],['','5']]);
  for (const value of ['',null,'1x','1,000','Infinity','0x10']) assert.equal(R.number(value),null);
  assert.equal(R.number('3.2'),3.2);
});
test('visual hierarchy and metadata prevent code guessing', () => {
  assert.equal(R.visual(make(['patient_count'],[['5']])).kind,'number');
  assert.equal(R.visual(make(['unknown'],[['00.01']])).kind,'table');
  assert.equal(R.visual(make(['category','patient_count'],[['00.01','5']])).kind,'bar');
  assert.equal(R.visual(make(['category','patient_count','patient_count'],[['male','5','8']])).kind,'grouped');
  assert.equal(R.visual(make(['category','patient_count'],[['a','5'],['a','6']])).kind,'table');
  assert.equal(R.visual(make(['category','patient_count'],[['a','oops']])).kind,'table');
  assert.equal(R.visual(make(['category','patient_count'],[['a','']])).values[0][0],'');
});
test('long-form pivot preserves order, missing pairs and duplicates', () => {
  const d = make(['category','category','patient_count'],[['a','yes','5'],['b','no','6']]);
  assert.deepEqual(R.visual(d).values,[['5',''],['','6']]);
  d.rows.push(['a','yes','7']); assert.equal(R.visual(d).kind,'table');
});
test('CSV quoting, Unicode, formula neutralization, numeric negatives', () => {
  const d = make(['category','numeric_measure'],[['=cmd','-3'],['  @cmd','2'],['a,"b\nü','4'],['\tcmd','5']]);
  const csv = R.delimited(d);
  assert.ok(csv.includes("'=cmd,-3")); assert.ok(csv.includes("'  @cmd"));
  assert.ok(csv.includes('"a,""b\nü"')); assert.ok(csv.includes("'\tcmd"));
});
test('snapshot exact schema, round trip, size and shape rejection', () => {
  const d = make(['patient_count'],[['5']]);
  const s = R.snapshot('<script>question</script>',d);
  const imported = R.validateSnapshot(JSON.stringify(s));
  assert.ok(imported.imported); assert.deepEqual(imported.rows,[['5']]);
  assert.equal(imported.sparql_query,undefined);
  for (const change of [{format_version:2},{captured_at:'2026-02-30T00:00:00Z'},{captured_at:'2026-10-17'},{captured_at:'2026-10-17T24:00:00Z'},{rows:[[{}]]},{diagnostics:{}},{column_metadata:[]},{suppression:{enabled:true}}]) assert.throws(() => R.validateSnapshot(JSON.stringify({...s,...change})));
  assert.throws(() => R.validateSnapshot('x'.repeat(2097153)));
});
test('policy matching is explicit and versioned', () => {
  const d = make(['patient_count'],[['5']]); assert.equal(R.policyMatches(d,null),false);
  assert.ok(R.policyMatches(d,policy));
  const active = {demo_min_group_size:5, policy_version:1}; assert.equal(R.policyMatches(d,active),false);
  d.suppression = {enabled:true,status:'released',threshold:5,policy_version:1}; assert.ok(R.policyMatches(d,active));
  assert.equal(R.policyMatches(d,{...active, demo_min_group_size:6}),false);
});
// Small DOM stand-in checks the renderer's structure and event handlers without a build dependency.
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.innerHTML=''; this.style={}; this.dataset={}; }
  setAttribute() {}
  querySelectorAll(tag) { return this.children.flatMap((child) => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  querySelector() { return null; }
  appendChild(e) { this.children.push(e); }
  remove() { this.removed=true; }
  select() {}
}
global.document = {createElement:(tag) => new Element(tag), body:new Element(), execCommand:() => true};
test('renderer always tables, paginates, escapes imports and gates tools', () => {
  const host = new Element(), d = make(['patient_count'],[['5']]); R.mount(host,'question',d,{policy:available});
  assert.match(host.children[0].children[1].innerHTML,/result-number/);
  assert.match(host.children[0].children[1].children[0].innerHTML,/<table/);
  const many = make(['category','patient_count'],Array.from({length:51},(_,i) => [String(i),'5']));
  const h = new Element(); R.mount(h,'question',many,{policy:available});
  const table = h.children[0].children[1].children[0]; assert.match(table.innerHTML,/1–50 of 51/);
  table.children[1].onclick(); assert.match(table.innerHTML,/51–51 of 51/);
  const imported = new Element(); R.mount(imported,'q',{...d, imported:true,rows:[['<script>']]},{policy:available});
  assert.match(imported.children[0].children[1].children[0].innerHTML,/&lt;script&gt;/);
  assert.equal(imported.children[0].children[1].children.length,2);
  assert.equal(imported.children[0].children[1].children[1].textContent,'This saved format does not include the complete reproducibility record.');
  const withheld = new Element(); R.mount(withheld,'q',{result_status:'withheld',suppression:{reason:'Small group'}});
  assert.equal(withheld.children[0].children.length,0); assert.match(withheld.children[0].innerHTML,/Small group/);
});
test('clipboard fallback success and failure', async () => {
  Object.defineProperty(global,'navigator',{value:{clipboard:{writeText:async () => {throw Error();}}},configurable:true});
  await R.clipboard('table');
  document.execCommand = () => false;
  await assert.rejects(R.clipboard('table'));
});

test('reproducibility record text is exact, validated and excluded from snapshots', () => {
  const record = {source:'database',question:'  Original café question\n  ',executed_at:'2026-10-17T17:00:00+00:00',
    dataset:{name:'Fixture dataset',version:null,description:null},sparql_query:'SELECT (COUNT(*) AS ?n) WHERE {}\n'};
  assert.ok(R.validReproducibility(record));
  assert.equal(R.reproducibilityText(record),
    'Database execution\n\n- Source: Database execution\n- Question:   Original café question\n  \n- Result: Not recorded\n- Executed at: 2026-10-17T17:00:00+00:00 (UTC)\n- Dataset: Fixture dataset\n- Dataset version: Not configured\n- Dataset description: Not configured\nDataset version unavailable; reproducibility record is incomplete.');
  for (const patch of [{executed_at:'2026-02-30T00:00:00Z'},{source:'unknown'},{dataset:{}}]) assert.ok(!R.validReproducibility({...record,...patch}));
  assert.ok(!R.reproducibilityText(record).includes(record.sparql_query));
  const d = {...make(['patient_count'],[['12']]),reproducibility:record,sparql_query:record.sparql_query};
  const exported = R.snapshot(record.question,d);
  assert.equal(exported.reproducibility,undefined); assert.equal(exported.sparql_query,undefined);
  const synthetic = R.syntheticRecord('Synthetic question',record.sparql_query);
  assert.ok(R.validReproducibility(synthetic)); assert.equal(synthetic.executed_at,null);
  assert.ok(R.reproducibilityText(synthetic).startsWith('Preview mode (no database)\n\n- Source: Synthetic preview, not for citation\n'));
  assert.ok(R.reproducibilityText(synthetic).includes('- Executed at: Not applicable'));
});

test('preview copy format includes only released illustrative values and preserves zero and gaps', () => {
  const r = R.syntheticRecord('how many people have type 2 diabetes');
  const d = make(['patient_count'],[['412']]);
  assert.equal(R.reproducibilityText(r,d), 'Preview mode (no database)\n\n- Source: Synthetic preview, not for citation\n- Question: how many people have type 2 diabetes\n- Result: 412 (illustrative number)\n- Executed at: Not applicable\n- Dataset: None, demonstration only');
  assert.ok(R.reproducibilityText(r,make(['patient_count'],[[0]])).includes('- Result: 0 (illustrative number)'));
  assert.ok(R.reproducibilityText(r,make(['patient_count'],[[null]])).includes('- Result: No value'));
  const table = {...make(['category','patient_count'],[['A',null],['B',12]]),truncated:true};
  const text = R.reproducibilityText(r,table);
  assert.ok(text.includes('A\t')); assert.ok(text.includes('B\t12'));
  assert.ok(text.includes('Results truncated at 200 rows.'));
});

test('live visualization follows verified roles across arbitrary aliases and column permutations', () => {
  const rows = [{'band':'18–39','condition_A':'9','condition_B':'17'},{'band':'40–64','condition_A':'23','condition_B':'31'}];
  for (const columns of [['band','condition_A','condition_B'],['condition_B','band','condition_A'],['condition_A','condition_B','band']]) {
    const d={columns,rows,column_metadata:columns.map((name) => ({name,role:name==='band' ? 'category':'patient_count',kind:'verified'}))};
    const chart=R.visual(d);
    assert.equal(chart.kind,'grouped'); assert.deepEqual(chart.labels,['18–39','40–64']);
    for (let i=0;i<rows.length;i++) for (let j=0;j<chart.series.length;j++) assert.equal(chart.values[i][j],rows[i][chart.series[j]]);
  }
  const bar=make(['patient_count','category'],[['9','00.01']],['arbitrary_total','diagnosis_code']);
  assert.deepEqual(R.visual(bar),{kind:'bar',labels:['00.01'],series:['arbitrary_total'],values:[['9']]});
  bar.column_metadata[0].name='wrong'; assert.equal(R.visual(bar).kind,'table');
});

test('reordered long-form live comparisons pivot without changing values or filling missing groups', () => {
  const d=make(['patient_count','category','category'],[['13','with','young'],['17','without','young'],['21','with','middle'],['25','without','middle'],['33','with','older']],['n','cohort','age']);
  const chart=R.visual(d);
  assert.equal(chart.kind,'grouped'); assert.deepEqual(chart.labels,['young','middle','older']);
  assert.deepEqual(chart.series,['with','without']); assert.deepEqual(chart.values,[['13','17'],['21','25'],['33','']]);
  d.rows.push(['11','with','young']); assert.equal(R.visual(d).kind,'table');
  assert.ok(R.comparisonRequested('How many people have diabetes compared with those without it?'));
  assert.equal(R.comparisonRequested('How many patients have diabetes?'),false);
});
