// One place to switch the UI from mock data to the real backend.
// 'mock' = canned responses from fixtures.js, no server needed.
// 'live' = POST {API_BASE}/nl-to-sparql?debug=true and POST {API_BASE}/run.
window.SQ = window.SQ || {};

SQ.config = {
  APP_NAME: "SIB DOC",
  API_MODE: "mock",
  API_BASE: "",
  BENCH_URL: "artifacts/bench/latest.json",
  MODEL_LABEL: "Qwen3-4B (mock)",
};
