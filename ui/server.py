"""A local viewer for the RFQ pipeline: drop .eml files, press Process, watch it work.

Stdlib only, no build step, no new dependencies.  Imports rfq as a library and
writes nothing the graded submission depends on.

    rfq-ui                 # or: python ui/server.py
    → http://127.0.0.1:8765

Binds to loopback only.  The brief forbids outbound side effects beyond the model
provider, and nothing here talks to anything else.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from rfq.documents import documentize_all  # noqa: E402
from rfq.ingest import ingest, ingest_bytes  # noqa: E402
from ui import engine, rollup  # noqa: E402

JOBS: dict[str, engine.Job] = {}
DECISIONS = HERE / "decisions.jsonl"
MAX_UPLOAD = 48 * 1024 * 1024


# --------------------------------------------------------------------------- #


def stage_files(files: list[dict], job_id: str | None = None) -> engine.Job:
    """Save uploads and read their headers.  No model call, nothing spent."""
    job = JOBS.get(job_id or "")
    if job is None or job.state == "running":
        work = Path(tempfile.mkdtemp(prefix="rfq-ui-"))
        inbox = work / "emails"
        inbox.mkdir(parents=True)
        job_id = f"job-{int(time.time() * 1000)}"
    else:
        inbox = job.inbox

    for f in files:
        name = Path(f.get("name", "mail.eml")).name
        if not name.lower().endswith(".eml"):
            name += ".eml"
        target = inbox / name
        n = 1
        while target.exists():
            target = inbox / f"{Path(name).stem}-{n}.eml"
            n += 1
        try:
            target.write_bytes(base64.b64decode(f["b64"]))
        except Exception:
            continue

    queue = engine.stage(job_id, inbox)
    fresh = engine.Job(job_id, inbox, queue)
    if job is not None and job.state in ("staged", "done"):
        # keep what has already been processed when more files are dropped in
        for eid, old in job.emails.items():
            if eid in fresh.emails and old["state"] == "done":
                fresh.emails[eid] = old
        fresh.cost = job.cost
        fresh.results = job.results
    JOBS[job_id] = fresh
    return fresh


def email_detail(job: engine.Job, eid: str) -> dict:
    """The email, the documents, every model call, and the result — for one email."""
    row = job.emails.get(eid) or {}
    out = {
        "id": eid,
        "state": row.get("state"),
        "summary": row.get("summary"),
        "result": row.get("result"),
        "trace": row.get("trace"),
        "error": row.get("error"),
        "calls": row.get("calls") or [],
    }
    try:
        e = ingest(job.inbox / f"{eid}.eml")
    except Exception as exc:
        out["error"] = f"could not read the email: {exc}"
        return out

    out["envelope"] = {
        "from": f"{e.sender_name} <{e.sender_addr}>".strip(),
        "to": e.headers.get("to"),
        "date": e.headers.get("date"),
        "subject": e.subject,
        "from_us": e.from_us,
        "message_id": e.headers.get("message_id"),
        "in_reply_to": e.headers.get("in_reply_to"),
    }
    out["body"] = {"current": e.body_current, "quoted": e.body_quoted}
    out["signals"] = {k: v for k, v in e.signals.items() if v}

    sizes = {n: len(v[0]) for n, v in attachment_bytes(job, eid).items()}
    docs = []
    for att in e.attachments:
        for d in documentize_all(att):
            docs.append({
                "source": d.source, "kind": d.kind, "error": d.error, "truncated": d.truncated,
                "meta": {k: v for k, v in d.meta.items() if not k.startswith("_")},
                "text": (d.text or "")[:60000],
                "bytes": sizes.get(d.source, 0),
                "images": [base64.b64encode(i).decode() for i in d.images[:8]],
                "media_type": d.media_type,
            })
    out["documents"] = docs
    return out


def attachment_bytes(job: engine.Job, eid: str) -> dict[str, tuple[bytes, str]]:
    """Every file this email carries, by name — including the ones inside a
    forwarded message, which is where the order often actually is."""
    found: dict[str, tuple[bytes, str]] = {}

    def walk(attachments, depth=0):
        for a in attachments:
            found.setdefault(a.filename, (a.data, a.mime))
            is_msg = a.mime == "message/rfc822" or a.filename.lower().endswith(".eml")
            if is_msg and depth < 3:
                raw = a.data
                try:
                    inner = ingest_bytes(raw, "inner")
                    if not (inner.headers.get("from") or inner.subject):
                        raise ValueError
                except Exception:
                    try:
                        inner = ingest_bytes(base64.b64decode(raw, validate=False), "inner")
                    except Exception:
                        continue
                walk(inner.attachments, depth + 1)

    try:
        walk(ingest(job.inbox / f"{eid}.eml").attachments)
    except Exception:
        pass
    return found


# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "RidgelineUI/0.2"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def _static(self, name):
        root = (HERE / "static").resolve()
        p = (root / name).resolve()
        if not str(p).startswith(str(root)) or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    def _job(self, q):
        return JOBS.get(q.get("job", [""])[0])

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._static("index.html")
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])

        if u.path == "/api/job":
            job = self._job(q)
            return self._json(job.snapshot() if job else {"error": "no such job"}, 200 if job else 404)

        if u.path == "/api/email":
            job = self._job(q)
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json(email_detail(job, q.get("id", [""])[0]))

        if u.path == "/api/call":
            # the full prompt and the full response for one model call
            job = self._job(q)
            eid, idx = q.get("id", [""])[0], int(q.get("n", ["0"])[0])
            calls = (job.emails.get(eid) or {}).get("calls") if job else None
            if not calls or idx >= len(calls):
                return self._json({"error": "no such call"}, 404)
            return self._json(calls[idx])

        if u.path == "/api/attachment":
            job = self._job(q)
            eid, name = q.get("id", [""])[0], q.get("name", [""])[0]
            if not job:
                return self._json({"error": "no such job"}, 404)
            hit = attachment_bytes(job, eid).get(name)
            if hit is None:
                return self._json({"error": "no such attachment"}, 404)
            data, mime = hit
            safe = Path(name).name.replace('"', "")
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/requests":
            job = self._job(q)
            if not job or job.state != "done":
                return self._json({"error": "not ready"}, 409)
            parsed = {r.id: r.to_json() for r in job.results}
            return self._json(rollup.build(parsed, job.inbox))

        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._json({"error": "upload too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if u.path == "/api/stage":
            files = payload.get("files") or []
            if not files:
                return self._json({"error": "no files"}, 400)
            return self._json(stage_files(files, payload.get("job")).snapshot())

        if u.path == "/api/process":
            job = JOBS.get(payload.get("job") or "")
            if not job:
                return self._json({"error": "no such job"}, 404)
            if job.state == "running":
                return self._json({"error": "already running"}, 409)
            engine.process(job)
            return self._json({"ok": True})

        if u.path == "/api/reset":
            job = JOBS.pop(payload.get("job") or "", None)
            if job:
                shutil.rmtree(job.inbox.parent, ignore_errors=True)
            return self._json({"ok": True})

        if u.path == "/api/decision":
            row = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "email": payload.get("email"), "raw": payload.get("raw"),
                "action": payload.get("action"), "was": payload.get("was"), "now": payload.get("now"),
            }
            with DECISIONS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Ridgeline RFQ viewer → {url}")
    print("drop .eml files, then press Process.  ctrl-c to stop.")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        for job in JOBS.values():
            shutil.rmtree(job.inbox.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
