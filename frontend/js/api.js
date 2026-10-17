// API client. Pages call SQ.api only; switching SQ.config.API_MODE to 'live'
// sends the same calls to the real backend.
window.SQ = window.SQ || {};

(function () {
  const F = () => SQ.fixtures;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => s.toLowerCase().replace(/\s+/g, " ").replace(/[?.!]+$/, "").trim();

  // ---------- Live ----------

  async function liveNlToSparql(text) {
    const res = await fetch(`${SQ.config.API_BASE}/nl-to-sparql?debug=true`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `The server answered ${res.status}.`);
    return body;
  }

  async function liveRun(query) {
    const res = await fetch(`${SQ.config.API_BASE}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sparql_query: query }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `The server answered ${res.status}.`);
    return body;
  }

  // ---------- Mock ----------

  function findFixture(text) {
    const n = norm(text);
    return F().FIXTURES.find((f) => norm(f.text) === n);
  }

  function findTerm(text) {
    return F().TERMS.find((t) => t.re.test(text));
  }

  // Builds a fixture on the fly for free text, using the intent-guard keyword idea.
  function improvise(text) {
    const term = findTerm(text);
    const t = text.toLowerCase();
    const codeIri = term ? F().ICD + term.code : null;
    const codes = term ? [{ iri: codeIri, label: term.label, lang: "de", score: 0.9 }] : [];
    const timingsLlm = { retrieve: 38, llm: 1180, validate: 11, compliance: 7 };
    const patientNo = t.match(/patient\s*(?:no\.?|number|#)?\s*(\d{2,})/);

    if (/\b(delete|insert|drop|clear|remove|update)\b/.test(t)) {
      const base = F().FIXTURES.find((f) => f.id === "a-delete");
      return Object.assign({}, base, { text, codes });
    }
    if (patientNo) {
      const id = patientNo[1].padStart(4, "0");
      const base = F().FIXTURES.find((f) => f.id === "a-count-0042");
      const sparql = base.sparql.replace(/0042/g, id);
      return Object.assign({}, base, {
        text, sparql,
        fails: {
          R1: { msg: `A query over the graph of one patient (${id}). Totals must cover all patients.`, match: `GRAPH <http://example.org/graph/mock_data-1_${id}>` },
          IG: { msg: `The request names one patient ("patient ${patientNo[1]}").` },
        },
        reason: `This query looks at a single patient (${id}). Only questions about the whole cohort are allowed.`,
      });
    }
    if (/\b(which (patients|ones|people)|who (has|have|are)|list|show (me )?(each|all|every)|ids?|identifiers?|pseudo|names?)\b/.test(t)) {
      if (!term) return null;
      const base = F().FIXTURES.find((f) => f.id === "a-which-ones");
      return Object.assign({}, base, { text, sparql: base.sparql.replace(/icd-10-gm:A00/g, `<${codeIri}>`), codes });
    }
    if (/(everything|entire|whole|all (the )?data|in total)/.test(t) && !term) {
      const base = F().FIXTURES.find((f) => f.id === "a-whole-graph");
      return Object.assign({}, base, { text });
    }
    // A count fixture cannot answer an arbitrary comparison or grouped question.
    if (SQ.result.comparisonRequested(text) || /\bgroup(?:ed|ing)?\s+by\b|\bbreakdown\b|\bsplit\s+by\b|\bby\s+(?:age|sex|gender|cohort|group|diagnosis|country|year|month|hospital|treatment)\b/i.test(text)) {
      const error = new Error('Preview mode cannot execute arbitrary comparison or grouped questions. Use the live console with a configured model and database. No comparison counts were generated.');
      error.kind = 'mock-shape'; throw error;
    }
    if (term && /(how many|count|number of|share|percentage|proportion|patients with|patients have)/.test(t)) {
      const base = F().FIXTURES.find((f) => f.id === "c-diabetes");
      return Object.assign({}, base, {
        text,
        sparql: base.sparql.replace(`${F().ICD}E11`, codeIri),
        codes,
        run: { columns: ["countResult"], rows: [[String(40 + Math.floor(Math.random() * 600))]] },
      });
    }
    if (term) {
      // Known term, unusual shape: pretend the model handled it.
      const base = F().FIXTURES.find((f) => f.id === "c-diabetes");
      return Object.assign({}, base, {
        text, path: "llm", tokens: 1580, attempts: 1, timings: timingsLlm,
        sparql: base.sparql.replace(`${F().ICD}E11`, codeIri), codes,
        examples: ["ex-billed-dx-count", "ex-03 cholera by sex"],
        run: { columns: ["countResult"], rows: [[String(40 + Math.floor(Math.random() * 600))]] },
      });
    }
    return null;
  }

  // Walks the stepper through the stages so the loading state can be reviewed.
  async function playStages(timings, onStage) {
    const order = timings.template !== undefined ? ["retrieve", "template", "validate", "compliance"] : ["retrieve", "llm", "validate", "compliance"];
    const floor = { retrieve: 250, template: 250, llm: 900, validate: 200, compliance: 200 };
    for (const stage of order) {
      onStage && onStage(stage);
      await wait(Math.max(floor[stage], Math.min(timings[stage] || 0, 2200)));
    }
  }

  async function mockNlToSparql(text, opts) {
    if (!text || !text.trim()) throw new Error("Type a question first.");
    const fixture = findFixture(text) || improvise(text);
    if (!fixture) {
      await playStages({ retrieve: 40, llm: 0, validate: 0, compliance: 0 }, opts.onStage);
      const err = new Error("No medical term in this question matched the mock terminology index. Try one of the examples, or mention a condition such as diabetes, cholera, asthma or hypertension.");
      err.kind = "no-term";
      throw err;
    }
    await playStages(fixture.timings, opts.onStage);
    return F().toResponse(fixture);
  }

  async function mockRun(query) {
    await wait(380);
    const f = F().FIXTURES.find((x) => x.sparql === query && x.run);
    if (f) return Object.assign({ elapsed_ms: 40 + Math.round(Math.random() * 260) }, f.run);
    return { columns: ["countResult"], rows: [[String(40 + Math.floor(Math.random() * 600))]], elapsed_ms: 52 };
  }

  // ---------- Team backend (EpsilonDev26/ep-rep), reached through dev_server.py ----------

  async function call(method, path, body) {
    let res;
    try {
      res = await fetch(`${SQ.config.API_BASE}${path}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new Error("Could not reach the dev server. Start it with: python3 dev_server.py");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `The backend answered ${res.status}.`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  SQ.backend = {
    createShare: (share_token) => call("POST", "/demo/shares", {share_token}),
    sharedResult: (id) => call("GET", `/demo/shares/${encodeURIComponent(id)}`),
    demoConfig: () => call("GET", "/demo/config"),
    demoAsk: (text) => call("POST", "/demo/ask", { text }),          // verdict, rephrase, timings, rows
    clarify: (text) => call("POST", "/demo/clarify", { text }),      // "Did you mean...?" choices, no model call
    agentsRun: (text) => call("POST", "/agents/run", { text }),      // full result: flags, violations, retries
    preview: (text) => call("POST", "/agents/preview", { text }),    // context the model sees, no model call
    health: () => call("GET", "/health"),
    config: () => call("GET", "/agents/config"),
    check: () => call("POST", "/agents/check"),
    reports: () => call("GET", "/agents/reports"),
    report: (name) => call("GET", `/agents/reports/${encodeURIComponent(name)}`),
  };

  SQ.api = {
    nlToSparql(text, opts = {}) {
      return SQ.config.API_MODE === "live" ? liveNlToSparql(text) : mockNlToSparql(text, opts);
    },
    run(query) {
      return SQ.config.API_MODE === "live" ? liveRun(query) : mockRun(query);
    },
    async bench() {
      if (SQ.config.API_MODE !== "live") return SQ.fixtures.bench;
      const res = await fetch(SQ.config.BENCH_URL);
      if (!res.ok) throw new Error(`Could not load ${SQ.config.BENCH_URL} (${res.status}).`);
      return res.json();
    },
  };
})();
