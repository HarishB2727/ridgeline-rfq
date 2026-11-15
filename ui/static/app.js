/* Ridgeline RFQ pipeline — the stages are the columns, each email is a row.
   A window onto the pipeline, not a second implementation: everything shown
   comes from the same functions `rfq parse` calls. */

const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x !== undefined) n.textContent = x; return n; };

const STAGES = [
  { key: "ingest",    n: 1, title: "Open the envelope",   kind: "code" },
  { key: "documents", n: 2, title: "Read attachments",    kind: "code" },
  { key: "classify",  n: 3, title: "What kind of email?", kind: "ai"   },
  { key: "extract",   n: 4, title: "Read each document",  kind: "ai"   },
  { key: "verify",    n: 5, title: "Check every line",    kind: "code" },
  { key: "link",      n: 6, title: "Place in the inbox",  kind: "code" },
];

let JOB = null, SNAP = null, OPEN = null, DETAIL = {}, timer = null;

/* ───────────────────────────────────────────────────────────── upload */

const send = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(b) }).then(r => r.json());

function addFiles(list) {
  const files = [...list].filter(f => /\.eml$/i.test(f.name) || f.type === "message/rfc822");
  if (!files.length) return alert("Those don't look like .eml files.");
  Promise.all(files.map(f => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res({ name: f.name, b64: String(r.result).split(",", 2)[1] });
    r.onerror = rej;
    r.readAsDataURL(f);
  }))).then(p => send("/api/stage", { job: JOB, files: p }))
    .then(s => { if (s.error) throw new Error(s.error); JOB = s.job; apply(s); })
    .catch(e => alert("Could not read those files: " + e.message));
}

function process() {
  if (!JOB) return;
  DETAIL = {};
  $("#process").disabled = true;
  send("/api/process", { job: JOB }).then(() => poll());
}

function poll() {
  if (!JOB) return;
  fetch(`/api/job?job=${encodeURIComponent(JOB)}`).then(r => r.json()).then(s => {
    if (s.error) return;
    apply(s);
    if (s.state === "running") timer = setTimeout(poll, 400);
    else if (OPEN) loadDetail(OPEN, true);
  });
}

/* ───────────────────────────────────────────────────────────── render */

function apply(s) {
  SNAP = s;
  $("#pipebar").classList.toggle("hidden", !s.order.length);
  $("#dropzone").classList.toggle("hidden", !!s.order.length);
  $("#reset").hidden = !s.order.length;
  $("#process").disabled = s.state === "running" || !s.order.length;
  $("#process").textContent = s.state === "running" ? "Processing…"
    : s.state === "done" ? "Process again" : `Process ${s.order.length}`;
  renderPipebar();
  renderMeter(s);
  renderGrid(s);
}

function renderPipebar() {
  const bar = $("#pipebar");
  if (bar.childElementCount) return;
  bar.append(el("div", "ph lead", "email"));
  for (const st of STAGES) {
    const h = el("div", `ph ${st.kind === "ai" ? "ai" : "codey"}`);
    const n = el("div", "n");
    n.append(`STEP ${st.n}`, el("span", "tag", st.kind === "ai" ? "model" : "code"));
    h.append(n, el("div", "t", st.title));
    bar.append(h);
  }
}

function renderMeter(s) {
  const m = $("#meter"); m.innerHTML = "";
  const done = s.order.filter(i => s.emails[i].state === "done").length;
  const bits = [[`${done}/${s.order.length}`, "processed"]];
  if (s.cost) bits.push(
    [`${s.cost.model_calls}`, "calls"],
    [`${s.cost.cache_hits}`, "cached"],
    [`$${(s.cost.cold_run?.cost_usd ?? 0).toFixed(4)}`, "cold"]);
  if (s.state === "running") bits.push([`${s.elapsed}s`, ""]);
  for (const [v, l] of bits) { const x = el("span"); x.append(el("b", null, v)); if (l) x.append(" ", el("span", "muted", l)); m.append(x); }
}

function cell(stateCls, body) {
  const c = el("div", `cell ${stateCls}`);
  for (const b of body) if (b) c.append(b);
  return c;
}
const val = (big, rest) => { const v = el("div", "v"); v.append(el("span", "big", big)); if (rest) v.append(rest); return v; };

function renderGrid(s) {
  const g = $("#grid");
  const openNow = OPEN;
  g.innerHTML = "";
  for (const q of s.queue) {
    const e = s.emails[q.id] || {};
    const sum = e.summary || {};
    const st = e.steps || {};
    const row = el("div", `erow ${openNow === q.id ? "open" : ""}`);

    /* lead cell */
    const lead = el("div", "cell lead");
    lead.append(el("span", "caret", "▶"));
    const main = el("div", "lead-main");
    main.append(el("div", "eid", q.id));
    main.append(el("div", "esubj", q.subject));
    main.append(el("div", "esender", q.sender || ""));
    lead.append(main);
    if (e.state === "running" || e.state === "linking") lead.append(el("span", "dotpulse"));
    lead.onclick = () => toggle(q.id);
    row.append(lead);

    /* 1 ingest */
    row.append(cell(st.ingest || "pending", [
      val(`${q.body_chars ?? 0}`, el("span", "muted", "chars")),
      q.quoted_chars ? el("div", "sub", `+${q.quoted_chars} thread — excluded`)
        : el("div", "sub", "no thread"),
      q.from_us ? el("span", "chip", "our rep") : null,
    ]));

    /* 2 documents */
    const docs = sum.documents || [];
    const kinds = {};
    for (const d of docs) if (d.source !== "body") kinds[d.kind] = (kinds[d.kind] || 0) + 1;
    const nAtt = docs.filter(d => d.source !== "body").length || (q.attachments || []).length;
    const bad = docs.some(d => d.kind === "unreadable");
    row.append(cell((bad ? "warnc " : "") + (st.documents || "pending"), [
      val(`${nAtt}`, el("span", "muted", nAtt === 1 ? "file" : "files")),
      el("div", "sub", docs.length
        ? Object.entries(kinds).map(([k, v]) => `${v} ${k}`).join(", ") || "body only"
        : (q.attachments || []).join(", ") || "body only"),
      bad ? el("span", "chip warn", "unreadable") : null,
    ]));

    /* 3 classify */
    const cCalls = (e.calls || []).filter(c => /classification/.test(c.label) || c.label.startsWith("Decide"));
    row.append(cell(st.classify || "pending", [
      sum.classification ? el("span", `chip ${["rfq", "rfq_revision", "purchase_order"].includes(sum.classification) ? "ok" : ""}`, sum.classification) : el("div", "v muted", "—"),
      cCalls.length ? el("div", "sub", `${cCalls.length} model call${cCalls.length > 1 ? "s" : ""}`) : null,
    ]));

    /* 4 extract */
    const xCalls = (e.calls || []).length - cCalls.length;
    const skipped = st.extract === "skipped";
    row.append(cell(st.extract || "pending", [
      skipped ? el("span", "chip", "skipped") : val(`${xCalls}`, el("span", "muted", xCalls === 1 ? "call" : "calls")),
      el("div", "sub", skipped ? "carries no items — the gate" : `${sum.n_items ?? 0} lines returned`),
    ]));

    /* 5 verify */
    row.append(cell((sum.n_dropped ? "warnc " : "") + (st.verify || "pending"), [
      val(`${sum.n_items ?? 0}`, el("span", "muted", "kept")),
      el("div", "sub", sum.n_dropped ? `${sum.n_dropped} dropped` : "nothing dropped"),
    ]));

    /* 6 link */
    const flag = sum.needs_review ? "warnc" : e.error ? "badc" : st.link === "done" ? "done" : (st.link || "pending");
    row.append(cell(flag, [
      sum.needs_review ? el("span", "chip warn", "needs review")
        : sum.duplicate_of ? el("span", "chip", "duplicate")
        : sum.revises ? el("span", "chip", "revision")
        : st.link === "done" ? el("span", "chip ok", "ready") : el("div", "v muted", "—"),
      el("div", "sub", sum.duplicate_of ? `of ${sum.duplicate_of}`
        : sum.revises ? `revises ${sum.revises}` : (sum.review_reason || "").slice(0, 60)),
    ]));

    g.append(row);

    if (openNow === q.id) {
      const d = el("div", "detail");
      d.id = "detail-" + q.id;
      d.append(el("div", "note", "loading…"));
      row.append(d);
      if (DETAIL[q.id]) renderDetail(DETAIL[q.id], d);
    }
  }
}

function toggle(id) {
  OPEN = OPEN === id ? null : id;
  renderGrid(SNAP);
  if (OPEN) loadDetail(OPEN);
}

function loadDetail(id, force) {
  if (DETAIL[id] && !force) return;
  fetch(`/api/email?job=${encodeURIComponent(JOB)}&id=${encodeURIComponent(id)}`)
    .then(r => r.json()).then(e => {
      DETAIL[id] = e;
      const host = $("#detail-" + id);
      if (host) renderDetail(e, host);
    });
}

/* ──────────────────────────────────────────────────── expanded detail */

function sec(n, title, after) {
  const d = el("div", "sec");
  d.append(el("div", "num", String(n)));
  const h = el("h3", null, title + " ");
  if (after) h.append(el("span", "after", "— " + after));
  d.append(h);
  return d;
}
function card(title, chips = [], warn) {
  const c = el("div", warn ? "card warncard" : "card");
  const h = el("div", "ch"); h.append(el("span", null, title));
  for (const ch of chips) h.append(ch);
  const b = el("div", "cb");
  c.append(h, b); c.body = b; return c;
}

function renderDetail(e, host) {
  host.innerHTML = "";
  const env = e.envelope || {}, sum = e.summary || {}, r = e.result || {};

  /* email | result, side by side */
  const cols = el("div", "cols");

  const left = card("the email, as it arrived");
  const kv = el("dl", "kv");
  for (const [k, v] of [["from", env.from], ["to", env.to], ["date", env.date], ["subject", env.subject]])
    if (v) kv.append(el("dt", null, k), el("dd", null, v));
  left.body.append(kv);
  const pre = el("pre", "txt scroll"); pre.textContent = e.body?.current || "(empty body)";
  left.body.append(pre);
  if (e.body?.quoted) {
    const q = el("div", "quoted");
    const lbl = el("div", "lbl");
    lbl.append(el("span", "chip code", "step 1"), "thread history — never shown to the model");
    const p2 = el("pre", "txt scroll"); p2.textContent = e.body.quoted;
    q.append(lbl, p2);
    left.body.append(q);
  }
  cols.append(left);

  const right = card("what came out", sum.n_items != null ? [el("span", "chip ok", `${sum.n_items} lines`)] : []);
  if (e.state !== "done") right.body.append(el("div", "note", "Not processed yet — press Process."));
  else {
    if (r.needs_review) {
      const w = card("a human has to look at this", [], true);
      w.body.append(el("div", null, r.review_reason || ""));
      right.body.append(w);
    }
    right.body.append(itemsTable(r.line_items || [], e));
  }
  cols.append(right);
  host.append(cols);

  /* stage 1 */
  host.append(sec(1, "Open the envelope", "code"));
  const c1 = card("what code decided");
  const row1 = el("div", "row");
  row1.append(el("span", "chip code", `current message ${(e.body?.current || "").length} chars`));
  row1.append(el("span", "chip", `thread history ${(e.body?.quoted || "").length} chars — excluded`));
  for (const k of Object.keys(e.signals || {})) {
    if (k === "attachment_names") continue;
    row1.append(el("span", "chip", k.replace(/_/g, " ")));
  }
  c1.body.append(row1);
  host.append(c1);

  /* stage 2 */
  host.append(sec(2, "Read the attachments", "code"));
  const c2 = card("documents found");
  const docs = e.documents || [];
  if (!docs.length) c2.body.append(el("div", "note", "No attachments — everything came from the body."));
  const roles = {};
  for (const t of (e.trace?.documents || [])) roles[t.source] = t.role;
  for (const doc of docs) {
    const dr = el("div", "docrow");
    dr.append(el("span", "docname", doc.source));
    dr.append(el("span", doc.kind === "unreadable" ? "chip warn" : "chip", doc.kind));
    if (roles[doc.source]) dr.append(el("span", "chip ok", roles[doc.source]));
    if (doc.truncated) dr.append(el("span", "chip warn", "truncated"));
    c2.body.append(dr);
    if (doc.error) c2.body.append(el("div", "note", doc.error));
    for (const b64 of doc.images || []) {
      const img = new Image(); img.src = `data:${doc.media_type};base64,${b64}`; img.className = "shot";
      c2.body.append(img);
    }
    if (doc.text) {
      const det = el("details", "raw");
      det.append(el("summary", null, "the text the model is given"));
      const p = el("pre", "txt"); p.textContent = doc.text;
      det.append(p); c2.body.append(det);
    }
  }
  host.append(c2);

  /* stages 3 & 4 — the model calls */
  const calls = e.calls || [];
  const cc = calls.filter(c => (c.schema || "").startsWith("email_classification"));
  const xc = calls.filter(c => !(c.schema || "").startsWith("email_classification"));

  host.append(sec(3, "What kind of email?", "model call"));
  if (!cc.length) { const n = card("not run yet"); host.append(n); }
  for (const c of cc) host.append(callCard(c));
  if (cc.length && !["rfq", "rfq_revision", "purchase_order"].includes(sum.classification)) {
    const g = card("the gate");
    g.body.append(el("div", null,
      `“${sum.classification}” carries no line items, so step 4 never ran. That is why this email cost almost nothing.`));
    host.append(g);
  }

  host.append(sec(4, "Read each document", `${xc.length} model call${xc.length === 1 ? "" : "s"}`));
  if (!xc.length) {
    const n = card("nothing read");
    n.body.append(el("div", "note", e.state === "done" ? "Skipped — see the gate above." : "Not run yet."));
    host.append(n);
  }
  for (const c of xc) host.append(callCard(c));

  /* stage 5 */
  const dropped = e.trace?.dropped_lines || [];
  host.append(sec(5, "Check every line", "code"));
  const c5 = card("verification", [el("span", dropped.length ? "chip warn" : "chip ok",
    dropped.length ? `${dropped.length} dropped` : "nothing dropped")]);
  c5.body.append(el("div", "note",
    "Every line must actually occur in the document it claims to come from; the unit must be one of the fourteen allowed; headings, totals and price rows are not items."));
  for (const dl of dropped) {
    const b = el("div", "docrow");
    b.append(el("span", "docname", dl.raw));
    b.append(el("span", "chip warn", dl.dropped || dl.note));
    c5.body.append(b);
  }
  host.append(c5);

  /* stage 6 */
  host.append(sec(6, "Place it in the inbox", "code"));
  const c6 = card("outcome");
  const o = el("div", "row");
  o.append(el("span", "chip ok", r.classification || "—"));
  if (r.duplicate_of) o.append(el("span", "chip", "duplicate of " + r.duplicate_of));
  if (r.revises) o.append(el("span", "chip", "revises " + r.revises));
  o.append(el("span", r.needs_review ? "chip warn" : "chip ok", r.needs_review ? "needs review" : "ready to quote"));
  c6.body.append(o);
  if (r.review_reason) c6.body.append(el("div", "note", r.review_reason));
  const per = SNAP?.cost?.per_email?.[e.id];
  if (per) c6.body.append(el("div", "note",
    `${per.calls + per.cache_hits} model call(s) · $${(per.cold_cost_usd ?? 0).toFixed(4)} cold`
    + (per.cache_hits ? ` · ${per.cache_hits} replayed from cache, $0.00 spent` : "")));
  host.append(c6);
}

/* ───────────────────────────────────────────── one model call, in full */

function callCard(c) {
  const box = el("div", "call");
  const h = el("div", "ch");
  h.append(el("span", "chip ai", "model"));
  h.append(el("span", "title", c.label));
  h.append(el("span", "chip", c.model));
  h.append(el("span", c.cached ? "chip ok" : "chip", c.cached ? "replayed from cache" : "called"));
  if (c.usage?.prompt_tokens) h.append(el("span", "chip", `${c.usage.prompt_tokens} in / ${c.usage.completion_tokens} out`));
  h.append(el("span", "chip", `$${(c.cost_usd ?? 0).toFixed(4)}`));
  box.append(h);

  const io = el("div", "io");
  const L = el("div");
  L.append(el("h4", null, "what we sent"));
  const ds = el("details", "raw");
  ds.append(el("summary", null, "system prompt — the rules it was given"));
  const p1 = el("pre", "txt"); p1.textContent = c.system || "";
  ds.append(p1); L.append(ds);
  const du = el("details", "raw"); du.open = true;
  du.append(el("summary", null, "user message — this email's own content"));
  const p2 = el("pre", "txt"); p2.textContent = (c.user || []).join("\n\n");
  du.append(p2); L.append(du);
  for (const b64 of c.images || []) {
    const img = new Image(); img.src = `data:image/png;base64,${b64}`; img.className = "shot";
    L.append(img);
  }

  const R = el("div");
  R.append(el("h4", null, "what came back"));
  const resp = c.response || {};
  if (resp.classification) {
    const row = el("div", "row"); row.append(el("span", "chip ok", resp.classification));
    R.append(row, el("div", null, resp.why || ""));
  } else if (resp.lines) {
    const row = el("div", "row");
    row.append(el("span", "chip ok", `${resp.lines.length} lines`), el("span", "chip", resp.document_role || ""));
    R.append(row);
    const t = el("table");
    const hr = el("tr");
    for (const x of ["qty", "uom", "description"]) hr.append(el("th", null, x));
    t.append(hr);
    for (const ln of resp.lines.slice(0, 60)) {
      const tr = el("tr");
      tr.append(el("td", "num", ln.qty ?? "—"), el("td", "uom", ln.uom || "—"), el("td", null, ln.description));
      t.append(tr);
    }
    R.append(t);
  }
  const dr = el("details", "raw");
  dr.append(el("summary", null, "raw JSON it returned"));
  const p3 = el("pre", "txt"); p3.textContent = JSON.stringify(c.response, null, 2);
  dr.append(p3); R.append(dr);

  io.append(L, R); box.append(io);
  return box;
}

/* ────────────────────────────────────────────────────────── line items */

function itemsTable(items, e) {
  if (!items.length) return el("div", "note",
    "No line items. If this email needed someone to act, it is flagged above.");
  const t = el("table");
  const hr = el("tr");
  for (const h of ["qty", "uom", "description", "source"]) hr.append(el("th", null, h));
  t.append(hr);
  const tl = e.trace?.line_items || [];
  items.forEach((it, i) => {
    const tr = el("tr", "item");
    tr.append(el("td", "num", it.qty ?? "—"), el("td", "uom", it.uom || "—"),
      el("td", null, it.description), el("td", "src", it.source));
    const prov = provRow({ ...it, ...(tl[i] || {}) }, e.id);
    prov.hidden = true;
    tr.onclick = ev => { ev.stopPropagation(); prov.hidden = !prov.hidden; };
    t.append(tr, prov);
  });
  return t;
}

function provRow(it, id) {
  const tr = el("tr", "prov");
  const td = el("td"); td.colSpan = 4;
  td.append(el("div", "raw", "raw: " + it.raw));
  const ctx = it._context_from || it.context_from;
  if (ctx?.length) {
    td.append(el("div", "muted small", "context carried from:"));
    const ul = el("ul");
    for (const c of ctx) ul.append(el("li", null, c));
    td.append(ul);
  } else td.append(el("div", "muted small", "no inherited context — the line stated everything itself"));
  const row = el("div", "row");
  const a = el("button", "mini", "✓ accept");
  a.onclick = ev => { ev.stopPropagation(); decide(id, it, "accept", a); };
  const o = el("button", "mini", "✎ overturn");
  o.onclick = ev => { ev.stopPropagation(); const n = prompt("What should this line say?", it.description); if (n !== null) decide(id, it, "overturn", o, n); };
  row.append(a, o, el("span", "muted small", "kept in ui/decisions.jsonl"));
  td.append(row); tr.append(td);
  return tr;
}

function decide(email, item, action, btn, now) {
  send("/api/decision", { email, raw: item.raw, action,
    was: { qty: item.qty, uom: item.uom, description: item.description },
    now: now ? { description: now } : null })
    .then(() => { btn.classList.add("done"); btn.textContent = action === "accept" ? "✓ accepted" : "✎ overturned"; });
}

/* ──────────────────────────────────────────────────────────── wiring */

$("#picker").addEventListener("change", ev => { addFiles(ev.target.files); ev.target.value = ""; });
$("#process").addEventListener("click", process);
$("#reset").addEventListener("click", () => send("/api/reset", { job: JOB }).then(() => {
  JOB = null; SNAP = null; OPEN = null; DETAIL = {};
  $("#grid").innerHTML = ""; $("#meter").innerHTML = "";
  $("#pipebar").classList.add("hidden"); $("#dropzone").classList.remove("hidden");
  $("#reset").hidden = true; $("#process").disabled = true; $("#process").textContent = "Process";
}));

let depth = 0;
addEventListener("dragenter", e => { e.preventDefault(); if (++depth === 1) $("#veil").classList.add("on"); });
addEventListener("dragover", e => e.preventDefault());
addEventListener("dragleave", e => { e.preventDefault(); if (--depth <= 0) { depth = 0; $("#veil").classList.remove("on"); } });
addEventListener("drop", e => { e.preventDefault(); depth = 0; $("#veil").classList.remove("on");
  if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files); });

renderPipebar();
$("#pipebar").classList.add("hidden");
