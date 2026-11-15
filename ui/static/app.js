/* Ridgeline RFQ pipeline — the stages are the columns, each email is a row,
   and every cell opens the thing it is talking about.
   A window onto the pipeline, not a second implementation. */

const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x !== undefined) n.textContent = x; return n; };

/* the columns.  `step` is the pipeline stage a column shows, or null for the
   two that describe the input rather than a stage. */
const COLS = [
  { key: "who",        step: null, label: "Person / organisation", kind: null   },
  { key: "email",      step: null, label: "The email, as is",      kind: null   },
  { key: "code",       step: 1,    label: "What code made of it",  kind: "code" },
  { key: "docs",       step: 2,    label: "Attachments",           kind: "code" },
  { key: "classify",   step: 3,    label: "Classification",        kind: "ai"   },
  { key: "extract",    step: 4,    label: "Model reads documents", kind: "ai"   },
  { key: "verify",     step: 5,    label: "Check every line",      kind: "code" },
  { key: "outcome",    step: 6,    label: "Outcome",               kind: "code" },
];
const STEP_OF = { code: "ingest", docs: "documents", classify: "classify", extract: "extract", verify: "verify", outcome: "link" };

let JOB = null, SNAP = null, OPEN = null, PANEL = null, DETAIL = {}, timer = null;

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
  send("/api/process", { job: JOB, live: $("#live").checked }).then(() => poll());
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

/* ───────────────────────────────────────────────────────────── chrome */

function apply(s) {
  SNAP = s;
  $("#pipebar").classList.toggle("hidden", !s.order.length);
  $("#dropzone").classList.toggle("hidden", !!s.order.length);
  $("#reset").hidden = !s.order.length;
  $("#process").disabled = s.state === "running" || !s.order.length;
  const live = $("#live").checked;
  $("#process").textContent = s.state === "running" ? "Processing…"
    : s.state === "done" ? (live ? "Run live again" : "Process again")
    : live ? `Run ${s.order.length} live` : `Process ${s.order.length}`;
  $("#process").classList.toggle("warnbtn", live);
  $("#livebar").classList.toggle("hidden", !live || !s.order.length);
  const est = (s.cost?.cold_run?.cost_usd) || (s.order.length * 0.012);
  $("#live-note").textContent = s.order.length
    ? `every recorded answer is ignored — ${s.order.length} email${s.order.length > 1 ? "s" : ""} will be sent to OpenAI for real, roughly $${est.toFixed(2)}. The committed cache is left untouched.`
    : "";
  renderPipebar();
  renderMeter(s);
  renderGrid(s);
}

function renderPipebar() {
  const bar = $("#pipebar");
  if (bar.childElementCount) return;
  for (const c of COLS) {
    const h = el("div", `ph ${c.kind === "ai" ? "ai" : c.kind === "code" ? "codey" : "plain"}`);
    const n = el("div", "n");
    if (c.step) { n.append(`STEP ${c.step}`, el("span", "tag", c.kind === "ai" ? "model" : "code")); }
    else n.append("INPUT");
    h.append(n, el("div", "t", c.label));
    bar.append(h);
  }
}

function renderMeter(s) {
  const m = $("#meter"); m.innerHTML = "";
  const done = s.order.filter(i => s.emails[i].state === "done").length;
  const bits = [[`${done}/${s.order.length}`, "processed"]];
  if (s.cost) bits.push(
    [`${s.cost.model_calls}`, "calls"], [`${s.cost.cache_hits}`, "cached"],
    [`$${(s.cost.cold_run?.cost_usd ?? 0).toFixed(4)}`, "cold"]);
  if (s.state === "running") bits.push([`${s.elapsed}s`, ""]);
  for (const [v, l] of bits) { const x = el("span"); x.append(el("b", null, v)); if (l) x.append(" ", el("span", "muted", l)); m.append(x); }
}

/* ─────────────────────────────────────────────────────────────── grid */

/* Every cell reads the same way: the value itself as a pill on top, one line of
   plain English underneath, and a persistent "view" that opens the thing it is
   talking about.  Consistency matters more here than density — the point of the
   grid is that you can scan down a column without re-learning it each row. */

function pill(text, kind = "", title = "") {
  const p = el("span", `pill ${kind}`, text);
  if (title) p.title = title;
  return p;
}

function cellBox(key, id, stateCls, pills, sub, openable = true) {
  const c = el("div", `cell ${stateCls} ${openable ? "clickable" : ""}`);
  const top = el("div", "pills");
  for (const x of [].concat(pills)) if (x) top.append(typeof x === "string" ? pill(x) : x);
  c.append(top);
  if (sub) c.append(el("div", "sub", sub));
  if (openable) {
    const v = el("span", "view");
    v.append("view", el("span", "arrow", "›"));
    c.append(v);
    c.onclick = () => openPanel(id, key);
    if (OPEN === id && PANEL === key) c.classList.add("on");
  } else {
    c.append(el("span", "view off", "—"));
  }
  return c;
}

const stepCls = v => v === "skipped" ? "skipped" : v === "done" ? "" : "pending";

/* ───────────── grouping: a party, their requests, the thread under each ─────────── */

function normSubject(x) {
  let t = (x || "").trim();
  for (;;) {
    const y = t.replace(/^\s*(re|fw|fwd|resend|resending|reminder)\s*:\s*/i, "");
    if (y === t) break;
    t = y;
  }
  return t.replace(/\s+/g, " ").trim().toLowerCase();
}

function buildGroups(s) {
  const byId = Object.fromEntries(s.queue.map(q => [q.id, q]));
  const sum = id => (s.emails[id] || {}).summary || {};
  const when = id => Date.parse(byId[id]?.date || "") || 0;

  const parent = {};
  for (const q of s.queue) parent[q.id] = q.id;
  const find = i => parent[i] === i ? i : (parent[i] = find(parent[i]));
  const union = (a, b) => {
    if (!byId[a] || !byId[b]) return;
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent[when(rb) < when(ra) ? ra : rb] = when(rb) < when(ra) ? rb : ra;
  };

  /* once an email has been read, the pipeline's own links are the truth */
  for (const q of s.queue) {
    const m = sum(q.id);
    if (m.revises) union(m.revises, q.id);
    if (m.duplicate_of) union(m.duplicate_of, q.id);
  }
  /* before it has, the subject is the only hint there is.  Deliberately not
     applied afterwards: two different orders can share one generic subject,
     and the duplicate rule has already decided they are not the same request. */
  const seen = new Map();
  for (const q of s.queue) {
    if (sum(q.id).classification) continue;
    const k = `${q.customer_domain || q.domain}|${normSubject(q.subject)}`;
    if (seen.has(k)) union(seen.get(k), q.id); else seen.set(k, q.id);
  }

  const threads = new Map();
  for (const q of s.queue) {
    const r = find(q.id);
    if (!threads.has(r)) threads.set(r, []);
    threads.get(r).push(q.id);
  }
  for (const arr of threads.values()) arr.sort((x, y) => when(x) - when(y) || x.localeCompare(y));

  const parties = new Map();
  for (const members of threads.values()) {
    const q = byId[members[0]];
    const dom = q.customer_domain || q.domain || "?";
    if (!parties.has(dom)) parties.set(dom, { domain: dom, org: q.org, people: [], threads: [] });
    const p = parties.get(dom);
    for (const m of members) if (!p.people.includes(byId[m].person)) p.people.push(byId[m].person);
    p.threads.push(members);
  }
  for (const p of parties.values()) p.threads.sort((x, y) => when(x[0]) - when(y[0]));
  return [...parties.values()].sort((x, y) => when(x.threads[0][0]) - when(y.threads[0][0]));
}

function relationOf(s, id) {
  const m = (s.emails[id] || {}).summary || {};
  if (m.duplicate_of) return { label: "duplicate", cls: "chip" };
  if (m.revises) return { label: "revision", cls: "chip ok" };
  return { label: "follow-up", cls: "chip" };
}

function renderGrid(s) {
  const g = $("#grid");
  g.innerHTML = "";
  for (const party of buildGroups(s)) {
    g.append(groupHead(party, s));
    for (const members of party.threads) {
      members.forEach((id, i) => g.append(emailRow(s, id, i > 0 ? relationOf(s, id) : null, members)));
    }
  }
}

function groupHead(p, s) {
  const h = el("div", "ghead");
  const left = el("div", "gleft");
  left.append(pill(p.org || p.domain, "org"));
  left.append(pill(p.domain, "mono"));
  for (const person of p.people.slice(0, 3)) left.append(pill(person, "person"));
  h.append(left);

  const nMsg = p.threads.reduce((a, t) => a + t.length, 0);
  const lines = p.threads.reduce((a, t) => {
    const live = t.filter(i => !((s.emails[i] || {}).summary || {}).duplicate_of);
    const last = live[live.length - 1];
    return a + (((s.emails[last] || {}).summary || {}).n_items || 0);
  }, 0);
  const right = el("div", "gright");
  right.append(pill(`${p.threads.length} request${p.threads.length > 1 ? "s" : ""}`, "ghost"));
  if (nMsg !== p.threads.length) right.append(pill(`${nMsg} messages`, "ghost"));
  if (lines) right.append(pill(`${lines} lines to quote`, "ok"));
  if (p.threads.some(t => t.some(i => (((s.emails[i] || {}).summary) || {}).needs_review)))
    right.append(pill("needs review", "warn"));
  h.append(right);
  return h;
}

function emailRow(s, id, rel, members) {
  const q = s.queue.find(x => x.id === id);
  const e = s.emails[id] || {};
  const sum = e.summary || {};
  const st = e.steps || {};
  const busy = e.state === "running" || e.state === "linking";
  const rowState = busy ? "running" : sum.needs_review ? "review" : e.error ? "failed" : "";
  const row = el("div", `erow ${rel ? "child" : ""} ${OPEN === id ? "open" : ""} ${rowState}`);
  const addr = (q.sender || "").replace(/.*</, "").replace(/>/, "");
  const date = q.date ? new Date(q.date).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";

  /* 1 — which message */
  const who = el("div", "cell who");
  const top = el("div", "pills");
  top.append(pill(q.subject, "subject", q.subject));
  if (rel) top.append(pill(rel.label, rel.cls === "chip ok" ? "ok" : ""));
  if (q.from_us) top.append(pill("our rep", "warn"));
  who.append(top);
  who.append(el("div", "sub", `${id}${date ? " · " + date : ""}${rel ? "" : "  ·  " + (q.person || "")}`));
  if (busy) who.append(el("span", "dotpulse"));
  row.append(who);

  /* 2 — the email, as is */
  row.append(cellBox("email", id, "",
    pill(addr || "—", "mono", q.sender),
    `${q.body_chars ?? 0} chars · ${q.bytes ? (q.bytes / 1024).toFixed(1) + " KB on disk" : ""}`));

  /* 3 — what code made of it */
  row.append(cellBox("code", id, stepCls(st.ingest),
    [pill(`${q.body_chars ?? 0} chars kept`, "code"),
     q.quoted_chars ? pill(`${q.quoted_chars} cut`, "warn") : null],
    q.quoted_chars ? "thread history excluded from the model" : "no thread below — nothing to cut"));

  /* 4 — attachments, by name and what each became */
  const docs = sum.documents || [];
  const att = docs.filter(d => d.source !== "body");
  const names = att.length ? att : (q.attachments || []).map(n => ({ source: n, kind: null }));
  const kindOf = { text: "read as text", image: "read as pictures", mixed: "text + pictures",
                   unreadable: "could not be opened" };
  const counts = {};
  for (const d of att) counts[d.kind] = (counts[d.kind] || 0) + 1;
  const extracted = att.length
    ? Object.entries(counts).map(([k, v]) => `${v} ${kindOf[k] || k}`).join(", ")
    : (q.attachments || []).length ? "not read yet" : "nothing attached — the body is the whole request";
  row.append(cellBox("docs", id, stepCls(st.documents),
    names.length
      ? names.slice(0, 3).map(d => pill(d.source,
          d.kind === "unreadable" ? "warn" : "mono", d.source))
        .concat(names.length > 3 ? [pill(`+${names.length - 3}`, "")] : [])
      : pill("no attachments", "ghost"),
    extracted, names.length > 0));

  /* 5 — classification */
  const cc = (e.calls || []).filter(c => (c.label || "").startsWith("Decide"));
  const isItem = ["rfq", "rfq_revision", "purchase_order"].includes(sum.classification);
  row.append(cellBox("classify", id, stepCls(st.classify),
    pill(sum.classification || "not run", sum.classification ? (isItem ? "ok" : "ghost") : "ghost"),
    cc.length ? `${cc.length} model call${cc.length > 1 ? "s" : ""} · ${isItem ? "carries items" : "carries no items"}`
      : "waiting", cc.length > 0));

  /* 6 — model reads the documents */
  const xc = (e.calls || []).length - cc.length;
  const skipped = st.extract === "skipped";
  row.append(cellBox("extract", id, stepCls(st.extract),
    pill(skipped ? "skipped" : `${xc} model call${xc === 1 ? "" : "s"}`, skipped ? "ghost" : "ai"),
    skipped ? "the gate — nothing to read" : `${sum.n_items ?? 0} lines returned`, xc > 0));

  /* 7 — the line check */
  row.append(cellBox("verify", id, stepCls(st.verify),
    [pill(`${sum.n_items ?? 0} kept`, sum.n_items ? "ok" : "ghost"),
     sum.n_dropped ? pill(`${sum.n_dropped} dropped`, "warn") : null],
    skipped ? "nothing to check — this email carries no items"
      : sum.n_dropped ? "a line was not found in the document it claimed"
      : sum.n_items ? "every line found in the document it came from"
      : "nothing survived — see the outcome",
    e.state === "done"));

  /* 8 — outcome */
  const live = members ? members.filter(m => !(((s.emails[m] || {}).summary) || {}).duplicate_of
    && (((s.emails[m] || {}).summary) || {}).n_items) : [];
  const isCurrent = live.length > 1 && id === live[live.length - 1];
  const outPill = sum.needs_review ? pill("needs review", "warn")
    : sum.duplicate_of ? pill("duplicate", "ghost")
    : st.link === "done" ? pill(isCurrent ? "current order" : "ready to quote", "ok")
    : pill("waiting", "ghost");
  row.append(cellBox("outcome", id, stepCls(st.link), outPill,
    sum.duplicate_of ? `superseded by ${sum.duplicate_of}`
      : sum.needs_review ? (sum.review_reason || "").slice(0, 52)
      : sum.revises ? `the complete order, ${sum.n_items ?? 0} lines`
      : `${sum.n_items ?? 0} lines`, e.state === "done"));

  if (OPEN === id) {
    const d = el("div", "detail");
    d.id = "detail-" + id;
    d.append(el("div", "note", "loading…"));
    row.append(d);
    if (DETAIL[id]) renderPanel(DETAIL[id], d);
  }
  return row;
}

function openPanel(id, key) {
  if (OPEN === id && PANEL === key) { OPEN = null; PANEL = null; }
  else { OPEN = id; PANEL = key; }
  renderGrid(SNAP);
  if (OPEN) loadDetail(OPEN);
}

function loadDetail(id, force) {
  if (DETAIL[id] && !force) return;
  fetch(`/api/email?job=${encodeURIComponent(JOB)}&id=${encodeURIComponent(id)}`)
    .then(r => r.json()).then(e => {
      DETAIL[id] = e;
      const host = $("#detail-" + id);
      if (host) renderPanel(e, host);
    });
}

/* ───────────────────────────────────────────────────── panel plumbing */

function card(title, chips = [], warn) {
  const c = el("div", warn ? "card warncard" : "card");
  const h = el("div", "ch"); h.append(el("span", null, title));
  for (const ch of chips) h.append(ch);
  const b = el("div", "cb");
  c.append(h, b); c.body = b; return c;
}

function panelHead(e, key) {
  const col = COLS.find(c => c.key === key);
  const h = el("div", "phead");
  if (col.step) h.append(el("div", "num", String(col.step)));
  h.append(el("h3", null, col.label));
  if (col.kind) h.append(el("span", col.kind === "ai" ? "chip ai" : "chip code",
    col.kind === "ai" ? "model call" : "code only"));
  const x = el("button", "closep", "✕");
  x.onclick = () => { OPEN = null; PANEL = null; renderGrid(SNAP); };
  h.append(el("div", "spacer"), x);
  return h;
}

function renderPanel(e, host) {
  host.innerHTML = "";
  host.append(panelHead(e, PANEL));
  const body = el("div", "pbody");
  host.append(body);
  ({
    email: panelEmail, code: panelCode, docs: panelDocs, classify: panelClassify,
    extract: panelExtract, verify: panelVerify, outcome: panelOutcome,
  }[PANEL] || panelEmail)(e, body);
}

/* ─────────────────────────────────────────────────────────── panels */

function panelEmail(e, host) {
  const env = e.envelope || {};
  const c = card("the email, as it arrived");
  const kv = el("dl", "kv");
  for (const [k, v] of [["from", env.from], ["to", env.to], ["date", env.date], ["subject", env.subject]])
    if (v) kv.append(el("dt", null, k), el("dd", null, v));
  c.body.append(kv);
  const pre = el("pre", "txt"); pre.textContent = e.body?.current || "(empty body)";
  c.body.append(pre);
  if (e.body?.quoted) {
    const q = el("div", "quoted");
    const lbl = el("div", "lbl");
    lbl.append(el("span", "chip code", "step 1"), "thread history — never shown to the model");
    const p2 = el("pre", "txt"); p2.textContent = e.body.quoted;
    q.append(lbl, p2);
    c.body.append(q);
  }
  host.append(c);
}

function panelCode(e, host) {
  const env = e.envelope || {}, sig = e.signals || {};
  const cur = e.body?.current || "", quoted = e.body?.quoted || "";

  /* the structured record — this IS what step 1 produces */
  const c1 = card("the record code built from the raw bytes");
  const dl = el("dl", "record");
  const rows = [
    ["id", e.id],
    ["from", env.from],
    ["to", env.to],
    ["date", env.date],
    ["subject", env.subject],
    ["message-id", env.message_id],
    ["in-reply-to", env.in_reply_to],
    ["attachments", (sig.attachment_names || []).join(", ")],
    ["body_current", `${cur.length} chars`],
    ["body_quoted", `${quoted.length} chars` + (quoted ? "  (removed)" : "")],
  ];
  for (const [k, v] of rows) {
    dl.append(el("dt", null, k));
    dl.append(el("dd", v ? "" : "none", v || "—"));
  }
  c1.body.append(dl);
  host.append(c1);

  /* the actual text that goes forward */
  const c2 = card("body_current — the text every later step works on",
    [el("span", "chip code", `${cur.length} chars`)]);
  const p1 = el("pre", "txt"); p1.textContent = cur || "(empty)";
  c2.body.append(p1);
  host.append(c2);

  /* the actual text that was removed */
  if (quoted) {
    const c3 = card("body_quoted — cut here, never reaches a model",
      [el("span", "chip warn", `${quoted.length} chars`)], true);
    const p2 = el("pre", "txt"); p2.textContent = quoted;
    c3.body.append(p2);
    host.append(c3);
  }

  /* header signals, with what each one actually is */
  const MEANING = {
    from_us: "sender is one of our own addresses — a rep forwarding a customer in",
    bounce_envelope: "delivery-status headers: this is a bounce, whatever it contains",
    auto_submitted: "Auto-Submitted header is set — machine generated",
    list_unsubscribe: "bulk-mail headers or an unsubscribe link — marketing",
    precedence_bulk: "Precedence: bulk",
    mentions_attachment: "the body promises an attachment",
    mentions_picture: "the body refers to a picture",
    has_rfc822_attachment: "an entire email is attached to this one",
  };
  const found = Object.keys(sig).filter(k => k !== "attachment_names");
  const c4 = card("header signals code found", [el("span", "chip", `${found.length}`)]);
  if (!found.length) c4.body.append(el("div", "note", "None — nothing in the headers says anything unusual."));
  for (const k of found) {
    const f = el("div", "flag");
    f.append(el("span", "fk", k));
    f.append(el("span", "fv", MEANING[k] || String(sig[k])));
    c4.body.append(f);
  }
  c4.body.append(el("div", "note",
    "These are facts, not conclusions. They are handed to the classifier as evidence, and two of them — a bounce envelope, and nothing readable at all — can overrule whatever it answers."));
  host.append(c4);
}

const fmtBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

function panelDocs(e, host) {
  const docs = (e.documents || []);
  const roles = {};
  for (const t of (e.trace?.documents || [])) roles[t.source] = t.role;
  if (!docs.length) { const c = card("attachments"); c.body.append(el("div", "note", "None — everything came from the body.")); return host.append(c); }
  for (const doc of docs) {
    const chips = [el("span", doc.kind === "unreadable" ? "chip warn" : "chip", doc.kind)];
    if (roles[doc.source]) chips.push(el("span", "chip ok", roles[doc.source]));
    if (doc.truncated) chips.push(el("span", "chip warn", "truncated"));
    if (doc.bytes) chips.push(el("span", "chip", fmtBytes(doc.bytes)));
    const dl = el("a", "mini dl", "download");
    dl.href = `/api/attachment?job=${encodeURIComponent(JOB)}&id=${encodeURIComponent(e.id)}&name=${encodeURIComponent(doc.source)}`;
    dl.setAttribute("download", doc.source);
    chips.push(dl);
    const c = card(doc.source, chips, doc.kind === "unreadable");
    if (doc.error) c.body.append(el("div", null, doc.error));
    if (doc.meta && Object.keys(doc.meta).length)
      c.body.append(el("div", "note mono", JSON.stringify(doc.meta)));
    for (const b64 of doc.images || []) {
      const img = new Image(); img.src = `data:${doc.media_type};base64,${b64}`; img.className = "shot";
      c.body.append(img);
    }
    if (doc.text) {
      const det = el("details", "raw"); det.open = docs.length === 1;
      det.append(el("summary", null, "the text the model is given"));
      const p = el("pre", "txt"); p.textContent = doc.text;
      det.append(p); c.body.append(det);
    }
    host.append(c);
  }
}

function panelClassify(e, host) {
  const calls = (e.calls || []).filter(c => (c.schema || "").startsWith("email_classification"));
  if (!calls.length) { const c = card("not run yet"); return host.append(c); }
  for (const c of calls) host.append(callCard(c));
  const sum = e.summary || {};
  if (!["rfq", "rfq_revision", "purchase_order"].includes(sum.classification)) {
    const g = card("the gate");
    g.body.append(el("div", null,
      `“${sum.classification}” carries no line items, so the next column never ran. That is why this email cost almost nothing.`));
    host.append(g);
  }
}

function panelExtract(e, host) {
  const calls = (e.calls || []).filter(c => !(c.schema || "").startsWith("email_classification"));
  if (!calls.length) {
    const c = card("nothing read");
    c.body.append(el("div", "note", e.state === "done"
      ? "Skipped — this email carries no items. See the classification column." : "Not run yet."));
    return host.append(c);
  }
  for (const c of calls) host.append(callCard(c));
}

function panelVerify(e, host) {
  const dropped = e.trace?.dropped_lines || [];
  const c = card("verification", [el("span", dropped.length ? "chip warn" : "chip ok",
    dropped.length ? `${dropped.length} dropped` : "nothing dropped")]);
  c.body.append(el("div", "note",
    "Code, not the model. Every line must actually occur in the document it claims to come from; the unit must be one of the fourteen allowed; headings, totals and price rows are not items; a line that states no number keeps a null quantity."));
  for (const dl of dropped) {
    const b = el("div", "docrow");
    b.append(el("span", "docname", dl.raw));
    b.append(el("span", "chip warn", dl.dropped || dl.note));
    c.body.append(b);
  }
  host.append(c);
  const kept = card("what survived", [el("span", "chip ok", `${(e.result?.line_items || []).length} lines`)]);
  kept.body.append(itemsTable(e.result?.line_items || [], e));
  host.append(kept);
}

function panelOutcome(e, host) {
  const r = e.result || {};
  const c = card("outcome", [], r.needs_review);
  const o = el("div", "row");
  o.append(el("span", "chip ok", r.classification || "—"));
  if (r.duplicate_of) o.append(el("span", "chip", "duplicate of " + r.duplicate_of));
  if (r.revises) o.append(el("span", "chip", "revises " + r.revises));
  o.append(el("span", r.needs_review ? "chip warn" : "chip ok",
    r.needs_review ? "needs review" : "ready to quote"));
  c.body.append(o);
  if (r.review_reason) c.body.append(el("div", null, r.review_reason));
  const per = SNAP?.cost?.per_email?.[e.id];
  if (per) c.body.append(el("div", "note",
    `${per.calls + per.cache_hits} model call(s) · $${(per.cold_cost_usd ?? 0).toFixed(4)} cold`
    + (per.cache_hits ? ` · ${per.cache_hits} replayed from cache, $0.00 spent` : "")));
  host.append(c);
  const t = card("the lines a rep would quote", [el("span", "chip ok", `${(r.line_items || []).length}`)]);
  t.body.append(itemsTable(r.line_items || [], e));
  host.append(t);
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
    const t = el("table"); const hr = el("tr");
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
    "No line items. If this email needed someone to act, it is flagged in the outcome column.");
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
$("#live").addEventListener("change", () => { if (SNAP) apply(SNAP); });
$("#reset").addEventListener("click", () => send("/api/reset", { job: JOB }).then(() => {
  JOB = null; SNAP = null; OPEN = null; PANEL = null; DETAIL = {};
  $("#grid").innerHTML = ""; $("#meter").innerHTML = "";
  $("#pipebar").classList.add("hidden"); $("#dropzone").classList.remove("hidden");
  $("#reset").hidden = true; $("#process").disabled = true; $("#process").textContent = "Process";
}));
addEventListener("keydown", e => { if (e.key === "Escape" && OPEN) { OPEN = null; PANEL = null; renderGrid(SNAP); } });

let depth = 0;
addEventListener("dragenter", e => { e.preventDefault(); if (++depth === 1) $("#veil").classList.add("on"); });
addEventListener("dragover", e => e.preventDefault());
addEventListener("dragleave", e => { e.preventDefault(); if (--depth <= 0) { depth = 0; $("#veil").classList.remove("on"); } });
addEventListener("drop", e => { e.preventDefault(); depth = 0; $("#veil").classList.remove("on");
  if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files); });

renderPipebar();
$("#pipebar").classList.add("hidden");

const archBtn = $("#arch-toggle");
archBtn.onclick = () => {
  const a = $("#arch");
  a.classList.toggle("collapsed");
  archBtn.textContent = a.classList.contains("collapsed") ? "show" : "hide";
};
