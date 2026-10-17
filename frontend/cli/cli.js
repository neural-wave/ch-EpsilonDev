// Console interface over the same SQ.api and fixtures as the graphical UI.
// Every question or command becomes one entry: a badge + title, then panels.
(function () {
  const { esc, fmtMs, fmtInt, fmtPct, highlight, evidenceMarks } = SQ.ui;
  const F = SQ.fixtures;

  const out = document.getElementById("out");
  const input = document.getElementById("cmd");
  const form = document.getElementById("prompt-form");
  const params = new URLSearchParams(location.search);
  // The console talks to the team backend through dev_server.py; ?mock=1 falls back to fixtures.
  SQ.config.API_MODE = params.get("mock") === "1" ? "mock" : "live";
  const live = SQ.config.API_MODE === "live";

  // ---------- theme: light by default, dark when chosen (remembered in this browser) ----------
  const THEME_KEY = "qg-console-theme";
  const themeBtn = document.getElementById("theme-btn");
  const MOON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
  const SUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="8" height="8"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg>';
  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
  function setTheme(theme, remember) {
    if (theme === "dark") document.documentElement.setAttribute("data-theme", "dark");
    else document.documentElement.removeAttribute("data-theme");
    themeBtn.innerHTML = theme === "dark" ? SUN : MOON;
    themeBtn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    themeBtn.title = themeBtn.getAttribute("aria-label");
    if (remember) { try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode */ } }
  }
  setTheme(isDark() ? "dark" : "light", false); // the inline script in index.html already applied a saved choice
  themeBtn.addEventListener("click", () => setTheme(isDark() ? "light" : "dark", true));
  document.getElementById("title").textContent = SQ.config.APP_NAME;
  const chip = document.getElementById("mode-chip");
  chip.textContent = live ? "Live" : "Preview";
  if (!live) chip.title = "Synthetic examples only — not live patient data";
  chip.classList.toggle("is-live", live);
  if (live) {
    SQ.backend.health()
      .then((h) => { chip.textContent = `Live: ${h.backend}`; chip.title = h.backend === "stub" ? "Stand-in agents, no model" : "Real model on the GPU"; })
      .catch(() => { chip.textContent = "Backend offline"; chip.classList.remove("is-live"); });
  }

  SQ.policy.subscribe((policy) => {
    const available = policy.state === "available";
    document.getElementById("snapshot-import").disabled = !available;
    document.getElementById("retry-privacy-policy").hidden = policy.state !== "failed";
    document.getElementById("privacy-policy").textContent = available
      ? policy.config.demo_min_group_size === null
        ? "Minimum group sizes are not enforced (pending organizer policy)."
        : `Small groups below ${policy.config.demo_min_group_size} are hidden.`
      : policy.state === "loading" ? "Loading privacy policy…" : "Privacy policy unavailable";
  });
  const loadPolicy = () => SQ.policy.load({mock: !live});
  const policyReady = loadPolicy();
  document.getElementById("retry-privacy-policy").addEventListener("click", loadPolicy);

  const history = [];
  let hIndex = 0;
  let busy = false;
  let rejectPending = false; // a refused question shakes the field once it is usable again
  let last = null; // { question, res }

  const examples = F.FIXTURES.filter((f) => f.group === "cohort" || f.group === "lookalike");
  const attacks = F.FIXTURES.filter((f) => f.group === "attack");

  // ---------- building blocks ----------

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1600);
  }

  function copy(text, done) {
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
      .then(() => toast(done))
      .catch(() => toast("Copy is not available here. Select the text instead."));
  }

  function entry(kind, title) {
    document.body.classList.remove("is-empty");
    const el = document.createElement("article");
    el.className = "entry";
    el.innerHTML = `<div class="entry-head"><span class="badge ${kind === "ask" ? "badge-ask" : "badge-cmd"}">${kind === "ask" ? "ASK" : "CMD"}</span>
      <span class="entry-title${kind === "ask" ? "" : " is-cmd"}">${esc(title)}</span></div>`;
    out.appendChild(el);
    return el;
  }

  const add = (parent, html) => { parent.insertAdjacentHTML("beforeend", html); return parent.lastElementChild; };
  const label = (text) => `<h3 class="label">${esc(text)}</h3>`;
  const para = (html, cls = "text") => `<p class="${cls}">${html}</p>`;

  // rows: array of { cells: [html...], cls?, data? }
  function tbl(headers, rows, numCols = []) {
    return `<table class="tbl"><thead><tr>${headers.map((h, i) => `<th${numCols.includes(i) ? ' class="num"' : ""}>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr${r.cls ? ` class="${r.cls}"` : ""}${r.data ? ` data-try="${esc(r.data)}" tabindex="0"` : ""}>${r.cells.map((c, i) => `<td${numCols.includes(i) ? ' class="num"' : ""}>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function windowPanel(title, bodyHtml, copyText, bodyCls = "") {
    const el = document.createElement("div");
    el.className = "window";
    el.innerHTML = `<div class="window-bar"><span>${esc(title)}</span><span class="window-tools">${copyText ? '<button type="button" class="window-copy">copy</button>' : ""}<span class="squares" aria-hidden="true"><i></i><i></i><i></i></span></span></div>
      <div class="window-body ${bodyCls}">${bodyHtml}</div>`;
    if (copyText) el.querySelector(".window-copy").addEventListener("click", () => copy(copyText, "Copied"));
    return el;
  }

  const mode = live ? "live" : "mock";

  // ---------- commands ----------

  function privateDiagnostics(e) { add(e, para("Developer diagnostics are not available in the user console.")); }

  const COMMANDS = {
    help: { usage: "help", text: "List commands", fn: help },
    examples: { usage: "examples", text: "Cohort questions you can try", fn: listExamples },
    attacks: { usage: "attacks", text: "Requests built to extract patient data", fn: listAttacks },
    try: { usage: "try e1 | a1", text: "Ask an example or attack by its id", fn: tryOne },
    run: { usage: "run", text: "Run the last allowed query again", fn: runLast },
    explain: { usage: "explain", text: "What the model was given for the last question", fn: privateDiagnostics },
    status: { usage: "status", text: "Backend mode, model and whether it is reachable", fn: privateDiagnostics },
    json: { usage: "json", text: "Raw API response for the last question", fn: privateDiagnostics },
    rules: { usage: "rules", text: "The five rules and the intent check", fn: rules },
    bench: { usage: "bench", text: "Benchmark summary", fn: privateDiagnostics },
    theme: { usage: "theme", text: "Switch light and dark", fn: theme },
    clear: { usage: "clear", text: "Clear the view (previous questions stay in the sidebar)", fn: () => clearView() },
  };

  function help(e) {
    add(e, para("Type any question in plain English to translate and check it, or use a command."));
    add(e, tbl(["Command", "What it does"], Object.values(COMMANDS).map((c) => ({ cells: [`<span class="k">${esc(c.usage)}</span>`, esc(c.text)] }))));
  }

  function listExamples(e) {
    add(e, para("Click a row, or type <span class=\"cmdname\">try e1</span>."));
    add(e, tbl(["Id", "Question", "Note"], examples.map((f, i) => ({
      data: `e${i + 1}`,
      cls: "clickable",
      cells: [`<span class="k">e${i + 1}</span>`, esc(f.text), f.group === "lookalike" ? "looks risky, is allowed" : ""],
    }))));
  }

  function listAttacks(e) {
    add(e, para("Each of these should come back blocked. Click a row, or type <span class=\"cmdname\">try a1</span>."));
    add(e, tbl(["Id", "Request", "Should fail on"], attacks.map((f, i) => ({
      data: `a${i + 1}`,
      cls: "clickable",
      cells: [`<span class="k">a${i + 1}</span>`, esc(f.text), esc(Object.keys(f.fails || {}).map((k) => (k === "IG" ? "intent" : k)).join(", "))],
    }))));
  }

  function resolveId(arg) {
    const m = (arg || "").trim().toLowerCase().match(/^([ea])(\d+)$/);
    const list = m && (m[1] === "e" ? examples : attacks);
    return list ? list[Number(m[2]) - 1] : null;
  }

  async function tryOne(e, arg) {
    const f = resolveId(arg);
    if (!f) {
      add(e, para(`No example or attack called "${esc(arg || "")}". Use <span class="cmdname">examples</span> or <span class="cmdname">attacks</span> to see the ids.`));
      return;
    }
    e.remove();
    return ask(f.text);
  }

  async function runLast(e) {
    if (!last) return add(e, para("Nothing to run yet. Ask a question first."));
    if (live) {
      if (!last.res.is_compliant) return add(e, para("The last question was blocked, so nothing runs."));
      add(e, para("Asking the last question again; results come with the answer."));
      e.remove();
      return ask(last.question);
    }
    if (!last.res.is_compliant) return add(e, para("The last query was blocked, so it cannot be run."));
    const p = add(e, '<div class="progress">running on Virtuoso</div>');
    try {
      const r = await SQ.api.run(last.res.sparql_query);
      p.remove();
      await policyReady;
      const fixture = sparqlOf(last.question, last.res);
      SQ.result.mount(e, last.question, {...r, column_metadata:F.RESULT_METADATA[fixture?.id] || r.columns.map((name) => ({name,role:"unknown",kind:"unknown"})), result_status:"released",suppression:null,truncated:false,row_limit:200, reproducibility:SQ.result.syntheticRecord(last.question,last.res.sparql_query)}, {policy:SQ.policy,toast});
    } catch (err) {
      p.remove();
      add(e, para(`Run failed: ${esc(err.message)}`));
    }
  }

  async function explain(e, arg) {
    if (live) return explainLive(e, (arg || "").trim() || (last && last.question));
    if (!last) return add(e, para("Nothing to explain yet. Ask a question first."));
    const t = last.res.trace || {};
    add(e, tbl(["Field", "Value"], [
      { cells: ['<span class="k">question</span>', esc(last.question)] },
      { cells: ['<span class="k">intent</span>', esc(t.intent || "unknown")] },
      { cells: ['<span class="k">path</span>', t.path === "fast" ? "fast path, template, no model" : `model ${esc(t.model || "")}, ${t.attempts} ${t.attempts === 1 ? "attempt" : "attempts"}`] },
      { cells: ['<span class="k">examples</span>', esc((t.examples_used || []).join(", ") || "none")] },
    ]));
    const codes = t.candidate_codes || [];
    if (codes.length) {
      add(e, label("Codes offered to the model"));
      add(e, tbl(["Code", "Label", "Lang", "Match"], codes.map((c) => ({ cells: [`<span class="k">${esc(c.iri.replace(/^.*\/(?=[^/]+$)/, ""))}</span>`, esc(c.label), esc(c.lang), c.score.toFixed(2)] })), [3]));
    }
  }

  function json(e) {
    if (!last) return add(e, para("No response yet. Ask a question first."));
    const publicResponse = JSON.parse(JSON.stringify(last.res));
    stripSavedQueries(publicResponse);
    delete publicResponse.trace;
    const text = JSON.stringify(publicResponse, null, 2);
    const lines = text.split("\n").map((l) => `<span class="ln">${esc(l).replace(/^(\s*)(&quot;[^&]*?&quot;)(:)/, '$1<span class="tok-var">$2</span>$3')}</span>`).join("");
    e.appendChild(windowPanel(`response: ~/${mode}`, `<pre class="code plain">${lines}</pre>`, text));
  }

  function rules(e) {
    if (live) {
      add(e, para("The guard checks these rules on the generated query, in code. The intent agent reads the request itself and can block it too."));
      add(e, tbl(["Rule", "Forbidden", "Intent flag"], LIVE_RULES.map((r) => ({ cells: [`<span class="k">${r.id}</span>`, esc(r.name), r.flag ? `<span class="dim">${esc(r.flag)}</span>` : ""] }))));
      return;
    }
    add(e, para("The rules check the generated query in code. No model takes part in the verdict."));
    add(e, tbl(["Rule", "Name", "Passes when"], F.RULES.map((r) => ({ cells: [`<span class="k">${r.id === "IG" ? "intent" : r.id}</span>`, esc(r.name), esc(r.pass)] }))));
  }

  async function bench(e) {
    if (live) return benchLive(e);
    const data = await SQ.api.bench();
    const s = data.summary;
    if (!live) add(e, para("Mock numbers. They show the layout, not results.", "text dim"));
    add(e, tbl(["Measure", "Value", "Note"], [
      { cells: ['<span class="k">verdicts correct</span>', fmtPct(s.accuracy), `${Math.round(s.accuracy * s.n)} of ${s.n} requests`] },
      { cells: ['<span class="k">unsafe let through</span>', `<span class="${s.false_pos ? "fail" : "pass"}">${s.false_pos}</span>`, "false positives, the costly error"] },
      { cells: ['<span class="k">safe but blocked</span>', String(s.false_neg), "false negatives"] },
      { cells: ['<span class="k">median time</span>', fmtMs(s.p50_ms), `95th percentile ${fmtMs(s.p95_ms)}`] },
      { cells: ['<span class="k">tokens per request</span>', fmtInt(s.mean_tokens), "mean, all model calls"] },
      { cells: ['<span class="k">no model needed</span>', fmtPct(s.fast_path_share, 0), "fast-path templates"] },
    ]));
    add(e, label("Models"));
    add(e, tbl(["Model", "Size", "Correct", "Unsafe through", "Median", "Tokens"],
      data.models.map((m) => ({ cells: [`<span class="k">${esc(m.name)}</span>${m.chosen ? ' <span class="dim">chosen</span>' : ""}`, esc(m.params), fmtPct(m.accuracy), String(m.false_pos), fmtMs(m.p50_ms), fmtInt(m.tokens)] })), [1, 2, 3, 4, 5]));
  }

  function theme(e) {
    const now = isDark() ? "light" : "dark";
    setTheme(now, true);
    add(e, para(`Theme: ${now}.`));
  }

  // ---------- asking ----------

  const STAGE_TEXT = {
    retrieve: "retrieve: matching terms and examples",
    template: "generate: filling a tested template, no model",
    llm: "generate: the model is writing the query",
    validate: "validate: parsing and checking terms",
    compliance: "rules: checking R0 to R4 and intent",
  };
  const STAGE_LABEL = { retrieve: "retrieve", template: "template", llm: "model", validate: "validate", compliance: "rules" };

  async function ask(question, opts = {}) {
    if (live) return askLive(question, opts);
    const e = entry("ask", question);
    const p = add(e, '<div class="progress">starting</div>');
    const t0 = performance.now();
    let stage = "starting";
    const tick = setInterval(() => { p.textContent = `${stage}   ${((performance.now() - t0) / 1000).toFixed(1)}s`; }, 100);
    try {
      const res = await SQ.api.nlToSparql(question, { onStage: (s) => { stage = STAGE_TEXT[s] || s; } });
      last = { question, res };
      let result = null;
      let runError = null;
      if (res.is_compliant) {
        stage = "run: fetching the totals from Virtuoso";
        try { result = await SQ.api.run(res.sparql_query); } catch (err) { runError = err; }
      }
      clearInterval(tick);
      p.remove();
      await policyReady;
      report(e, question, res, result, runError);
    } catch (err) {
      clearInterval(tick);
      p.remove();
      add(e, `<div class="verdict"><span class="badge badge-cmd">ERROR</span><span class="verdict-text">${esc(err.message)}</span></div>`);
      if (err.kind === "no-term") add(e, para('Type <span class="cmdname">examples</span> for questions the mock knows.'));
    }
  }

  // ---------- answer-first helpers ----------

  const sparqlOf = (question, res) => {
    const f = F.FIXTURES.find((x) => x.text.toLowerCase() === question.trim().toLowerCase());
    return f && f.sparql === res.sparql_query ? f : null; // only trust fixture prose for the exact query
  };

  const humanize = (cls) => cls.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  const article = (w) => (/^[aeiou]/i.test(w) ? "an" : "a");
  const SYSTEMS = { "icd-10-gm": "ICD-10-GM", chop: "CHOP", loinc: "LOINC", ucum: "UCUM", atc: "ATC" };

  // Plain-English reading of a query, derived by rules from its text (no model call).
  function describe(sparql, codes) {
    const classes = [...new Set([...sparql.matchAll(/(?:\ba\b|rdf:type)\s+sphn:([A-Za-z]+)/g)].map((m) => m[1]))];
    const codeRefs = [];
    for (const m of sparql.matchAll(/<https:\/\/biomedit\.ch\/rdf\/sphn-resource\/([a-z0-9-]+)\/([^>\/]+)>|\b(icd-10-gm|chop|loinc):([A-Za-z0-9.\-]+)/g)) {
      const sys = m[1] || m[3];
      const code = m[2] || m[4];
      if (!SYSTEMS[sys]) continue;
      const hit = (codes || []).find((c) => c.iri.endsWith("/" + code));
      codeRefs.push(`${SYSTEMS[sys]} ${code}${hit ? ` (${hit.label})` : ""}`);
    }
    const subcodes = /OPTION\s*\(\s*inference/i.test(sparql);
    const groups = [...(sparql.match(/GROUP BY\s+((?:\?\w+\s*)+)$/m) || [, ""])[1].matchAll(/\?(\w+)/g)].map((m) => m[1].replace(/_/g, " "));
    const record = classes.find((c) => c !== "SubjectPseudoIdentifier") || classes[0];
    let s = "Patients";
    if (record) s += ` with ${article(humanize(record))} ${humanize(record)}`;
    if (codeRefs.length) s += ` coded ${[...new Set(codeRefs)].join(" or ")}${subcodes ? ", including all its subcodes" : ""}`;
    s += ".";
    if (groups.length) s += ` Grouped by ${groups.join(" and ")}.`;
    return s;
  }

  // Turn codes and IRIs in results into words.
  function readable(v, codes) {
    const s = String(v);
    if (!/^https?:\/\//.test(s)) return s;
    const hit = (codes || []).find((c) => c.iri === s);
    if (hit) return hit.label;
    return s.replace(/^.*[\/#]/, "");
  }

  // "1 patient matches", "767 patients match": the count decides singular or plural.
  const isOne = (v) => Number(String(v).replace(/,/g, "")) === 1;
  const patientsWord = (v) => (isOne(v) ? "patient" : "patients");

  function sentence(question, res, result, fixture) {
    if (fixture && fixture.answer && result) return fixture.answer.replace("{0}", result.rows[0] ? result.rows[0][0] : "");
    if (!result || !result.rows.length) return "The query ran but returned no rows.";
    if (result.rows.length === 1 && result.columns.length === 1) {
      const v = result.rows[0][0];
      const term = KNOWN_TERMS.find((t) => question.toLowerCase().includes(t));
      return term ? `${v} ${patientsWord(v)} ${isOne(v) ? "has" : "have"} ${term}.` : `${v} ${patientsWord(v)} ${isOne(v) ? "matches" : "match"}.`;
    }
    return `${result.rows.length} groups. The largest is ${readable(result.rows[0][0], res.trace && res.trace.candidate_codes)} with ${result.rows[0][1]}.`;
  }

  const KNOWN_TERMS = ["type 2 diabetes", "type 1 diabetes", "diabetes", "cholera", "asthma", "hypertension", "covid", "breast cancer", "cancer"];
  function alternative(question, res, fixtureAny) {
    if (fixtureAny && fixtureAny.alternative) return fixtureAny.alternative;
    const term = KNOWN_TERMS.find((t) => question.toLowerCase().includes(t));
    if (term) return `How many patients have ${term}?`;
    return "How many patients are in the cohort?";
  }

  function stewardDetails(res, result) {
    const t = res.trace || {};
    const tm = t.timings_ms || {};
    const d = document.createElement("details");
    d.className = "steward";
    d.innerHTML = `<summary><span>Details for data stewards</span><span class="steward-hint">rules, pipeline, SPARQL</span></summary><div class="steward-body"></div>`;
    const body = d.querySelector(".steward-body");
    add(body, label("Rules"));
    add(body, tbl(["Rule", "Check", "Result"], (t.rules || []).map((r) => ({
      cls: r.passed ? "" : "is-fail",
      cells: [
        `<span class="k">${r.id === "IG" ? "intent" : r.id}</span>`,
        `${esc(r.name)}${r.passed ? "" : `<span class="ev">${esc(r.message)}${r.evidence && r.evidence.line ? ` Line ${r.evidence.line}.` : ""}</span>`}`,
        r.passed ? '<span class="pass">pass</span>' : '<span class="fail">FAIL</span>',
      ],
    }))));
    add(body, label("Pipeline"));
    const rows = Object.keys(STAGE_LABEL).filter((k) => tm[k] > 0).map((k) => ({
      cells: [
        `<span class="k">${STAGE_LABEL[k]}</span>`,
        fmtMs(tm[k]),
        k === "llm" ? `${t.attempts} ${t.attempts === 1 ? "attempt" : "attempts"}, ${fmtInt(res.tokens_consumed)} tokens` : k === "template" ? "0 tokens" : "",
      ],
    }));
    if (result) rows.push({ cells: ['<span class="k">virtuoso</span>', fmtMs(result.elapsed_ms || 0), "running the query, outside the API's measured time"] });
    add(body, tbl(["Stage", "Time", "Note"], rows, [1]));
    body.appendChild(windowPanel(`response: ~/${mode}`, `<pre class="code">${highlight(res.sparql_query, evidenceMarks(t.rules))}</pre>`, res.sparql_query));
    return d;
  }

  function report(e, question, res, result, runError) {
    const t = res.trace || {};
    const tm = t.timings_ms || {};
    const codes = t.candidate_codes || [];
    const fixture = sparqlOf(question, res);
    const fixtureAny = F.FIXTURES.find((x) => x.text.toLowerCase() === question.trim().toLowerCase());
    const failed = (t.rules || []).filter((r) => !r.passed);
    const meta = fmtMs(tm.total || 0);

    if (res.is_compliant) {
      // 1. The answer, 2. what was counted
      const counted = t.interpretation || (fixture && fixture.counted) || describe(res.sparql_query, codes);
      if (runError) add(e, para(`Run failed: ${esc(runError.message)}`));
      if (result) {
        const authored = fixture || F.FIXTURES.find((item) => item.sparql === res.sparql_query);
        const metadata = F.RESULT_METADATA[authored?.id] || result.columns.map((name) => ({name, role:"unknown", kind:"unknown"}));
        SQ.result.mount(e, question, {...result, column_metadata:metadata, result_status:"released", suppression:null, truncated:false, row_limit:200, reproducibility:SQ.result.syntheticRecord(question,res.sparql_query)},
          {policy:SQ.policy, toast, caption:sentence(question, res, result, fixture), description:counted});
      }

      // 4. Small verdict line
      add(e, `<p class="verdict-small"><span class="badge badge-sm badge-allow">ALLOWED</span><span>Checked against five rules and the intent check before it ran. ${meta}.</span></p>`);
    } else {
      // Blocked: reason first, then a safe question to ask instead
      add(e, `<div class="answer is-blocked">
        <p class="verdict-small"><span class="badge badge-sm badge-block">BLOCKED</span><span>caught by ${esc(failed.map((r) => (r.id === "IG" ? "intent check" : r.id)).join(", "))}</span></p>
        <p class="answer-text">${esc(res.compliance_reason || "")}</p>
      </div>`);
      // 5. A safe alternative, offered not applied
      const alt = alternative(question, res, fixtureAny);
      add(e, `<div class="suggest"><span class="suggest-label">You could ask instead</span>
        <button type="button" class="suggest-btn" data-ask="${esc(alt)}"><span>${esc(alt)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg></button>
        <span class="dim small-note">Nothing was changed for you. The request above stays blocked.</span></div>`);
    }

    // 4. Everything technical, collapsed
  }

  // ---------- live: the team backend ----------

  const LIVE_RULES = [
    { id: "R1", name: "A single, identifiable patient", flag: "single_patient" },
    { id: "R2", name: "Results not aggregated across all patients", flag: "individual_rows" },
    { id: "R3", name: "Matching the whole graph without restriction", flag: "whole_graph" },
    { id: "R4", name: "Anything other than a read-only SELECT", flag: "not_a_select" },
    { id: "R5", name: "Exposing identifiers", flag: "asks_for_identifiers" },
    { id: "PARSE", name: "A query that cannot be read, so it cannot be checked", flag: null },
  ];
  const FLAG_TEXT = {
    single_patient: "names a single patient",
    individual_rows: "asks for individual records",
    whole_graph: "asks for everything in the database",
    not_a_select: "asks to change or export data",
    asks_for_identifiers: "asks for identifiers",
  };
  const STEP_TEXT = {
    context: "Find similar examples, schema cards and codes",
    intent: "Read the request for forbidden asks (intent agent, runs in parallel)",
    generate: "Write the query and check its syntax (query agent, judge)",
    guard: "Check the five rules on the query (guard)",
    generate_retry: "Rewrite the query after a problem (retry)",
    guard_retry: "Check the rewritten query (guard)",
  };
  // 1012 -> "1,012"; anything that is not a plain number is left as it is.
  const nice = (v) => (/^-?\d{4,}(\.\d+)?$/.test(String(v).trim()) ? Number(v).toLocaleString("en-US") : String(v));
  const humanCol = (c) => String(c).replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").toLowerCase();

  function liveError(e, err) {
    const hint = err.status === 400 ? "Check the question and try again."
      : err.status === 503 ? "The pipeline could not finish. With the real model this usually means the GPU is unreachable: type <span class=\"cmdname\">status</span>."
      : err.status === 502 ? "Start the team backend on port 8080, then try again."
      : "";
    add(e, `<div class="verdict"><span class="badge badge-cmd">ERROR</span><span class="verdict-text">${esc(err.message)}</span></div>`);
    if (hint) add(e, para(hint));
  }

  // Status words, roughly in pipeline order. The backend does not stream its steps,
  // so these set the mood rather than report exact progress.
  const THINKING = ["Reading your question", "Searching medical codes", "Finding similar questions", "Tinkering with the query",
    "Drafting the SPARQL", "Checking the privacy rules", "Cross-checking"];
  const STILL_THINKING = ["Still tinkering", "Weighing the rules", "Polishing", "Almost there"];
  const MIN_THINKING_MS = 650; // stub answers arrive in ~20 ms; a brief beat makes the change readable

  function thinking(e) {
    const el = add(e, `<div class="thinking" role="status"><span class="pulse" aria-hidden="true"></span>
      <span class="word" aria-hidden="true">${THINKING[0]}…</span><span class="secs" aria-hidden="true">0.0s</span>
      <span class="visually-hidden">Working on your question</span></div>`);
    const word = el.querySelector(".word");
    const secs = el.querySelector(".secs");
    const t0 = performance.now();
    let i = 0;
    let note = null;
    const tick = setInterval(() => {
      const s = (performance.now() - t0) / 1000;
      secs.textContent = `${s.toFixed(1)}s`;
      if (s > 15 && !note) note = add(e, '<p class="thinking-note">The model is taking its time. Slow questions can take up to a minute.</p>');
    }, 100);
    const rotate = setInterval(() => {
      i += 1;
      const w = i < THINKING.length ? THINKING[i] : STILL_THINKING[(i - THINKING.length) % STILL_THINKING.length];
      word.textContent = `${w}…`;
      word.classList.remove("swap");
      void word.offsetWidth; // restart the slide-in
      word.classList.add("swap");
    }, 1500);
    return () => { clearInterval(tick); clearInterval(rotate); el.remove(); if (note) note.remove(); };
  }

  function revealAnswer(e, from) {
    Array.from(e.children).slice(from).forEach((el, i) => {
      el.classList.add("reveal");
      el.style.animationDelay = `${i * 70}ms`;
    });
  }

  // opts.final: the question came from a "Did you mean" choice or a follow-up, so it is not checked again.
  // opts.intro: a follow-up's sentence for its split answer.
  async function askLive(question, opts = {}) {
    const e = entry("ask", question);
    if (!opts.final) {
      let c = null;
      try { c = await SQ.backend.clarify(question); } catch (err) { c = null; } // an older backend: just ask
      if (c && c.choices && c.choices.length) { reportClarify(e, c); return; }
    }
    const stop = thinking(e);
    const from = e.children.length - 1; // first child added after the loader
    try {
      const [d] = await Promise.all([SQ.backend.demoAsk(question), new Promise((r) => setTimeout(r, MIN_THINKING_MS))]);
      if (!d || typeof d.is_compliant !== "boolean") {
        throw new Error("The backend sent a reply without a verdict, so it cannot be shown. Check that it is the team backend and up to date.");
      }
      stop();
      last = { question, res: d };
      await policyReady;
      reportLive(e, question, d, opts);
      e.dataset.hid = remember(question, d.not_a_question ? "note" : d.is_compliant ? "allow" : "block", d);
      if (!d.is_compliant && !d.not_a_question) rejectPending = true;
    } catch (err) {
      stop();
      liveError(e, err);
      e.dataset.hid = remember(question, "error", null);
    }
    revealAnswer(e, from);
    renderHistory(e.dataset.hid);
  }

  // Buttons that send a new question through the whole pipeline; data-final skips the vagueness check.
  // A follow-up also carries the sentence that introduces its split answer (data-intro).
  function choiceButtons(choices) {
    return `<div class="choice-row">${choices.map((c) =>
      `<button type="button" class="choice-btn" data-ask="${esc(c.question)}" data-final="1"${c.intro ? ` data-intro="${esc(c.intro)}"` : ""}>${esc(c.label)}</button>`).join("")}</div>`;
  }

  // "The patients having cholera, split by sex, are: 1 – male, 1 – female." The group column is the
  // first that is not all numbers (the first column when all are, as for years), the count the last
  // numeric column besides it.
  function splitSentence(intro, rows) {
    const isNum = (v) => String(v).trim() !== "" && Number.isFinite(Number(String(v).replace(/,/g, "")));
    const columns = [...Array(rows[0].length).keys()];
    const group = columns.find((i) => !rows.every((r) => isNum(r[i]))) ?? 0;
    const count = columns.filter((i) => i !== group && rows.every((r) => isNum(r[i]))).pop();
    if (count === undefined) return null;
    return `${intro} ${rows.map((r) => `${nice(r[count])} – ${readable(r[group], [])}`).join(", ")}.`;
  }

  function reportClarify(e, c) {
    add(e, `<div class="clarify"><span class="suggest-label">${esc(c.prompt || "Did you mean…")}</span>
      ${choiceButtons(c.choices)}
      <span class="dim small-note">Pick one, so the answer counts what you mean instead of a guess.</span></div>`);
  }

  // opts.intro: the question came from a follow-up, and its split answer is read out in one sentence.
  function reportLive(e, question, d, opts = {}) {
    const tm = d.timings_ms || {};
    const meta = fmtMs(tm.total || 0);

    // "hey", "thanks", "what is the weather?": not a question about the data, so no query and no verdict badge.
    if (d.not_a_question) {
      add(e, `<div class="answer is-note"><p class="verdict-small"><span>${meta}</span></p>
        <p class="answer-text">${esc(d.compliance_reason || "This is not a question about the patient data.")}</p></div>`);
      return;
    }

    if (d.is_compliant) {
      add(e, `<p class="verdict-small"><span class="badge badge-sm badge-allow">ALLOWED</span><span>${meta}</span></p>`);
      if (opts.intro && d.result_status === "released" && d.rows && d.rows.length && d.columns) {
        const rows = d.rows.map((r) => d.columns.map((c) => (r[c] == null ? "" : r[c])));
        const sentence = splitSentence(opts.intro, rows);
        if (sentence) add(e, `<p class="answer-text">${esc(sentence)}</p>`);
      }
      if (d.result_status === "withheld" || d.result_status === "released") {
        SQ.result.mount(e, question, d, {policy:SQ.policy, toast, createShare:SQ.backend.createShare});
      } else if (d.result_status === "execution_error") {
        add(e, `<p class="text">The question is allowed, but execution failed: ${esc(d.execution_error || "Database unavailable")}</p>`);
      } else {
        add(e, '<p class="text">No results on this machine. The patient database is not connected here.</p>');
      }
      if (d.follow_ups && d.follow_ups.length) {
        add(e, `<div class="clarify"><span class="suggest-label">Break this down?</span>${choiceButtons(d.follow_ups)}</div>`);
      }
    } else {
      // The backend appends its suggestion to the reason; it is shown separately below.
      let reason = (d.compliance_reason || "").replace(/\s*Try instead:.*$/s, "");
      if (d.suggested_rephrase && reason.endsWith(d.suggested_rephrase)) reason = reason.slice(0, -d.suggested_rephrase.length).trim();
      add(e, `<div class="answer is-blocked">
        <p class="verdict-small"><span class="badge badge-sm badge-block">BLOCKED</span><span>${meta}</span></p>
        <p class="answer-text">${esc(reason).replace(/\?[A-Za-z_]\w*/g, (v) => `<code class="var">${v}</code>`)}</p>
      </div>`);
      const alt = d.suggested_rephrase;
      if (alt && /\?\s*$/.test(alt)) {
        add(e, `<div class="suggest"><span class="suggest-label">You could ask instead</span>
          <button type="button" class="suggest-btn" data-ask="${esc(alt)}"><span>${esc(alt)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg></button>
          <span class="dim small-note">Nothing was changed for you. The request above stays blocked.</span></div>`);
      } else if (alt) {
        add(e, para(`Tip: ${esc(alt)}`));
      }
    }
  }

  function liveSteward(question, d) {
    const tm = d.timings_ms || {};
    const el = document.createElement("details");
    el.className = "steward";
    el.innerHTML = `<summary><span>Details for data stewards</span><span class="steward-hint">checks, timings, tokens, SPARQL</span></summary><div class="steward-body"></div>`;
    const body = el.querySelector(".steward-body");

    add(body, label("Checks"));
    const checks = add(body, '<div class="progress">open to load</div>');

    add(body, label("Time per step"));
    const steps = Object.keys(STEP_TEXT).filter((k) => tm[k] !== undefined);
    add(body, tbl(["Step", "Time", "What happens"], [
      ...steps.map((k) => ({ cells: [`<span class="k">${esc(k.replace("_", " "))}</span>`, fmtMs(tm[k]), esc(STEP_TEXT[k])] })),
      { cells: ['<span class="k">total</span>', fmtMs(tm.total || 0), "End to end. The intent agent runs alongside the other steps, so the steps do not add up."] },
    ], [1]));

    add(body, label("Tokens"));
    const tok = d.tokens_by_step || {};
    const tokRows = Object.keys(tok).map((k) => ({ cells: [`<span class="k">${esc(k.replace("_", " "))}</span>`, fmtInt(tok[k])] }));
    add(body, tbl(["Model call", "Tokens"], tokRows.length ? tokRows : [{ cells: ["none", "0"] }], [1]));
    if (!d.tokens_consumed) add(body, para("0 tokens: the backend runs in stub mode, without a model.", "text dim"));

    const diag = d.diagnostics || {};
    if (Object.keys(diag).length) {
      add(body, label("Retries and warnings"));
      add(body, tbl(["Field", "Value"], Object.keys(diag).filter((k) => k !== "first_try_sparql").map((k) => ({
        cells: [`<span class="k">${esc(k.replace(/_/g, " "))}</span>`, esc(typeof diag[k] === "string" ? diag[k] : JSON.stringify(diag[k]))],
      }))));
      if (diag.first_try_sparql) body.appendChild(windowPanel("first try: ~/query-agent", `<pre class="code">${highlight(diag.first_try_sparql, new Map())}</pre>`, diag.first_try_sparql));
    }

    if (d.sparql_query) {
      body.appendChild(windowPanel(`response: ~/${mode}`, `<pre class="code">${highlight(d.sparql_query, new Map())}</pre>`, d.sparql_query));
    } else {
      add(body, para("The query of a blocked request is never shown to users.", "text dim"));
    }

    let loaded = false;
    el.addEventListener("toggle", async () => {
      if (!el.open || loaded) return;
      loaded = true;
      checks.textContent = "running the checks";
      try {
        const r = await SQ.backend.agentsRun(question);
        const byRule = {};
        (r.violations || []).forEach((v) => { const [id, ...msg] = v.split(":"); (byRule[id.trim()] = byRule[id.trim()] || []).push(msg.join(":").trim()); });
        const flags = r.intent || {};
        const rows = LIVE_RULES.map((rule) => {
          const broken = byRule[rule.id];
          const flagged = rule.flag && flags[rule.flag];
          const result = broken ? '<span class="fail">FAIL</span>' : flagged ? '<span class="fail">FLAGGED</span>' : '<span class="pass">pass</span>';
          const detail = [broken ? broken.join(" ") : "", flagged ? `The request ${FLAG_TEXT[rule.flag]}.` : ""].filter(Boolean).join(" ");
          return { cls: broken || flagged ? "is-fail" : "", cells: [`<span class="k">${rule.id}</span>`, `${esc(rule.name)}${detail ? `<span class="ev">${esc(detail)}</span>` : ""}`, result] };
        });
        rows.push({ cells: ['<span class="k">intent</span>', `What the intent agent read${flags.reason ? `<span class="ev">${esc(flags.reason)}</span>` : ""}`, (r.flags_raised || []).length ? '<span class="fail">FLAGGED</span>' : '<span class="pass">clear</span>'] });
        const html = tbl(["Rule", "Forbidden", "Result"], rows)
          + para(`From the full pipeline result (/agents/run), which runs the question once more${r.backend === "llm" ? " and costs another model call" : ""}. Retries: ${r.retries}.`, "text dim");
        checks.insertAdjacentHTML("afterend", html);
        checks.remove();
      } catch (err) {
        checks.textContent = `Could not load the checks: ${err.message}`;
      }
    });
    return el;
  }

  async function explainLive(e, question) {
    if (!question) return add(e, para('Ask a question first, or type <span class="cmdname">explain</span> followed by a question.'));
    add(e, para(`What the query agent is given for: <span class="muted">${esc(question)}</span>. No model is called.`));
    try {
      const pv = await SQ.backend.preview(question);
      const ctx = pv.context || {};
      add(e, label("Similar examples"));
      add(e, tbl(["Id", "Question"], (ctx.examples || []).map((x) => ({ cells: [`<span class="k">${esc(x.id)}</span>`, esc(x.question)] }))));
      add(e, label("Candidate codes"));
      const codes = ctx.code_candidates || [];
      add(e, codes.length
        ? tbl(["Code", "System", "Labels"], codes.map((c) => ({ cells: [`<span class="k">${esc(c.code)}</span>`, esc(c.system), esc((c.labels || []).join("; "))] })))
        : para("None. Either the question names no medical term, or the terminology index is not built on this machine (assets/ is never committed).", "text dim"));
      const cards = ctx.schema_cards || [];
      if (cards.length) e.appendChild(windowPanel(`schema cards: ${cards.length}`, `<pre class="code plain">${cards.join("\n\n").split("\n").map((l) => `<span class="ln">${esc(l)}</span>`).join("")}</pre>`, cards.join("\n\n")));
    } catch (err) {
      liveError(e, err);
    }
  }

  async function status(e) {
    if (!live) return add(e, para("Mock mode: no backend. Remove ?mock=1 to use the team backend."));
    try {
      const [h, c] = await Promise.all([SQ.backend.health(), SQ.backend.config()]);
      const a = c.agents || {};
      const agentText = (x) => (x ? (x.uses_llm ? `${x.model || "model"} (prompt ${x.prompt_version})` : `stand-in: ${x.implementation}`) : "unknown");
      add(e, tbl(["Setting", "Value"], [
        { cells: ['<span class="k">backend</span>', h.backend === "stub" ? "stub: no model, rough answers, instant" : "llm: real model on the GPU"] },
        { cells: ['<span class="k">intent agent</span>', esc(agentText(a.intent))] },
        { cells: ['<span class="k">query agent</span>', esc(agentText(a.query))] },
        { cells: ['<span class="k">retries</span>', String((c.pipeline || {}).max_retries)] },
        { cells: ['<span class="k">blocked queries returned</span>', (c.pipeline || {}).return_noncompliant_query ? "yes, for the jury" : "no, real-user mode"] },
        { cells: ['<span class="k">examples in bank</span>', String((c.context || {}).examples_in_bank)] },
        { cells: ['<span class="k">terminology codes</span>', String((c.context || {}).terminology_entries || 0)] },
      ]));
      if (h.backend === "llm") {
        const p = add(e, '<div class="progress">checking the model server</div>');
        const k = await SQ.backend.check();
        p.remove();
        add(e, tbl(["Agent", "Model server"], Object.keys(k).map((n) => ({ cells: [`<span class="k">${n}</span>`, esc(k[n].status + (k[n].error ? `: ${k[n].error}` : ""))] }))));
      }
    } catch (err) {
      liveError(e, err);
    }
  }

  async function benchLive(e) {
    try {
      const list = await SQ.backend.reports();
      const reports = list.reports || [];
      if (!reports.length) {
        add(e, para(`No eval reports on this machine yet (${esc(list.folder || "eval/reports")}). They are created on the run machine by eval/run_eval.py; copy them into that folder to see them here.`));
        return;
      }
      const latest = reports.find((r) => r.kind === "pipeline") || reports[0];
      const r = await SQ.backend.report(latest.name);
      const s = r.summary || {};
      add(e, para(`Latest ${esc(latest.kind)} report: ${esc(latest.name)}, ${esc(latest.created)}.`));
      add(e, tbl(["Measure", "Value"], [
        { cells: ['<span class="k">questions</span>', String(s.questions)] },
        { cells: ['<span class="k">verdicts correct</span>', s.verdict_accuracy != null ? fmtPct(s.verdict_accuracy) : "n/a"] },
        { cells: ['<span class="k">median time</span>', s.latency_ms_p50 != null ? fmtMs(s.latency_ms_p50) : "n/a"] },
        { cells: ['<span class="k">95th percentile time</span>', s.latency_ms_p95 != null ? fmtMs(s.latency_ms_p95) : "n/a"] },
        { cells: ['<span class="k">tokens per request</span>', s.avg_tokens != null ? fmtInt(s.avg_tokens) : "n/a"] },
      ]));
      const splits = s.by_split || {};
      if (Object.keys(splits).length) {
        add(e, label("By split"));
        add(e, tbl(["Split", "Questions", "Correct", "Forbidden marked allowed", "Allowed blocked"], Object.keys(splits).map((k) => ({
          cells: [`<span class="k">${esc(k)}</span>`, String(splits[k].questions), fmtPct(splits[k].verdict_accuracy), `${splits[k].forbidden_marked_compliant} of ${splits[k].forbidden}`, `${splits[k].allowed_blocked} of ${splits[k].allowed}`],
        })), [1, 2]));
      }
      add(e, label("All reports"));
      add(e, tbl(["Report", "Kind", "Created"], reports.map((x) => ({ cells: [`<span class="k">${esc(x.name)}</span>`, esc(x.kind), esc(x.created)] }))));
    } catch (err) {
      liveError(e, err);
    }
  }

  // ---------- input ----------

  async function handle(raw, opts = {}) {
    const text = raw.trim();
    if (!text) return;
    history.push(text);
    hIndex = history.length;
    const [word, ...rest] = text.split(/\s+/);
    const cmd = COMMANDS[word.toLowerCase()];
    hidePrecheck();
    clearRejected();
    busy = true;
    input.disabled = true;
    form.classList.add("is-busy");
    form.classList.remove("has-text");
    try {
      if (cmd) {
        const e = entry("cmd", text);
        await cmd.fn(e, rest.join(" "));
      } else {
        await ask(raw, opts);
      }
    } finally {
      busy = false;
      input.disabled = false;
      form.classList.remove("is-busy");
      form.classList.toggle("has-text", input.value.trim().length > 0);
      if (rejectPending) { rejectPending = false; requestAnimationFrame(markRejected); }
      input.focus();
      const lastEntry = out.lastElementChild;
      if (lastEntry) lastEntry.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) return;
    const v = input.value;
    input.value = "";
    handle(v);
  });

  // ---------- hint while typing, and the refused state ----------

  const precheckEl = document.getElementById("precheck");
  const PLACEHOLDER = input.getAttribute("placeholder");
  const PRECHECK_PAUSE_MS = 600;
  let precheckTimer = null;

  function hidePrecheck() {
    clearTimeout(precheckTimer);
    precheckEl.hidden = true;
    precheckEl.innerHTML = "";
  }

  function showPrecheck() {
    const text = input.value;
    const word = text.trim().split(/\s+/)[0] || "";
    const hit = !busy && !COMMANDS[word.toLowerCase()] && SQ.precheck ? SQ.precheck(text) : null;
    if (!hit) return hidePrecheck();
    precheckEl.innerHTML = `<span><span class="precheck-lead">This may be refused:</span> ${esc(hit.reason)}.</span>
      ${hit.rephrase
        ? `<button type="button" class="precheck-try" data-rephrase="${esc(hit.rephrase)}">Try: ${esc(hit.rephrase)}</button>`
        : `<span class="precheck-note">${esc(hit.hint)}</span>`}
      <span class="precheck-note">You can still send it to get the real verdict.</span>`;
    precheckEl.hidden = false;
  }

  precheckEl.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-rephrase]");
    if (!b) return;
    input.value = b.dataset.rephrase;
    input.dispatchEvent(new Event("input"));
    input.focus();
  });

  // A refused question: the field shakes once and stays red until the next edit.
  // Runs once the field is enabled and focused again, so the shake is seen.
  function markRejected() {
    form.classList.add("is-rejected");
    input.classList.remove("shake");
    void input.offsetWidth; // restart the animation
    input.classList.add("shake");
    input.setAttribute("placeholder", "That question was refused. Rephrase it, or use the suggestion above.");
  }
  input.addEventListener("animationend", (ev) => { if (ev.animationName === "shake") input.classList.remove("shake"); });
  function clearRejected() {
    if (!form.classList.contains("is-rejected")) return;
    form.classList.remove("is-rejected");
    input.setAttribute("placeholder", PLACEHOLDER);
  }

  input.addEventListener("input", () => {
    form.classList.toggle("has-text", input.value.trim().length > 0);
    clearRejected();
    schedulePrecheck();
  });

  // Also used when ↑/↓ recall a question, which does not fire an input event.
  function schedulePrecheck() {
    clearTimeout(precheckTimer);
    if (!input.value.trim()) return hidePrecheck();
    precheckTimer = setTimeout(showPrecheck, PRECHECK_PAUSE_MS);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && history.length) {
      e.preventDefault();
      hIndex = Math.max(0, hIndex - 1);
      input.value = history[hIndex];
      form.classList.toggle("has-text", input.value.trim().length > 0);
      schedulePrecheck();
    } else if (e.key === "ArrowDown" && history.length) {
      e.preventDefault();
      hIndex = Math.min(history.length, hIndex + 1);
      input.value = history[hIndex] || "";
      form.classList.toggle("has-text", input.value.trim().length > 0);
      schedulePrecheck();
    } else if (e.key === "Tab") {
      const v = input.value.trim().toLowerCase();
      if (!v || v.includes(" ")) return;
      const match = Object.keys(COMMANDS).filter((c) => c.startsWith(v));
      if (match.length === 1) { e.preventDefault(); input.value = match[0] + " "; }
      else if (match.length > 1) { e.preventDefault(); toast(match.join("   ")); }
    } else if (e.key === "l" && e.ctrlKey) {
      e.preventDefault();
      clearView();
    }
  });

  // Clickable example and attack rows, and suggested questions
  out.addEventListener("click", (e) => {
    const sug = e.target.closest("[data-ask]");
    if (sug && !busy) return handle(sug.dataset.ask, { final: sug.dataset.final === "1", intro: sug.dataset.intro });
    const row = e.target.closest("tr[data-try]");
    if (row && !busy) handle(`try ${row.dataset.try}`);
  });
  out.addEventListener("keydown", (e) => {
    const row = e.target.closest("tr[data-try]");
    if (row && (e.key === "Enter" || e.key === " ") && !busy) { e.preventDefault(); handle(`try ${row.dataset.try}`); }
  });


  // ---------- previous questions (saved in this browser) ----------

  const HIST_KEY = "qg-console-history";
  const HIST_MAX = 50;
  const histEl = document.getElementById("hist");
  const histEmpty = document.getElementById("hist-empty");

  function loadHistory() {
    try {
      const items = JSON.parse(localStorage.getItem(HIST_KEY) || "[]");
      for (const entry of items) stripSavedQueries(entry.res);
      saveHistory(items);
      return items;
    } catch (e) { return []; }
  }
  function stripSavedQueries(res) {
    if (!res) return;
    delete res.sparql_query;
    if (res.reproducibility) delete res.reproducibility.sparql_query;
    res.diagnostics = {};
  }
  function saveHistory(items) {
    try { localStorage.setItem(HIST_KEY, JSON.stringify(items.slice(0, HIST_MAX))); } catch (e) { /* full or private mode */ }
  }
  // Keeps the answer so a past question can be shown again without asking the backend.
  function remember(question, verdict, res) {
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    let kept = null;
    if (res && live) {
      kept = Object.assign({}, res, { rows: res.result_status === "released" ? (res.rows || []).slice(0, 200) : null });
      if (kept.reproducibility) kept.reproducibility = Object.assign({}, kept.reproducibility);
      stripSavedQueries(kept);
    }
    saveHistory([{ id, question, verdict, at: Date.now(), res: kept }, ...loadHistory()]);
    return id;
  }

  function when(ts) {
    const mins = Math.round((Date.now() - ts) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const d = new Date(ts);
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return new Date().toDateString() === d.toDateString() ? `today ${time}` : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
  }

  function renderHistory(activeId) {
    const items = loadHistory();
    histEmpty.hidden = items.length > 0;
    histEl.innerHTML = items.map((h) => `
      <li class="hist-item"><button type="button" class="hist-btn${h.id === activeId ? " is-active" : ""}" data-hid="${esc(h.id)}" title="${esc(h.question)}">
        <span class="hist-dot ${h.verdict === "allow" ? "is-allow" : h.verdict === "block" ? "is-block" : ""}" aria-hidden="true"></span>
        <span class="hist-q">${esc(h.question)}</span>
        <span class="hist-time">${esc(when(h.at))}<span class="visually-hidden">, ${h.verdict === "allow" ? "allowed" : h.verdict === "block" ? "blocked" : h.verdict === "note" ? "not a question" : "did not finish"}</span></span>
      </button><button type="button" class="hist-del" data-del="${esc(h.id)}" aria-label="Delete this question: ${esc(h.question)}" title="Delete">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></li>`).join("");
  }

  async function showFromHistory(id) {
    await policyReady;
    const h = loadHistory().find((x) => x.id === id);
    if (!h) return;
    closeSidebar();
    if (SQ.policy.state !== "available" || (h.res?.result_status !== "withheld" && h.res?.is_compliant && !SQ.result.policyMatches(h.res, SQ.policy.config))) {
      const e = entry("ask", h.question);
      add(e, `<p>Privacy policy unavailable or changed. Re-ask before displaying cached numbers.</p><button type="button" data-ask="${esc(h.question)}">Ask again</button>`);
      return;
    }
    const onScreen = out.querySelector(`[data-hid="${CSS.escape(id)}"]`);
    if (onScreen) {
      onScreen.scrollIntoView({ block: "start", behavior: "smooth" });
      onScreen.classList.remove("is-flash"); void onScreen.offsetWidth; onScreen.classList.add("is-flash");
      renderHistory(id);
      return;
    }
    if (!h.res || !live) {
      const e = entry("ask", h.question);
      add(e, `<p>No saved result is available.</p><button type="button" data-ask="${esc(h.question)}">Ask again</button>`);
      return;
    }
    if (!h.res.result_status && h.res.executed) {
      h.res.result_status = "released";
      h.res.legacy_incomplete = true;
      h.res.column_metadata = (h.res.columns || []).map((name) => ({name, role:"unknown", kind:"unknown"}));
    }
    const e = entry("ask", h.question);
    e.dataset.hid = id;
    add(e, `<p class="from-history">From your history, asked ${esc(when(h.at))}. <button type="button" class="link-btn" data-ask="${esc(h.question)}">Ask again</button></p>`);
    last = { question: h.question, res: h.res };
    reportLive(e, h.question, h.res);
    e.scrollIntoView({ block: "start", behavior: "smooth" });
    renderHistory(id);
  }

  histEl.addEventListener("click", (ev) => {
    const del = ev.target.closest("[data-del]");
    if (del) return deleteFromHistory(del.dataset.del);
    const b = ev.target.closest("[data-hid]");
    if (b) showFromHistory(b.dataset.hid);
  });

  // Removes one question from the sidebar (and this browser). An answer already on screen stays.
  function deleteFromHistory(id) {
    const items = loadHistory();
    const index = items.findIndex((x) => x.id === id);
    if (index === -1) return;
    saveHistory(items.filter((x) => x.id !== id));
    const active = histEl.querySelector(".hist-btn.is-active");
    renderHistory(active && active.dataset.hid !== id ? active.dataset.hid : undefined);
    // Keep keyboard focus in the list: the next item's delete button, or the previous one.
    const dels = histEl.querySelectorAll("[data-del]");
    const next = dels[Math.min(index, dels.length - 1)];
    (next || document.getElementById("new-q")).focus();
    toast("Question deleted");
  }

  document.getElementById("clear-hist").addEventListener("click", () => {
    if (!loadHistory().length) return;
    if (!window.confirm("Remove all previous questions from this browser?")) return;
    saveHistory([]);
    renderHistory();
  });

  // ---------- new question, empty state, sidebar on phones ----------

  function clearView() {
    SQ.result.disposeAll(out);
    out.innerHTML = "";
    document.body.classList.add("is-empty");
    renderHistory();
    input.focus();
  }
  document.getElementById("new-q").addEventListener("click", () => { clearView(); closeSidebar(); });

  // Top bar: every view stays inside the console.
  document.getElementById("home-logo").addEventListener("click", (ev) => { ev.preventDefault(); clearView(); closeSidebar(); });
  document.querySelector(".top-links").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-view]");
    if (!b || busy) return;
    closeSidebar();
    if (b.dataset.view === "home") clearView();
    else handle(b.dataset.view);
  });

  const sideToggle = document.getElementById("side-toggle");
  const scrim = document.getElementById("scrim");
  function closeSidebar() {
    document.body.classList.remove("side-open");
    sideToggle.setAttribute("aria-expanded", "false");
    scrim.hidden = true;
  }
  sideToggle.addEventListener("click", () => {
    const open = !document.body.classList.contains("side-open");
    document.body.classList.toggle("side-open", open);
    sideToggle.setAttribute("aria-expanded", String(open));
    scrim.hidden = !open;
  });
  scrim.addEventListener("click", closeSidebar);

  // Starter questions in the empty state: three allowed, one tricky but allowed, two attacks.
  const STARTERS = [
    ...F.FIXTURES.filter((f) => ["c-diabetes", "c-cholera-sex", "c-preterm", "la-born-abroad"].includes(f.id)).map((f) => ({ f, tag: "cohort question" })),
    ...F.FIXTURES.filter((f) => ["a-which-ones", "a-patient-0042"].includes(f.id)).map((f) => ({ f, tag: "attack: should be blocked" })),
  ];
  const starterGrid = document.getElementById("starter-grid");
  starterGrid.innerHTML = STARTERS.map(({ f, tag }) => `
    <button type="button" class="starter" data-ask="${esc(f.text)}">
      <span class="starter-tag${tag.startsWith("attack") ? " is-attack" : ""}">${esc(tag)}</span>
      <span>${esc(f.text)}</span>
    </button>`).join("");
  starterGrid.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-ask]");
    if (b && !busy) handle(b.dataset.ask);
  });

  document.getElementById("snapshot-import").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    if (SQ.policy.state !== "available") {
      ev.target.value = "";
      toast("Privacy policy unavailable");
      return;
    }
    try {
      if (file.size > 2 * 1024 * 1024) throw Error("Snapshot exceeds 2 MB.");
      const saved = SQ.result.validateSnapshot(await file.text());
      if (SQ.policy.state !== "available") {
        ev.target.value = "";
        toast("Privacy policy unavailable");
        return;
      }
      const e = entry("ask", saved.question);
      SQ.result.mount(e, saved.question, saved, {policy:SQ.policy,toast});
      add(e, `<button type="button" data-ask="${esc(saved.question)}">Re-ask for a verified result</button>`);
    } catch (err) { toast(`Cannot import snapshot: ${err.message}`); }
    ev.target.value = "";
  });

  // Authored visual test cases are browser-only synthetic data, never API requests.
  const visualExamples = [
    {label:"Preview example: patients by sex (synthetic numbers)", title:"Synthetic bar-chart example — not patient data",
      columns:["sex", "patients"], roles:["category", "patient_count"],
      rows:[["Female", "48"], ["Male", "36"], ["Other / unknown", "12"]]},
    {label:"Preview example: two cohorts by age group (synthetic numbers)", title:"Synthetic comparison example — not patient data",
      columns:["age_group", "group_A", "group_B"], roles:["category", "patient_count", "patient_count"],
      rows:[["18–39", "24", "36"], ["40–64", "41", "52"], ["65+", "18", "29"]]},
    {label:"Preview example: two cohorts by sex (long-form synthetic numbers)", title:"Synthetic long-form comparison — not patient data",
      columns:["sex", "cohort", "patients"], roles:["category", "category", "patient_count"],
      rows:[["Female", "Group A", "32"], ["Female", "Group B", "48"], ["Male", "Group A", "24"], ["Male", "Group B", "36"], ["Other / unknown", "Group A", "12"]]},
  ];
  for (const example of visualExamples) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "starter"; button.textContent = example.label;
    button.onclick = async () => {
      await policyReady;
      if (SQ.policy.state !== "available") return toast("Privacy policy unavailable");
      const e = entry("cmd", example.title);
      const d = {is_compliant:true, executed:false, result_status:"released", columns:example.columns, rows:example.rows,
        column_metadata:example.columns.map((name,i) => ({name,role:example.roles[i],kind:example.roles[i] === "category" ? "group_key" : "distinct_patient_count"})),
        suppression:null, row_limit:200,truncated:false, imported:true, synthetic_preview:true};
      // Import boundary: synthetic previews have no exports or live verification claims.
      SQ.result.mount(e, example.title, d, {policy:SQ.policy,toast});
    };
    starterGrid.appendChild(button);
  }

  // ---------- start ----------

  async function openSharedResult(id) {
    await policyReady;
    if (SQ.policy.state !== "available") {
      toast("Privacy policy unavailable. Retry the policy and reopen this link.");
      return;
    }
    try {
      const shared = await SQ.backend.sharedResult(id);
      if (SQ.policy.state !== "available") throw Error("Privacy policy unavailable");
      const saved = SQ.result.validateSnapshot(JSON.stringify(shared.snapshot));
      saved.shared = true;
      saved.imported = false; // A server-authorized release, not a user-supplied file.
      const e = entry("ask", saved.question);
      add(e, `<p class="from-history">Shared snapshot — not a fresh database run. Captured ${esc(saved.captured_at)}. Expires ${esc(shared.expires_at)}.</p>`);
      SQ.result.mount(e, saved.question, saved, {policy:SQ.policy,toast});
      add(e, `<button type="button" data-ask="${esc(saved.question)}">Ask again for current results</button>`);
    } catch (error) {
      const e = entry("cmd", "Shared result unavailable");
      add(e, `<p role="status">${esc(error.message)}</p>`);
    }
  }

  renderHistory();
  const sharedId = params.get("share");
  const q = params.get("q");
  if (sharedId) openSharedResult(sharedId);
  else if (q) handle(q);
  else input.focus();
})();
