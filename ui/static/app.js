/* Ridgeline RFQ desk — a viewer, not a second implementation.
   Everything shown here comes from the same pipeline `rfq parse` runs. */

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const esc = (s) => String(s ?? "");
const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? String(iso).slice(0, 16)
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

let JOB = null, DATA = null, ACTIVE = null;

/* ---------------------------------------------------------------- upload */

function readFiles(fileList) {
  const files = [...fileList].filter(f => /\.eml$/i.test(f.name) || f.type === "message/rfc822");
  if (!files.length) { alert("Those don't look like .eml files."); return; }
  Promise.all(files.map(f => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res({ name: f.name, b64: String(r.result).split(",", 2)[1] });
    r.onerror = rej;
    r.readAsDataURL(f);
  }))).then(payload => {
    showProgress(`reading ${payload.length} email${payload.length > 1 ? "s" : ""}…`);
    return fetch("/api/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: payload })
    });
  }).then(r => r.json()).then(j => {
    if (j.error) throw new Error(j.error);
    JOB = j.job;
    poll();
  }).catch(e => { hideProgress(); alert("Upload failed: " + e.message); });
}

function poll() {
  fetch(`/api/status?job=${encodeURIComponent(JOB)}`).then(r => r.json()).then(s => {
    if (s.state === "running") {
      showProgress(`reading ${s.total} email${s.total > 1 ? "s" : ""}… ${s.elapsed}s`);
      return setTimeout(poll, 700);
    }
    hideProgress();
    if (s.state === "error") return alert("The run failed: " + s.error);
    loadRequests();
  }).catch(() => setTimeout(poll, 1200));
}

function loadRequests() {
  fetch(`/api/requests?job=${encodeURIComponent(JOB)}`).then(r => r.json()).then(j => {
    DATA = j;
    $("#empty").style.display = "none";
    renderTotals(j.requests.totals, j.cost);
    renderSidebar(j.requests);
  });
}

function showProgress(text) { $("#progress-text").textContent = text; $("#progress").classList.remove("hidden"); }
function hideProgress() { $("#progress").classList.add("hidden"); }

/* ---------------------------------------------------------------- header */

function renderTotals(t, cost) {
  const box = $("#totals");
  box.innerHTML = "";
  const cold = cost?.cold_run || {};
  const bits = [
    [`${t.emails}`, "emails"],
    [`${t.requests}`, "requests"],
    [`${t.customers}`, "customers"],
    [`${t.line_items}`, "line items"],
    [`${t.needs_review}`, "need review"],
    [cost?.model_calls === 0 ? "cached" : `${cost?.model_calls ?? 0} calls`,
     cost?.model_calls === 0 ? "$0.00 this run" : `$${(cost?.cost_usd ?? 0).toFixed(4)}`],
  ];
  for (const [n, label] of bits) {
    const s = el("span");
    s.append(el("b", null, n), " ", el("span", "muted", label));
    box.append(s);
  }
  if (cold.cost_usd) box.append(el("span", "muted", `cold: $${cold.cost_usd.toFixed(4)}`));
}

/* --------------------------------------------------------------- sidebar */

const GROUPS = [
  ["customer", "customers"],
  ["supplier", "suppliers — quoting us"],
  ["none", "no action — bounces, marketing"],
];

function renderSidebar(roll) {
  const box = $("#parties");
  box.innerHTML = "";
  for (const [type, label] of GROUPS) {
    const parties = roll.parties.filter(p => p.type === type);
    if (!parties.length) continue;
    box.append(el("div", "group-head", label));
    for (const p of parties) {
      const wrap = el("div", "party");
      wrap.append(el("div", "party-name", p.domain));
      for (const r of p.requests) {
        const b = el("button", "req");
        b.dataset.key = r.key;
        const l1 = el("div", "req-line1");
        l1.append(el("span", "req-subject", r.subject || "(no subject)"));
        l1.append(el("span", `chip ${r.status.replace(" ", "-")}`, r.status));
        if (r.needs_review) l1.append(el("span", "chip review", "review"));
        b.append(l1);
        const meta = [`${r.n_items} items`, r.ref ? `ref ${r.ref}` : null, fmtDate(r.last_activity)]
          .filter(Boolean).join(" · ");
        b.append(el("div", "req-meta", meta));
        b.onclick = () => { ACTIVE = r; markActive(r.key); renderRequest(r); };
        wrap.append(b);
      }
      box.append(wrap);
    }
  }
}

function markActive(key) {
  document.querySelectorAll(".req").forEach(n => n.classList.toggle("active", n.dataset.key === key));
}

/* -------------------------------------------------------- request detail */

function renderRequest(r) {
  const d = $("#detail");
  d.innerHTML = "";
  d.append(el("h2", null, r.subject || "(no subject)"));
  const sub = el("div", "sub");
  sub.textContent = [r.domain, r.ref ? `their ref ${r.ref}` : null, r.our_quote ? `our ${r.our_quote}` : null,
    `opened ${fmtDate(r.opened)}`].filter(Boolean).join("  ·  ");
  d.append(sub);

  if (r.needs_review) {
    const c = el("div", "card warn");
    c.append(el("strong", null, "A human has to look at this before anyone quotes it."));
    for (const reason of r.review_reasons) c.append(el("div", null, reason));
    d.append(c);
  }

  /* the thread */
  d.append(headed("the thread", `${r.timeline.length} message${r.timeline.length > 1 ? "s" : ""}`));
  const tl = el("div", "timeline");
  for (const e of r.timeline) {
    const b = el("button", "tl");
    b.append(el("span", "id", e.id));
    b.append(el("span", "when", fmtDate(e.date)));
    const what = [e.classification,
      e.duplicate_of ? `duplicate of ${e.duplicate_of}` : null,
      e.revises ? `revises ${e.revises}` : null,
      `${e.n_items} items`,
      (e.attachments || []).join(", ")].filter(Boolean).join("  ·  ");
    b.append(el("span", "what", what));
    if (e.needs_review) b.append(el("span", "chip review", "review"));
    b.onclick = () => openEmail(e.id, r);
    tl.append(b);
  }
  d.append(tl);

  /* what a revision changed */
  if (r.changes && r.changes.length) {
    d.append(headed("what changed", `applied from ${r.current_from}`));
    const c = el("div", "card");
    for (const ch of r.changes) {
      const row = el("div", `change ${ch.kind}`);
      row.append(el("span", "k", ch.kind));
      row.append(document.createTextNode(ch.text));
      c.append(row);
    }
    d.append(c);
  }

  /* the order as it now stands */
  d.append(headed("the order as it now stands", `${r.n_items} lines, from ${r.current_from}`));
  d.append(itemsTable(r.line_items, r.current_from));
}

function headed(label, n) {
  const h = el("h3", null, label + " ");
  if (n) h.append(el("span", "n", `— ${n}`));
  return h;
}

function itemsTable(items, emailId) {
  if (!items || !items.length) {
    return el("div", "card muted", "No line items. If this email needed someone to act, it is flagged above.");
  }
  const t = el("table");
  const head = el("tr");
  for (const h of ["qty", "uom", "description", "source"]) head.append(el("th", null, h));
  t.append(el("thead")).firstChild.append(head);
  const body = el("tbody");
  items.forEach((it, i) => {
    const tr = el("tr", "item");
    tr.append(el("td", "num", it.qty === null || it.qty === undefined ? "—" : String(it.qty)));
    tr.append(el("td", "uom", it.uom || "—"));
    tr.append(el("td", null, it.description));
    tr.append(el("td", "src", it.source));
    const prov = provenanceRow(it, emailId);
    prov.style.display = "none";
    tr.onclick = () => { prov.style.display = prov.style.display === "none" ? "" : "none"; };
    body.append(tr, prov);
  });
  t.append(body);
  return t;
}

function provenanceRow(it, emailId) {
  const tr = el("tr", "prov");
  const td = el("td");
  td.colSpan = 4;
  td.append(el("div", "raw", `raw: ${it.raw}`));

  const ctx = it._context_from || it.context_from;
  if (ctx && ctx.length) {
    td.append(el("div", "muted", "context carried from:"));
    const ul = el("ul");
    for (const c of ctx) ul.append(el("li", null, c));
    td.append(ul);
  } else {
    td.append(el("div", "muted", "no inherited context — the line stated everything itself"));
  }
  if (it._notes) for (const n of it._notes) td.append(el("div", "muted", "note: " + n));

  const row = el("div", "row");
  const accept = el("button", "mini", "✓ accept");
  accept.onclick = (ev) => { ev.stopPropagation(); decide(emailId, it, "accept", accept); };
  const wrong = el("button", "mini", "✎ overturn");
  wrong.onclick = (ev) => {
    ev.stopPropagation();
    const note = prompt("What should this line say?", it.description);
    if (note !== null) decide(emailId, it, "overturn", wrong, note);
  };
  row.append(accept, wrong, el("span", "muted", "decisions are kept in ui/decisions.jsonl"));
  td.append(row);
  tr.append(td);
  return tr;
}

function decide(emailId, item, action, btn, now) {
  fetch("/api/decision", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: emailId, raw: item.raw, action,
      was: { qty: item.qty, uom: item.uom, description: item.description },
      now: now ? { description: now } : null,
    })
  }).then(() => { btn.classList.add("done"); btn.textContent = action === "accept" ? "✓ accepted" : "✎ overturned"; });
}

/* ------------------------------------------------- one email, stage by stage */

function openEmail(id, req) {
  fetch(`/api/email?job=${encodeURIComponent(JOB)}&id=${encodeURIComponent(id)}`)
    .then(r => r.json()).then(e => renderEmail(e, req));
}

function stage(n, title, note) {
  const s = el("section", "stage");
  s.dataset.n = n;
  s.append(headed(title, note));
  s.firstChild.style.marginTop = "0";
  return s;
}

function renderEmail(e, req) {
  const d = $("#detail");
  d.innerHTML = "";
  const back = el("button", "mini", "← back to the request");
  back.onclick = () => renderRequest(req);
  d.append(back);

  const env = e.envelope || {};
  d.append(el("h2", null, `${e.id} — ${env.subject || "(no subject)"}`));
  d.append(el("div", "sub", `${env.from || ""}  ·  ${env.date || ""}`));

  /* 1 — envelope and the split */
  const s1 = stage(1, "the message", env.from_us ? "from one of our own reps" : null);
  const cur = el("pre", "body");
  cur.textContent = e.body?.current || "(empty)";
  s1.append(cur);
  if (e.body?.quoted) {
    s1.append(el("div", "quoted-label",
      "▼ thread history below — this was NOT given to the extractor. It was handled when it arrived."));
    const q = el("pre", "body quoted");
    q.textContent = e.body.quoted;
    s1.append(q);
  }
  d.append(s1);

  /* 2 — documents */
  const docs = e.documents || [];
  const s2 = stage(2, "documents found", `${docs.length} attachment${docs.length === 1 ? "" : "s"}`);
  if (!docs.length) s2.append(el("div", "card muted", "No attachments — everything came from the body."));
  const roles = {};
  for (const dd of (e.trace?.documents || [])) roles[dd.source] = dd.role;
  for (const doc of docs) {
    const c = el("div", doc.kind === "unreadable" ? "card warn" : "card");
    const head = el("div", "row");
    head.append(el("strong", null, doc.source));
    head.append(el("span", "chip", doc.kind));
    if (roles[doc.source]) head.append(el("span", "chip", roles[doc.source]));
    if (doc.truncated) head.append(el("span", "chip review", "truncated"));
    c.append(head);
    if (doc.error) c.append(el("div", null, doc.error));
    if (doc.meta && Object.keys(doc.meta).length) {
      c.append(el("div", "muted mono", JSON.stringify(doc.meta)));
    }
    for (const b64 of doc.images || []) {
      const img = new Image();
      img.src = `data:${doc.media_type};base64,${b64}`;
      img.className = "doc-img";
      c.append(img);
    }
    if (doc.text) {
      const det = el("details", "doc");
      det.append(el("summary", null, "what the model was given to read"));
      const pre = el("pre", "body");
      pre.textContent = doc.text;
      det.append(pre);
      c.append(det);
    }
    s2.append(c);
  }
  d.append(s2);

  /* 3 — classification */
  const t = e.trace || {};
  const s3 = stage(3, "classification", t.classification);
  const c3 = el("div", "card");
  c3.append(el("div", null, t.why || ""));
  if (t.classifier?.code_override) {
    c3.append(el("div", "muted", "code override: " + t.classifier.code_override));
  }
  if (t.classifier?.escalated) {
    c3.append(el("div", "muted", "escalated for a second look: " + t.classifier.escalated.reason));
  }
  if (!["rfq", "rfq_revision", "purchase_order"].includes(t.classification)) {
    c3.append(el("div", "muted", "This class carries no line items, so extraction was skipped — that is the cost gate."));
  }
  s3.append(c3);
  d.append(s3);

  /* 4 — lines kept */
  const items = e.parsed?.line_items || [];
  const s4 = stage(4, "line items kept", `${items.length}`);
  s4.append(itemsTable(items.map((it, i) => ({ ...it, ...(t.line_items?.[i] || {}) })), e.id));
  d.append(s4);

  /* 5 — lines dropped */
  const dropped = t.dropped_lines || [];
  const s5 = stage(5, "dropped by verification", `${dropped.length}`);
  if (!dropped.length) {
    s5.append(el("div", "card muted", "Nothing was dropped — every line the model returned was found in its source."));
  } else {
    for (const dl of dropped) {
      const c = el("div", "card");
      c.append(el("div", "mono", dl.raw));
      c.append(el("div", "muted", "→ " + (dl.dropped || dl.note)));
      s5.append(c);
    }
  }
  d.append(s5);

  /* 6 — review + cost */
  const s6 = stage(6, "outcome");
  const c6 = el("div", e.parsed?.needs_review ? "card warn" : "card");
  c6.append(el("div", null, e.parsed?.needs_review
    ? "Flagged: " + (e.parsed.review_reason || "")
    : "Ready to quote — nothing unreadable, nothing unresolved."));
  const per = DATA?.cost?.per_email?.[e.id];
  if (per) {
    c6.append(el("div", "muted",
      `${per.calls + per.cache_hits} model call(s) · $${(per.cold_cost_usd ?? 0).toFixed(4)} cold` +
      (per.cache_hits ? ` · ${per.cache_hits} replayed from cache` : "")));
  }
  s6.append(c6);
  d.append(s6);
}

/* ------------------------------------------------------------------ wiring */

$("#picker").addEventListener("change", ev => readFiles(ev.target.files));

let dragDepth = 0;
window.addEventListener("dragenter", e => { e.preventDefault(); if (++dragDepth === 1) $("#drop-veil").classList.add("on"); });
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("dragleave", e => { e.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; $("#drop-veil").classList.remove("on"); } });
window.addEventListener("drop", e => {
  e.preventDefault();
  dragDepth = 0;
  $("#drop-veil").classList.remove("on");
  if (e.dataTransfer?.files?.length) readFiles(e.dataTransfer.files);
});
