// Shared UI: header, theme, present mode, SPARQL highlighter and the result
// components reused by the Studio and the Attack gallery.
window.SQ = window.SQ || {};

(function () {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmtInt = (n) => Number(n).toLocaleString("en-US");
  const fmtMs = (n) => (n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(2)} s`);
  const fmtPct = (x, d = 1) => `${(x * 100).toFixed(d)}%`;

  const ICON = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg>',
    cross: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    dash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M6 12h12"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>',
    // A gate: two posts and a bar. Neutral mark, not a national emblem.
    mark: '<svg class="wordmark-mark" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="3" width="4" height="18" fill="currentColor"/><rect x="18" y="3" width="4" height="18" fill="currentColor"/><rect x="6" y="10" width="12" height="4" fill="currentColor"/></svg>',
    attack: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M8 1.5l6.5 12h-13z" stroke-linejoin="round"/><path d="M8 6v3.5M8 11.6v.1" stroke-linecap="round"/></svg>',
    // Three bars: a question about totals.
    ask: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="8" width="3" height="6"/><rect x="6.5" y="4" width="3" height="10"/><rect x="11" y="6" width="3" height="8"/></svg>',
  };

  const STAGES = {
    retrieve: { label: "Retrieve context", color: "var(--stage-1)" },
    template: { label: "Fast-path template", color: "var(--stage-3)" },
    llm: { label: "Model", color: "var(--stage-2)" },
    validate: { label: "Validate", color: "var(--stage-5)" },
    compliance: { label: "Rules", color: "var(--stage-4)" },
  };

  // ---------- Header, theme, present mode ----------

  const PAGES = [
    { href: "index.html", key: "studio", label: "Studio" },
    { href: "attacks.html", key: "attacks", label: "Attacks" },
    { href: "bench.html", key: "bench", label: "Benchmark" },
    { href: "how.html", key: "how", label: "How it works" },
  ];

  function storedTheme() {
    try { return localStorage.getItem("sq-theme"); } catch (e) { return null; }
  }
  function saveTheme(v) {
    try { localStorage.setItem("sq-theme", v); } catch (e) { /* private mode */ }
  }
  function currentTheme() {
    const set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme(v) {
    document.documentElement.setAttribute("data-theme", v);
    const btn = document.querySelector("[data-theme-toggle]");
    if (btn) {
      btn.innerHTML = v === "dark" ? ICON.sun : ICON.moon;
      btn.setAttribute("aria-label", v === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
  }

  function mountHeader(active) {
    const host = document.querySelector("[data-header]");
    if (!host) return;
    const live = SQ.config.API_MODE === "live";
    host.innerHTML = `
      <div class="wrap">
        <a class="wordmark" href="index.html">${ICON.mark}<span>${esc(SQ.config.APP_NAME)}</span></a>
        <nav class="nav" aria-label="Main">
          ${PAGES.map((p) => `<a href="${p.href}"${p.key === active ? ' aria-current="page"' : ""}>${p.label}</a>`).join("")}
        </nav>
        <div class="header-tools">
          <span class="pill${live ? " is-live" : ""}" title="${live ? "Connected to the backend" : "Responses come from fixtures.js"}"><span class="pill-dot"></span>${live ? "Live backend" : "Mock data"}</span>
          <button class="icon-btn" type="button" data-theme-toggle></button>
        </div>
      </div>`;
    const params = new URLSearchParams(location.search);
    const forced = params.get("theme");
    applyTheme(forced === "light" || forced === "dark" ? forced : storedTheme() || currentTheme());
    host.querySelector("[data-theme-toggle]").addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      saveTheme(next);
    });
    if (params.get("present") === "1") document.body.classList.add("present");
  }

  // ---------- SPARQL highlighter ----------

  const KEYWORDS = new Set(("SELECT WHERE GRAPH OPTIONAL FILTER BIND AS GROUP BY ORDER DESC ASC DISTINCT COUNT SUM AVG MIN MAX SAMPLE " +
    "GROUP_CONCAT PREFIX DEFINE OPTION VALUES UNION EXISTS NOT IF BOUND COALESCE STR STRSTARTS STRAFTER REPLACE REGEX " +
    "ROUND LANG LANGMATCHES CONSTRUCT DELETE INSERT DROP CLEAR LIMIT OFFSET HAVING IN SEPARATOR").split(" "));
  const TOKEN = /(<[^<>\s]*>)|("(?:[^"\\]|\\.)*")|([?$][A-Za-z_]\w*)|(#.*$)|([A-Za-z][\w-]*:[\w.-]*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]+)/g;

  function highlightLine(line) {
    let out = "";
    let last = 0;
    line.replace(TOKEN, (m, iri, str, v, com, pname, num, word, idx) => {
      out += esc(line.slice(last, idx));
      if (iri) out += `<span class="tok-iri">${esc(iri)}</span>`;
      else if (str) out += `<span class="tok-str">${esc(str)}</span>`;
      else if (v) out += `<span class="tok-var">${esc(v)}</span>`;
      else if (com) out += `<span class="tok-com">${esc(com)}</span>`;
      else if (pname) out += `<span class="tok-iri">${esc(pname)}</span>`;
      else if (num) out += `<span class="tok-num">${esc(num)}</span>`;
      else if (word && KEYWORDS.has(word.toUpperCase())) out += `<span class="tok-kw">${esc(word)}</span>`;
      else out += esc(m);
      last = idx + m.length;
      return m;
    });
    return out + esc(line.slice(last));
  }

  // marks: Map(lineNumber -> "evidence" | "note")
  function highlight(sparql, marks) {
    return sparql.split("\n").map((line, i) => {
      const mark = marks && marks.get(i + 1);
      const cls = mark === "evidence" ? " is-evidence" : mark === "note" ? " is-note" : "";
      return `<span class="ln${cls}">${highlightLine(line) || " "}</span>`;
    }).join("");
  }

  function evidenceMarks(rules) {
    const marks = new Map();
    (rules || []).forEach((r) => { if (!r.passed && r.evidence && r.evidence.line) marks.set(r.evidence.line, "evidence"); });
    return marks;
  }

  // ---------- Components ----------

  function renderCode(sparql, marks, label) {
    const lines = sparql.split("\n").length;
    return `
      <div class="code-panel">
        <div class="code-bar"><span>${esc(label || "Generated SPARQL")}, ${lines} lines</span>
          <button class="btn btn-quiet btn-small" type="button" data-copy>Copy query</button></div>
        <pre class="code" tabindex="0" aria-label="SPARQL query"><code>${highlight(sparql, marks)}</code></pre>
      </div>`;
  }

  function firstFailure(rules) {
    return (rules || []).find((r) => !r.passed);
  }

  function renderVerdict(res, question) {
    const ok = res.is_compliant;
    const t = res.trace || {};
    const failed = (t.rules || []).filter((r) => !r.passed);
    const ruleCount = (t.rules || []).filter((r) => r.id !== "IG").length || 5;
    const reason = ok
      ? `Passes all ${ruleCount} rules and the intent check. The query only returns totals across all patients.`
      : res.compliance_reason;
    const path = t.path === "fast" ? "Fast path, no model" : `Model, ${t.attempts || 1} ${t.attempts === 1 ? "attempt" : "attempts"}`;
    return `
      <section class="verdict ${ok ? "is-allow" : "is-block"} is-entering" aria-live="polite">
        <p class="verdict-word">${ok ? ICON.check : ICON.cross}<span>${ok ? "Allowed" : "Blocked"}</span></p>
        <p class="verdict-reason">${esc(reason)}</p>
        ${question ? `<p class="verdict-question">You asked: ${esc(question)}</p>` : ""}
        <dl class="verdict-facts">
          ${!ok ? `<div class="fact"><dt>Caught by</dt><dd>${esc(failed.map((r) => r.id === "IG" ? "Intent guard" : r.id).join(", "))}</dd></div>` : ""}
          <div class="fact"><dt>Path</dt><dd>${esc(path)}</dd></div>
          <div class="fact"><dt>Time</dt><dd>${fmtMs((t.timings_ms || {}).total || 0)}</dd></div>
          <div class="fact"><dt>Tokens</dt><dd>${fmtInt(res.tokens_consumed)}</dd></div>
        </dl>
      </section>`;
  }

  function renderRules(rules) {
    return `
      <ul class="rules">
        ${rules.map((r) => `
          <li class="rule ${r.passed ? "is-pass" : "is-fail"}">
            <span class="rule-id">${r.id === "IG" ? "Intent" : r.id}</span>
            <span class="rule-name">${esc(r.name)}</span>
            <p class="rule-msg">${esc(r.message)}${r.evidence && r.evidence.text ? `<code class="rule-evidence">${r.evidence.line ? `line ${r.evidence.line}: ` : ""}${esc(r.evidence.text)}</code>` : ""}</p>
            <span class="rule-status">${r.passed ? ICON.check + "Pass" : ICON.cross + "Fail"}</span>
          </li>`).join("")}
      </ul>`;
  }

  function renderTiming(timings) {
    const keys = Object.keys(STAGES).filter((k) => timings[k] > 0);
    const total = timings.total || keys.reduce((a, k) => a + timings[k], 0);
    return `
      <div class="timing">
        <div class="section-title"><h2>Where the time went</h2><span class="small muted">${fmtMs(total)} in total</span></div>
        <div class="timing-bar" role="img" aria-label="${keys.map((k) => `${STAGES[k].label} ${fmtMs(timings[k])}`).join(", ")}">
          ${keys.map((k) => `<span style="flex:${timings[k]};background:${STAGES[k].color}" title="${STAGES[k].label}: ${fmtMs(timings[k])}"></span>`).join("")}
        </div>
        <ul class="timing-legend">
          ${keys.map((k) => `<li><span class="swatch" style="background:${STAGES[k].color}"></span>${STAGES[k].label} ${fmtMs(timings[k])}</li>`).join("")}
        </ul>
      </div>`;
  }

  function renderTable(columns, rows) {
    const isNum = (v) => /^[\d,.\s%-]+$/.test(String(v)) && String(v).trim() !== "";
    return `<table class="data"><thead><tr>${columns.map((c, i) => `<th${rows.length && isNum(rows[0][i]) ? ' class="num"' : ""}>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td${isNum(v) ? ' class="num"' : ""}>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1800);
  }

  // Copy buttons anywhere on the page.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const pre = btn.closest(".code-panel").querySelector("pre");
    const text = Array.from(pre.querySelectorAll(".ln")).map((l) => l.textContent).join("\n");
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
      .then(() => toast("Query copied"))
      .catch(() => toast("Copy is not available here. Select the text instead."));
  });

  SQ.ui = {
    esc, fmtInt, fmtMs, fmtPct, ICON, STAGES,
    mountHeader, highlight, evidenceMarks, firstFailure,
    renderCode, renderVerdict, renderRules, renderTiming, renderTable, toast,
  };
})();
