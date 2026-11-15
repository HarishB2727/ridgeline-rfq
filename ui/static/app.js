/* Ridgeline RFQ desk — a window onto the pipeline, not a second implementation.
   Everything shown comes from the same functions `rfq parse` calls. */

const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x !== undefined) n.textContent = x; return n; };
const AI_STEPS = new Set(["classify", "extract"]);

let JOB = null, SNAP = null, SELECTED = null, timer = null;

/* ------------------------------------------------------------------ upload */

function send(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(r => r.json());
}

function addFiles(list) {
  const files = [...list].filter(f => /\.eml$/i.test(f.name) || f.type === "message/rfc822");
  if (!files.length) return alert("Those don't look like .eml files.");
  Promise.all(files.map(f => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res({ name: f.name, b64: String(r.result).split(",", 2)[1] });
    r.onerror = rej;
    r.readAsDataURL(f);
  }))).then(payload => send("/api/stage", { job: JOB, files: payload }))
    .then(snap => { if (snap.error) throw new Error(snap.error); JOB = snap.job; apply(snap); })
    .catch(e => alert("Could not read those files: " + e.message));
}

function process() {
  if (!JOB) return;
  $("#process").disabled = true;
  send("/api/process", { job: JOB }).then(() => poll(true));
}

function poll(loop) {
  if (!JOB) return;
  fetch(`/api/job?job=${encodeURIComponent(JOB)}`).then(r => r.json()).then(snap => {
    if (snap.error) return;
    apply(snap);
    if (snap.state === "running") timer = setTimeout(() => poll(true), 450);
    else if (loop && SELECTED) openEmail(SELECTED);
  });
}

/* ------------------------------------------------------------------- render */

function apply(snap) {
  SNAP = snap;
  renderQueue(snap);
  renderMeter(snap);
  $("#process").disabled = snap.state === "running" || !snap.order.length;
  $("#process").textContent = snap.state === "running" ? "Processing…"
    : snap.state === "done" ? "Process again" : `Process ${snap.order.length || ""}`.trim();
  $("#reset").hidden = !snap.order.length;
  $("#dropzone").style.display = snap.order.length ? "none" : "";
  $("#queue-count").textContent = snap.order.length ? `${snap.order.length}` : "";
  if (!SELECTED && snap.state === "running") {
    const running = snap.order.find(id => snap.emails[id].state === "running");
    if (running) openEmail(running);
  }
  if (SELECTED && $("#detail").dataset.id === SELECTED && snap.emails[SELECTED]?.state === "done"
      && $("#detail").dataset.stale === "1") openEmail(SELECTED);
}

function renderMeter(snap) {
  const m = $("#meter"); m.innerHTML = "";
  const done = snap.order.filter(id => snap.emails[id].state === "done").length;
  const bits = [[`${done}/${snap.order.length}`, "read"]];
  if (snap.cost) {
    const c = snap.cost;
    bits.push([`${c.model_calls}`, "model calls"]);
    bits.push([c.cache_hits ? `${c.cache_hits}` : "0", "from cache"]);
    bits.push([`$${(c.cold_run?.cost_usd ?? 0).toFixed(4)}`, "cold cost"]);
  }
  if (snap.state === "running") bits.push([`${snap.elapsed}s`, ""]);
  for (const [v, l] of bits) { const s = el("span"); s.append(el("b", null, v)); if (l) s.append(" ", el("span", "muted", l)); m.append(s); }
}

function renderQueue(snap) {
  const box = $("#queue-list"); box.innerHTML = "";
  for (const q of snap.queue) {
    const e = snap.emails[q.id] || {};
    const sum = e.summary || {};
    let cls = e.state === "done" ? "done" : e.state === "running" || e.state === "linking" ? "running" : "";
    if (e.state === "done" && sum.needs_review) cls = "review";
    if (e.error) cls = "failed";
    const row = el("div", `qrow ${cls} ${SELECTED === q.id ? "active" : ""}`);
    row.append(el("div", "qdot"));
    const main = el("div", "qmain");
    main.append(el("div", "qid", q.id));
    main.append(el("div", "qsubj", q.subject));
    const meta = e.state === "done"
      ? `${sum.classification || ""}${sum.n_items != null ? ` · ${sum.n_items} items` : ""}`
      : (q.attachments?.length ? `${q.attachments.length} attachment(s)` : "no attachments");
    main.append(el("div", "qmeta", meta));
    row.append(main);
    row.onclick = () => { SELECTED = q.id; renderQueue(SNAP); openEmail(q.id); };
    box.append(row);
  }
}

function stepsPreview(snap) {
  const ol = $("#steps-preview"); ol.innerHTML = "";
  (snap?.steps || []).forEach((s, i) => {
    const li = el("li");
    li.append(el("div", "n", String(i + 1)));
    const d = el("div");
    const t = el("div", "t"); t.append(s.title, " ");
    t.append(el("span", AI_STEPS.has(s.key) ? "chip ai" : "chip code", AI_STEPS.has(s.key) ? "model call" : "code"));
    d.append(t, el("div", "d", s.note));
    li.append(d);
    ol.append(li);
  });
}

/* ------------------------------------------------------------- email detail */

function openEmail(id) {
  SELECTED = id;
  fetch(`/api/email?job=${encodeURIComponent(JOB)}&id=${encodeURIComponent(id)}`)
    .then(r => r.json()).then(e => renderDetail(e));
}

function sec(n, title, after) {
  const d = el("div", "sec");
  d.append(el("div", "num", String(n)));
  const h = el("h3", null, title + " ");
  if (after) h.append(el("span", "after", "— " + after));
  d.append(h);
  return d;
}

function card(title, chips = []) {
  const c = el("div", "card");
  const h = el("div", "ch");
  h.append(el("span", null, title));
  for (const ch of chips) h.append(ch);
  c.append(h);
  const b = el("div", "cb");
  c.append(b);
  c.body = b;
  return c;
}

function renderDetail(e) {
  $("#welcome").hidden = true;
  const d = $("#detail");
  d.hidden = false;
  d.dataset.id = e.id;
  d.dataset.stale = e.state === "done" ? "0" : "1";
  d.innerHTML = "";

  const env = e.envelope || {};
  const sum = e.summary || {};
  const head = el("div", "dhead");
  head.append(el("h2", null, `${e.id} · ${env.subject || "(no subject)"}`));
  if (sum.classification) head.append(el("span", "chip accent", sum.classification));
  if (sum.needs_review) head.append(el("span", "chip warn", "needs review"));
  if (sum.duplicate_of) head.append(el("span", "chip", "duplicate of " + sum.duplicate_of));
  if (sum.revises) head.append(el("span", "chip", "revises " + sum.revises));
  d.append(head);
  d.append(el("div", "dsub", [env.from, env.date].filter(Boolean).join("  ·  ")));

  /* the six-step rail */
  const rail = el("div", "rail");
  const steps = SNAP?.steps || [];
  const state = SNAP?.emails?.[e.id]?.steps || {};
  steps.forEach((s, i) => {
    const r = el("div", `rstep ${state[s.key] || "pending"}`);
    const n = el("div", "rn"); n.append(`STEP ${i + 1}`, " ");
    n.append(el("span", AI_STEPS.has(s.key) ? "chip ai" : "chip code", AI_STEPS.has(s.key) ? "AI" : "code"));
    r.append(n, el("div", "rt", s.title));
    rail.append(r);
  });
  d.append(rail);

  /* side by side: the email itself | what we got out of it */
  const cols = el("div", "cols");

  const left = card("The email, as it arrived");
  const kv = el("dl", "kv");
  for (const [k, v] of [["from", env.from], ["to", env.to], ["date", env.date], ["subject", env.subject]]) {
    if (!v) continue;
    kv.append(el("dt", null, k), el("dd", null, v));
  }
  left.body.append(kv);
  const cur = el("pre", "txt scroll"); cur.textContent = e.body?.current || "(empty body)";
  left.body.append(cur);
  if (e.body?.quoted) {
    const q = el("div", "quoted");
    const lbl = el("div", "lbl");
    lbl.append(el("span", "chip code", "step 1"), "thread history — never shown to the model");
    const pre = el("pre", "txt scroll"); pre.textContent = e.body.quoted;
    q.append(lbl, pre);
    left.body.append(q);
  }
  cols.append(left);

  const right = card("What we extracted", sum.n_items != null ? [el("span", "chip ok", `${sum.n_items} lines`)] : []);
  if (e.state !== "done") {
    right.body.append(el("div", "empty-note", e.state === "pending"
      ? "Not processed yet — press Process." : "Reading…"));
  } else {
    const r = e.result || {};
    if (r.needs_review) {
      const w = el("div", "card warn");
      const wh = el("div", "ch"); wh.append(el("span", null, "a human has to look at this"));
      const wb = el("div", "cb"); wb.append(el("div", null, r.review_reason || ""));
      w.append(wh, wb);
      right.body.append(w);
    }
    right.body.append(itemsTable(r.line_items || [], e));
  }
  cols.append(right);
  d.append(cols);

  /* steps 1 & 2 */
  d.append(sec(1, "Open the envelope", "code · no model"));
  const c1 = card("what code decided");
  const sp = el("div", "row");
  sp.append(el("span", "chip code", `current message: ${(e.body?.current || "").length} chars`));
  sp.append(el("span", "chip code", `thread history: ${(e.body?.quoted || "").length} chars (excluded)`));
  for (const k of Object.keys(e.signals || {})) {
    if (k === "attachment_names") continue;
    sp.append(el("span", "chip", k.replace(/_/g, " ")));
  }
  c1.body.append(sp);
  d.append(c1);

  d.append(sec(2, "Read the attachments", "code · no model"));
  const c2 = card("documents found");
  const docs = e.documents || [];
  if (!docs.length) c2.body.append(el("div", "empty-note", "No attachments — everything came from the body."));
  const roles = {};
  for (const t of (e.trace?.documents || [])) roles[t.source] = t.role;
  for (const doc of docs) {
    const row = el("div", "docrow");
    row.append(el("span", "docname", doc.source));
    row.append(el("span", doc.kind === "unreadable" ? "chip warn" : "chip", doc.kind));
    if (roles[doc.source]) row.append(el("span", "chip ok", roles[doc.source]));
    if (doc.truncated) row.append(el("span", "chip warn", "truncated"));
    c2.body.append(row);
    if (doc.error) c2.body.append(el("div", "empty-note", doc.error));
    for (const b64 of doc.images || []) {
      const img = new Image(); img.src = `data:${doc.media_type};base64,${b64}`; img.className = "shot";
      c2.body.append(img);
    }
    if (doc.text) {
      const det = el("details", "raw");
      det.append(el("summary", null, "the text the model will be given"));
      const pre = el("pre", "txt"); pre.textContent = doc.text;
      det.append(pre);
      c2.body.append(det);
    }
  }
  d.append(c2);

  /* steps 3 & 4 — the model calls */
  const calls = e.calls || [];
  const classify = calls.filter(c => (c.schema || "").startsWith("email_classification"));
  const extract = calls.filter(c => !(c.schema || "").startsWith("email_classification"));

  d.append(sec(3, "What kind of email?", "model call"));
  if (!classify.length) d.append(card("not run yet").body.parentElement);
  classify.forEach((c, i) => d.append(callCard(c, e, calls.indexOf(c))));
  if (classify.length && !["rfq", "rfq_revision", "purchase_order"].includes(sum.classification)) {
    const g = card("the gate");
    g.body.append(el("div", null,
      `“${sum.classification}” carries no line items, so step 4 was skipped entirely. That is why this email cost almost nothing.`));
    d.append(g);
  }

  d.append(sec(4, "Read each document", `model call ×${extract.length}`));
  if (!extract.length) {
    const n = card("nothing to read");
    n.body.append(el("div", "empty-note", e.state === "done"
      ? "Extraction was skipped — see the gate above." : "Not run yet."));
    d.append(n);
  }
  extract.forEach(c => d.append(callCard(c, e, calls.indexOf(c))));

  /* step 5 */
  const dropped = e.trace?.dropped_lines || [];
  d.append(sec(5, "Check every line", "code · no model"));
  const c5 = card("verification", [el("span", dropped.length ? "chip warn" : "chip ok",
    dropped.length ? `${dropped.length} dropped` : "nothing dropped")]);
  c5.body.append(el("div", "empty-note",
    "Every line the model returned must actually occur in the document it claims to come from, the unit must be one of the fourteen allowed, and headings, totals and price rows are not items."));
  for (const dl of dropped) {
    const b = el("div", "docrow");
    b.append(el("span", "docname", dl.raw));
    b.append(el("span", "chip warn", dl.dropped || dl.note));
    c5.body.append(b);
  }
  d.append(c5);

  /* step 6 */
  d.append(sec(6, "Place it in the inbox", "code · no model"));
  const c6 = card("outcome");
  const r6 = e.result || {};
  const out = el("div", "row");
  out.append(el("span", "chip accent", r6.classification || "—"));
  if (r6.duplicate_of) out.append(el("span", "chip", "duplicate of " + r6.duplicate_of));
  if (r6.revises) out.append(el("span", "chip", "revises " + r6.revises));
  out.append(el("span", r6.needs_review ? "chip warn" : "chip ok",
    r6.needs_review ? "needs review" : "ready to quote"));
  c6.body.append(out);
  if (r6.review_reason) c6.body.append(el("div", "empty-note", r6.review_reason));
  const per = SNAP?.cost?.per_email?.[e.id];
  if (per) c6.body.append(el("div", "empty-note",
    `${per.calls + per.cache_hits} model call(s) · $${(per.cold_cost_usd ?? 0).toFixed(4)} cold` +
    (per.cache_hits ? ` · ${per.cache_hits} replayed from cache, $0.00 spent` : "")));
  d.append(c6);
}

/* ----------------------------------------------------- one model call, shown */

function callCard(c, e, idx) {
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

  const left = el("div");
  left.append(el("h4", null, "what we sent"));
  left.append(el("div", "empty-note", "The system prompt tells it the rules; the user message is this email's actual content."));
  const dl = el("details", "raw");
  dl.append(el("summary", null, "system prompt — the rules it was given"));
  const pre1 = el("pre", "txt"); pre1.textContent = c.system || "";
  dl.append(pre1);
  left.append(dl);
  const du = el("details", "raw");
  du.open = true;
  du.append(el("summary", null, "user message — what it was asked about"));
  const pre2 = el("pre", "txt"); pre2.textContent = (c.user || []).join("\n\n");
  du.append(pre2);
  left.append(du);
  for (const b64 of c.images || []) {
    const img = new Image(); img.src = `data:image/png;base64,${b64}`; img.className = "shot";
    left.append(img);
  }

  const right = el("div");
  right.append(el("h4", null, "what came back"));
  const r = c.response || {};
  if (r.classification) {
    const row = el("div", "row");
    row.append(el("span", "chip accent", r.classification));
    right.append(row);
    right.append(el("div", null, r.why || ""));
  } else if (r.lines) {
    right.append(el("div", "row", ""));
    right.lastChild.append(el("span", "chip ok", `${r.lines.length} lines`),
      el("span", "chip", r.document_role || ""));
    const t = el("table");
    const tr = el("tr");
    for (const x of ["qty", "uom", "description"]) tr.append(el("th", null, x));
    t.append(tr);
    for (const ln of r.lines.slice(0, 60)) {
      const row = el("tr");
      row.append(el("td", "num", ln.qty ?? "—"), el("td", "uom", ln.uom || "—"), el("td", null, ln.description));
      t.append(row);
    }
    right.append(t);
  }
  const draw = el("details", "raw");
  draw.append(el("summary", null, "the raw JSON it returned"));
  const pre3 = el("pre", "txt"); pre3.textContent = JSON.stringify(c.response, null, 2);
  draw.append(pre3);
  right.append(draw);

  io.append(left, right);
  box.append(io);
  return box;
}

/* --------------------------------------------------------------- line items */

function itemsTable(items, e) {
  if (!items.length) return el("div", "empty-note",
    "No line items. If this email needed someone to act, it is flagged above.");
  const t = el("table");
  const hr = el("tr");
  for (const h of ["qty", "uom", "description", "source"]) hr.append(el("th", null, h));
  t.append(hr);
  const traceLines = e.trace?.line_items || [];
  items.forEach((it, i) => {
    const tr = el("tr", "item");
    tr.append(el("td", "num", it.qty ?? "—"), el("td", "uom", it.uom || "—"),
      el("td", null, it.description), el("td", "src", it.source));
    const prov = provRow({ ...it, ...(traceLines[i] || {}) }, e.id);
    prov.hidden = true;
    tr.onclick = () => { prov.hidden = !prov.hidden; };
    t.append(tr, prov);
  });
  return t;
}

function provRow(it, emailId) {
  const tr = el("tr", "prov");
  const td = el("td"); td.colSpan = 4;
  td.append(el("div", "raw", "raw: " + it.raw));
  const ctx = it._context_from || it.context_from;
  if (ctx?.length) {
    td.append(el("div", "muted small", "context carried from:"));
    const ul = el("ul");
    for (const c of ctx) ul.append(el("li", null, c));
    td.append(ul);
  } else {
    td.append(el("div", "muted small", "no inherited context — the line stated everything itself"));
  }
  const row = el("div", "row");
  const a = el("button", "mini", "✓ accept");
  a.onclick = ev => { ev.stopPropagation(); decide(emailId, it, "accept", a); };
  const o = el("button", "mini", "✎ overturn");
  o.onclick = ev => {
    ev.stopPropagation();
    const now = prompt("What should this line say?", it.description);
    if (now !== null) decide(emailId, it, "overturn", o, now);
  };
  row.append(a, o, el("span", "muted small", "kept in ui/decisions.jsonl"));
  td.append(row);
  tr.append(td);
  return tr;
}

function decide(email, item, action, btn, now) {
  send("/api/decision", {
    email, raw: item.raw, action,
    was: { qty: item.qty, uom: item.uom, description: item.description },
    now: now ? { description: now } : null,
  }).then(() => { btn.classList.add("done"); btn.textContent = action === "accept" ? "✓ accepted" : "✎ overturned"; });
}

/* ------------------------------------------------------------------- wiring */

$("#picker").addEventListener("change", ev => { addFiles(ev.target.files); ev.target.value = ""; });
$("#process").addEventListener("click", process);
$("#reset").addEventListener("click", () => {
  send("/api/reset", { job: JOB }).then(() => {
    JOB = null; SNAP = null; SELECTED = null;
    $("#detail").hidden = true; $("#welcome").hidden = false;
    $("#queue-list").innerHTML = ""; $("#dropzone").style.display = "";
    $("#meter").innerHTML = ""; $("#reset").hidden = true;
    $("#process").disabled = true; $("#process").textContent = "Process";
    $("#queue-count").textContent = "";
  });
});

let depth = 0;
addEventListener("dragenter", e => { e.preventDefault(); if (++depth === 1) $("#veil").classList.add("on"); });
addEventListener("dragover", e => e.preventDefault());
addEventListener("dragleave", e => { e.preventDefault(); if (--depth <= 0) { depth = 0; $("#veil").classList.remove("on"); } });
addEventListener("drop", e => {
  e.preventDefault(); depth = 0; $("#veil").classList.remove("on");
  if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
});

stepsPreview({ steps: [
  { key: "ingest", title: "Open the envelope", note: "split what they wrote now from the thread quoted below it" },
  { key: "documents", title: "Read the attachments", note: "Excel, Word, CSV, PDF, photos — each becomes text or pictures" },
  { key: "classify", title: "What kind of email?", note: "one of seven. Four of them carry no items and stop here" },
  { key: "extract", title: "Read each document", note: "one call per document: which rows are items, what context they carry" },
  { key: "verify", title: "Check every line", note: "does it really occur in the document it claims to come from?" },
  { key: "link", title: "Place it in the inbox", note: "duplicate? revision? does a human need to look?" },
]});
