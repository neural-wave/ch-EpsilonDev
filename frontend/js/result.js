// Shared result presentation. Metadata, never numeric-looking labels, selects measures.
(function (root) {
  const esc = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const measure = (m) => ['patient_count', 'numeric_measure'].includes(m?.role);
  const number = (v) => v !== null && v !== undefined && String(v).trim() !== '' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(String(v).trim()) && Number.isFinite(Number(v)) ? Number(v) : null;
  const normalize = (d) => (d.rows || []).map((r) => d.columns.map((c, i) => (Array.isArray(r) ? r[i] : r[c]) ?? ''));
  function comparisonRequested(question) {
    return /\bcompar(?:e|es|ed|ing|ison|isons)\b|\bversus\b|\bvs\.?\s|\bwith\b[\s\S]*\bwithout\b/i.test(question);
  }
  function visual(d) {
    const rows = normalize(d), m = d.column_metadata || [], n = d.columns.length;
    const valid = (i) => rows.every((r) => r[i] === '' || number(r[i]) !== null);
    if (!rows.length) return {kind:'table'};
    if (m.length !== n || m.some((item,i) => item.name !== d.columns[i])) return {kind:'table'};
    if (n === 1 && rows.length === 1 && measure(m[0]) && number(rows[0][0]) !== null) return {kind:'number', value:rows[0][0]};
    const categories = m.flatMap((item,i) => item.role === 'category' ? [i] : []);
    const measures = m.flatMap((item,i) => measure(item) ? [i] : []);
    if (categories.length === 1 && measures.length >= 1 && measures.length <= 2 && categories.length + measures.length === n && measures.every(valid)) {
      const category = categories[0];
      if (new Set(rows.map((r) => String(r[category]))).size === rows.length) {
        return {kind:measures.length === 1 ? 'bar':'grouped', labels:rows.map((r) => r[category]),
          series:measures.map((i) => d.columns[i]), values:rows.map((r) => measures.map((i) => r[i]))};
      }
    }
    if (n === 3 && categories.length === 2 && measures.length === 1 && valid(measures[0])) {
      let [category, group] = categories;
      const distinct = (i) => [...new Set(rows.map((r) => r[i]))];
      // Identify the two-cohort axis structurally, without relying on medical terms or aliases.
      if (distinct(category).length === 2 && distinct(group).length > 2) [category, group] = [group, category];
      const groups = distinct(group), labels = distinct(category);
      if (groups.length === 2 && rows.every((r) => r[category] !== '' && r[group] !== '') &&
          new Set(rows.map((r) => JSON.stringify([r[category],r[group]]))).size === rows.length) {
        return {kind:'grouped', labels, series:groups, values:labels.map((label) => groups.map((cohort) => rows.find((r) => r[category] === label && r[group] === cohort)?.[measures[0]] ?? ''))};
      }
    }
    return {kind:'table'};
  }
  function policyMatches(d, policy) {
    if (!policy) return false;
    if (policy.demo_min_group_size === null) return true;
    const s = d.suppression;
    return s?.enabled === true && s.status === 'released' && s.threshold === policy.demo_min_group_size && s.policy_version === policy.policy_version;
  }
  function safeCell(value, numeric) {
    const s = String(value ?? '');
    return numeric && number(value) !== null ? s : /^[\s\u0000-\u001f]*[=+\-@\t\r\n]/.test(s) || /^[\t\r\n]/.test(s) ? "'" + s : s;
  }
  function delimited(d, separator = ',') {
    const quote = (v) => /["\r\n,\t]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    const lines = [d.columns.map((c) => safeCell(c, false)), ...normalize(d).map((r) => r.map((v, i) => safeCell(v, measure(d.column_metadata?.[i]))))];
    return lines.map((r) => r.map(quote).join(separator)).join('\r\n');
  }
  function validTimestamp(value) {
    if (typeof value !== 'string') return false;
    const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
    if (!parts || !Number.isFinite(Date.parse(value))) return false;
    const [, y, mo, day, hour, minute, second, zone] = parts;
    const year = Number(y), month = Number(mo);
    const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31,30,31,30,31,31,30,31,30,31];
    return month >= 1 && month <= 12 && Number(day) >= 1 && Number(day) <= days[month - 1] &&
      Number(hour) <= 23 && Number(minute) <= 59 && Number(second) <= 59 &&
      (zone === 'Z' || Number(zone.slice(1,3)) <= 23 && Number(zone.slice(4)) <= 59);
  }
  function syntheticRecord(question) {
    return {source:'synthetic', question, executed_at:null,
      dataset:{name:'Synthetic browser example',version:null,description:'Demonstration only; no database was queried.'}};
  }
  function validReproducibility(record) {
    return record && ['database','synthetic'].includes(record.source) &&
      typeof record.question === 'string' && record.dataset &&
      ['name','version','description'].every((key) => Object.hasOwn(record.dataset,key) &&
        (record.dataset[key] === null || typeof record.dataset[key] === 'string')) &&
      (record.source === 'synthetic' ? record.executed_at === null :
        validTimestamp(record.executed_at) && /(?:Z|\+00:00)$/.test(record.executed_at));
  }
  function reproducibilityText(record, result) {
    const value = (key) => record.dataset[key] ?? 'Not configured';
    const synthetic = record.source === 'synthetic';
    const cells = result ? normalize(result) : [];
    let summary = !result ? 'Not recorded' : !cells.length ? 'No rows' :
      result.columns.length === 1 && cells.length === 1 ? String(cells[0][0] === '' ? 'No value' : cells[0][0]) :
      '\n' + delimited(result, '\t');
    if (synthetic) summary += result?.columns.length === 1 && cells.length === 1 ? ' (illustrative number)' : ' (illustrative result)';
    if (result?.truncated) summary += '\nResults truncated at ' + result.row_limit + ' rows.';
    const heading = synthetic ? 'Preview mode (no database)' : 'Database execution';
    return `${heading}\n\n- Source: ${synthetic ? 'Synthetic preview, not for citation' : 'Database execution'}\n` +
      `- Question: ${record.question}\n- Result: ${summary}\n` +
      `- Executed at: ${synthetic ? 'Not applicable' : record.executed_at + ' (UTC)'}\n` +
      `- Dataset: ${synthetic ? 'None, demonstration only' : value('name')}` +
      (synthetic ? '' : `\n- Dataset version: ${value('version')}\n- Dataset description: ${value('description')}` +
        (record.dataset.version === null ? '\nDataset version unavailable; reproducibility record is incomplete.' : ''));
  }
  const fields = ['format_version','question','columns','rows','column_metadata','captured_at','truncated','row_limit','suppression'];
  function snapshot(question, d) {
    return {format_version:1, question, columns:d.columns, rows:normalize(d), column_metadata:d.column_metadata,
      captured_at:new Date().toISOString(), truncated:d.truncated === true, row_limit:200, suppression:d.suppression ?? null};
  }
  function validateSnapshot(text) {
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024) throw Error('Snapshot exceeds 2 MB.');
    const s = JSON.parse(text);
    const exact = (x, keys) => x && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).length === keys.length && keys.every((k) => Object.hasOwn(x, k));
    const cell = (v) => v === null || typeof v === 'string' || typeof v === 'number' && Number.isFinite(v);
    if (!exact(s, fields) || s.format_version !== 1 || typeof s.question !== 'string' || !validTimestamp(s.captured_at) || typeof s.truncated !== 'boolean' || s.row_limit !== 200 || !Array.isArray(s.columns) || !s.columns.length || s.columns.length > 100 || !s.columns.every((c) => typeof c === 'string') || new Set(s.columns).size !== s.columns.length || !Array.isArray(s.rows) || s.rows.length > 200 || !s.rows.every((r) => Array.isArray(r) && r.length === s.columns.length && r.every(cell)) || !Array.isArray(s.column_metadata) || s.column_metadata.length !== s.columns.length || !s.column_metadata.every((m, i) => exact(m, ['name','role','kind']) && m.name === s.columns[i] && ['patient_count','numeric_measure','category','unknown'].includes(m.role) && typeof m.kind === 'string')) throw Error('Invalid snapshot format.');
    if (s.truncated && s.rows.length !== 200) throw Error('Invalid snapshot truncation metadata.');
    if (s.suppression !== null && !(exact(s.suppression, ['enabled','threshold','policy_version','status','reason']) && s.suppression.enabled === true && Number.isInteger(s.suppression.threshold) && s.suppression.threshold >= 1 && s.suppression.policy_version === 1 && s.suppression.status === 'released' && s.suppression.reason === null)) throw Error('Invalid snapshot policy.');
    return {...s, result_status:'released', executed:true, is_compliant:true, imported:true};
  }
  async function clipboard(text) {
    try { await navigator.clipboard.writeText(text); return; } catch (_) { /* fallback below */ }
    const previousFocus = document.activeElement;
    const field = document.createElement('textarea'); field.value = text; field.style.position = 'fixed'; field.style.opacity = '0';
    document.body.appendChild(field); field.select();
    try { if (!document.execCommand('copy')) throw Error('Copy unavailable.'); } finally { field.remove(); previousFocus?.focus?.(); }
  }
  function download(text, name, type) {
    const url = URL.createObjectURL(new Blob([text], {type})), a = document.createElement('a');
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const instances = new Set();
  let removalObserver;
  function disposeAll(parent) {
    for (const instance of [...instances]) {
      if (!parent || parent.contains(instance.element)) instance.dispose();
    }
  }

  function mount(parent, question, d, options = {}) {
    const policy = options.policy;
    if (d.imported && policy?.state !== 'available') throw Error('Privacy policy unavailable');
    const box = document.createElement('section'); box.className = 'released-result'; parent.appendChild(box);
    if (d.imported) box.innerHTML = d.synthetic_preview ? '<p class="text">Synthetic preview — no database execution</p>' : '<p class="text">Imported snapshot — not re-verified against live data</p>';
    if (d.result_status === 'withheld') { box.innerHTML += `<p role="status">${esc(d.suppression?.reason || 'Result withheld by the privacy policy.')}</p>`; return; }
    if (d.result_status !== 'released') return;
    const blocked = document.createElement('p'); blocked.className = 'result-policy-blocked'; blocked.setAttribute('role', 'status');
    box.appendChild(blocked);
    const sensitive = document.createElement('div'); sensitive.className = 'result-sensitive'; sensitive.hidden = true; sensitive.inert = true;
    box.appendChild(sensitive);
    const allowed = () => policy?.state === 'available' && (d.imported || policyMatches(d, policy.config));
    function updatePolicy() {
      const visible = allowed();
      sensitive.hidden = !visible; sensitive.inert = !visible;
      blocked.hidden = visible;
      blocked.textContent = policy?.state === 'available'
        ? 'Privacy policy changed. Re-ask before displaying cached numbers.'
        : policy?.state === 'loading' ? 'Loading privacy policy…' : 'Privacy policy unavailable';
      sensitive.querySelectorAll('button').forEach((button) => {
        button.disabled = !visible || button.dataset.pageDisabled === 'true';
      });
    }
    const rows = normalize(d), chart = visual(d);
    if (chart.kind === 'number') sensitive.innerHTML = `<div class="result-hero"><span class="result-number">${esc(chart.value)}</span></div>`;
    else if (chart.values) {
      const max = Math.max(0, ...chart.values.flat().map((v) => Math.abs(number(v) ?? 0)));
      sensitive.innerHTML = `<div class="result-chart" role="group" aria-label="Result chart"><p>${chart.series.map(esc).join(' · ')}</p>${chart.labels.map((label, i) => `<div class="chart-group"><strong>${esc(label)}</strong>${chart.values[i].map((v, j) => `<div class="chart-row"><span>${esc(chart.series[j])}</span><span class="chart-track"><span class="chart-bar series-${j}" style="width:${number(v) === null || !max ? 0 : Math.abs(number(v)) / max * 100}%"></span></span><span>${esc(v === '' ? 'No value' : v)}</span></div>`).join('')}</div>`).join('')}</div>`;
    }
    if (comparisonRequested(question) && rows.length === 1 && d.columns.length === 1) {
      const notice = document.createElement('p'); notice.className = 'result-comparison-notice'; notice.setAttribute('role','status');
      notice.textContent = 'Only one value was returned. Separate cohort counts are needed for a comparison chart; no missing groups have been inferred.';
      sensitive.prepend(notice);
    }
    if (options.caption || options.description) {
      const caption = document.createElement('div');
      caption.innerHTML = `<p class="answer-text">${esc(options.caption || '')}</p><p class="counted">${esc(options.description || '')}</p>`;
      sensitive.prepend(caption);
    }
    const table = document.createElement('div'); table.className = 'result-table-wrap'; sensitive.appendChild(table);
    let page = 0;
    function renderPage() {
      const start = page * 50, stop = Math.min(start + 50, rows.length);
      table.innerHTML = `<table class="tbl result-tbl"><thead><tr>${d.columns.map((c) => `<th scope="col">${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.slice(start, stop).map((r) => `<tr>${r.map((v,i) => `<td${measure(d.column_metadata[i]) ? ' class="num"' : ''}>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table><p role="status">Showing ${rows.length ? start + 1 : 0}–${stop} of ${rows.length} rows${d.legacy_incomplete ? ' (legacy history may be incomplete)' : ''}.</p>`;
      if (chart.kind === 'number') {
        table.innerHTML = `<details class="result-exact-value"><summary>Show exact value</summary>${table.innerHTML}</details>`;
      }
      if (rows.length > 50) {
        for (const [label, change, disabled] of [['Previous', -1, page === 0], ['Next', 1, stop >= rows.length]]) {
          const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.disabled = disabled;
          b.dataset.pageDisabled = String(disabled);
          b.onclick = () => {
            if (!allowed()) return;
            page += change; renderPage(); updatePolicy(); table.querySelector('button:not(:disabled)')?.focus();
          };
          table.appendChild(b);
        }
      }
    }
    renderPage();
    if (d.truncated) { const p = document.createElement('p'); p.textContent = 'Results are truncated at 200 rows by the backend.'; sensitive.appendChild(p); }
    // Copying shares the result's existing policy subscription and privacy boundary.
    const record = d.reproducibility;
    if (!d.imported && !d.shared && validReproducibility(record)) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'reproducibility-copy';
      button.setAttribute('aria-label', 'Copy reproducibility record');
      button.title = record.source === 'synthetic' ? 'Copy reproducibility record — synthetic preview, not for citation' : 'Copy reproducibility record';
      button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg>';
      button.onclick = async () => {
        if (!allowed()) { options.toast?.('Privacy policy unavailable or changed'); return; }
        try { await clipboard(reproducibilityText(record, d)); options.toast?.('Reproducibility record copied'); }
        catch (_) { options.toast?.('Could not copy the reproducibility details.'); }
      };
      sensitive.classList.add('has-reproducibility-copy');
      sensitive.appendChild(button);
    } else {
      const notice = document.createElement('p'); notice.className = 'reproducibility-unavailable';
      notice.textContent = d.synthetic_preview ? 'No query was executed for this visual preview.' :
        d.imported || d.shared ? 'This saved format does not include the complete reproducibility record.' :
        'Reproducibility details unavailable for this saved result.';
      sensitive.appendChild(notice);
    }
    if (!d.imported) {
      const tools = document.createElement('div'); tools.className = 'result-tools'; sensitive.appendChild(tools);
      const actions = [
        ['Download CSV', () => download(delimited(d), 'results.csv', 'text/csv;charset=utf-8')],
        ['Copy table', async () => { await clipboard(delimited(d, '\t')); options.toast?.('Table copied'); }],
        ['Download snapshot', () => { if (confirm(`Save this question with the released result? Check the question contains no identifying or confidential text.\n\n${question}`) && allowed()) download(JSON.stringify(snapshot(question, d), null, 2), 'result.snapshot.json', 'application/json'); }],
      ];
      if (d.share_token && options.createShare) {
        let published;
        actions.push(['Create shareable link', async () => {
          if (!published && !confirm(`Create an unlisted link? Anyone with the link can view this question and aggregate result until the link expires. Check the question contains no identifying or confidential text.\n\n${question}`)) return;
          if (!allowed()) return;
          published = published || await options.createShare(d.share_token);
          if (!allowed()) return;
          const url = new URL(location.href);
          url.search = ''; url.hash = ''; url.searchParams.set('share', published.share_id);
          let panel = sensitive.querySelector('.share-link');
          if (!panel) { panel = document.createElement('div'); panel.className = 'share-link'; sensitive.appendChild(panel); }
          panel.innerHTML = `<label>Shareable link<input type="text" readonly value="${esc(url.href)}"></label><p>Anyone with this link can view the saved aggregate result. Expires ${esc(published.expires_at)}.</p>`;
          try { await clipboard(url.href); options.toast?.('Shareable link copied'); }
          catch (_) { options.toast?.('Link created. Select and copy it below.'); }
        }]);
      }
      const actionIcons = {
        'Download CSV': '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
        'Copy table': '<rect x="8" y="7" width="12" height="14" rx="2"/><path d="M16 7V3H4v14h4m0-5h12m-6-5v14"/>',
        'Download snapshot': '<path d="M5 3h9l5 5v6M14 3v6h5M12 12v9m-4-4 4 4 4-4M5 3v18h3"/>',
      };
      for (const [label, action] of actions) {
        const b = document.createElement('button'); b.type = 'button';
        if (actionIcons[label]) {
          b.className = 'result-action-icon'; b.setAttribute('aria-label', label); b.title = label;
          b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${actionIcons[label]}</svg>`;
        } else b.textContent = label;
        b.onclick = async () => {
          if (!allowed()) { options.toast?.('Privacy policy unavailable or changed'); return; }
          try { await action(); } catch (error) { options.toast?.(label === 'Create shareable link' ? error.message || 'Could not share this result.' : 'Could not export this result.'); }
        };
        if (actionIcons[label]) tools.appendChild(b);
        else { b.className = 'result-share-action'; sensitive.appendChild(b); }
      }
    }
    const unsubscribe = policy?.subscribe ? policy.subscribe(updatePolicy) : (updatePolicy(), () => {});
    const instance = {element:box, dispose() {
      unsubscribe(); instances.delete(instance);
      if (!instances.size) { removalObserver?.disconnect(); removalObserver = null; }
    }};
    instances.add(instance);
    if (!removalObserver && typeof MutationObserver !== 'undefined') {
      removalObserver = new MutationObserver(() => {
        for (const active of [...instances]) if (!active.element.isConnected) active.dispose();
      });
      removalObserver.observe(document.documentElement, {childList:true, subtree:true});
    }
    return instance;
  }
  const api = {comparisonRequested, syntheticRecord, validReproducibility, reproducibilityText, number, normalize, visual, policyMatches, delimited, snapshot, validateSnapshot, mount, disposeAll, clipboard};
  if (typeof module !== 'undefined') module.exports = api;
  else { root.SQ = root.SQ || {}; root.SQ.result = api; }
})(typeof window === 'undefined' ? globalThis : window);
