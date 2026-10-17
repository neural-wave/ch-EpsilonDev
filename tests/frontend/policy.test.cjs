const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createPolicy, validate, MOCK_POLICY} = require('../../frontend/js/policy.js');

test('mock policy loads without fetching and subscribers can unsubscribe', async () => {
  const p = createPolicy({fetcher:() => {throw Error('Must not fetch');}});
  const states = []; const stop = p.subscribe((p) => states.push(p.state));
  await p.load({mock:true});
  assert.deepEqual(p.config,MOCK_POLICY);
  assert.deepEqual(states,['loading','loading','available']);
  stop(); await p.load({mock:true}); assert.equal(states.length,3);
});
test('failed policies clear configuration and retry restores it', async () => {
  let ok = true;
  const p = createPolicy({fetcher:async () => ({ok,json:async () => MOCK_POLICY})});
  await p.load(); assert.equal(p.state,'available');
  ok = false; await p.load(); assert.equal(p.state,'failed'); assert.equal(p.config,null);
  ok = true; await p.load(); assert.equal(p.state,'available');
});
test('invalid schema, JSON, network failures and timeout fail closed', async () => {
  for (const fetcher of [
    async () => ({ok:true,json:async () => ({...MOCK_POLICY,demo_min_group_size:true})}),
    async () => ({ok:true,json:async () => {throw Error('Bad JSON');}}),
    async () => {throw Error('Disconnected');},
    () => new Promise(() => {}),
  ]) {
    const p = createPolicy({fetcher,timeoutMs:5}); await p.load();
    assert.equal(p.state,'failed'); assert.equal(p.config,null);
  }
  for (const data of [null,{}, {...MOCK_POLICY,extra:1},{...MOCK_POLICY,demo_min_group_size:0}]) assert.throws(() => validate(data));
});
test('older response cannot replace a newer policy load', async () => {
  let finish;
  const p = createPolicy({fetcher:() => new Promise((resolve) => {finish = resolve;})});
  const old = p.load(); await p.load({mock:true});
  finish({ok:true,json:async () => ({...MOCK_POLICY,demo_min_group_size:9})});
  await old; assert.equal(p.config.demo_min_group_size,null);
});
