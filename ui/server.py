"""A local viewer: drop .eml files, watch them go through the pipeline, inspect each one.

Stdlib only, no build step, no new dependencies — `pip install -e .` stays the one
setup step. It imports rfq as a library and never writes to parsed.json, cache/ or
anything else the graded submission depends on.

    rfq-ui            # or: python ui/server.py
    → http://127.0.0.1:8765

Binds to loopback only. The brief forbids outbound side effects beyond the model
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

from rfq.cli import _load_dotenv, default_cache  # noqa: E402
from rfq.documents import documentize_all  # noqa: E402
from rfq.ingest import ingest  # noqa: E402
from rfq.pipeline import DEFAULT_CLASSIFY_MODEL, DEFAULT_EXTRACT_MODEL, DEFAULT_VISION_MODEL, run  # noqa: E402
from ui import rollup  # noqa: E402

JOBS: dict[str, dict] = {}
DECISIONS = HERE / "decisions.jsonl"
MAX_UPLOAD = 40 * 1024 * 1024


# --------------------------------------------------------------------------- #
# running an inbox
# --------------------------------------------------------------------------- #


def start_job(files: list[dict]) -> str:
    job_id = f"job-{int(time.time() * 1000)}"
    work = Path(tempfile.mkdtemp(prefix="rfq-ui-"))
    inbox = work / "emails"
    inbox.mkdir(parents=True)

    saved = 0
    for f in files:
        name = Path(f.get("name", "mail.eml")).name
        if not name.lower().endswith(".eml"):
            name += ".eml"
        try:
            (inbox / name).write_bytes(base64.b64decode(f["b64"]))
            saved += 1
        except Exception:
            continue

    JOBS[job_id] = {
        "id": job_id, "dir": str(work), "inbox": str(inbox), "total": saved,
        "state": "running", "started": time.time(), "error": None,
        "parsed": None, "requests": None, "cost": None, "traces": {},
    }
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


def _run_job(job_id: str) -> None:
    job = JOBS[job_id]
    try:
        _load_dotenv()
        parsed, cost, results = run(
            Path(job["inbox"]),
            cache_dir=default_cache(),
            api_key=__import__("os").getenv("OPENAI_API_KEY"),
            models={
                "classify": DEFAULT_CLASSIFY_MODEL,
                "extract": DEFAULT_EXTRACT_MODEL,
                "vision": DEFAULT_VISION_MODEL,
            },
            concurrency=6,
        )
        job["parsed"] = parsed
        job["cost"] = cost
        job["traces"] = {r.id: r.trace() for r in results}
        job["requests"] = rollup.build(parsed, Path(job["inbox"]))
        job["state"] = "done"
    except Exception as exc:
        job["state"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["trace"] = traceback.format_exc(limit=4)
    finally:
        job["elapsed"] = round(time.time() - job["started"], 1)


def email_detail(job: dict, eid: str) -> dict:
    """Everything the walkthrough needs for one email, assembled on demand."""
    path = Path(job["inbox"]) / f"{eid}.eml"
    out = {"id": eid, "trace": job["traces"].get(eid), "parsed": (job["parsed"] or {}).get(eid)}
    try:
        e = ingest(path)
    except Exception as exc:
        out["error"] = str(exc)
        return out

    out["envelope"] = {
        "from": f"{e.sender_name} <{e.sender_addr}>".strip(),
        "to": e.headers.get("to"),
        "date": e.headers.get("date"),
        "subject": e.subject,
        "from_us": e.from_us,
    }
    # The split, shown rather than described: the greyed half is what the
    # extractor never saw, which is the whole defence against quoted history.
    out["body"] = {"current": e.body_current, "quoted": e.body_quoted}

    docs = []
    for att in e.attachments:
        for d in documentize_all(att):
            docs.append({
                "source": d.source,
                "kind": d.kind,
                "error": d.error,
                "truncated": d.truncated,
                "meta": {k: v for k, v in d.meta.items() if not k.startswith("_")},
                "text": (d.text or "")[:40000],
                "images": [base64.b64encode(i).decode() for i in d.images[:6]],
                "media_type": d.media_type,
            })
    out["documents"] = docs
    return out


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "RidgelineUI/0.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

    def _static(self, name: str) -> None:
        p = (HERE / "static" / name).resolve()
        if not str(p).startswith(str((HERE / "static").resolve())) or not p.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        route = u.path

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])

        if route == "/api/status":
            job = JOBS.get(q.get("job", [""])[0])
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json({
                "state": job["state"], "total": job["total"], "error": job.get("error"),
                "elapsed": job.get("elapsed", round(time.time() - job["started"], 1)),
                "cost": job.get("cost"),
            })

        if route == "/api/requests":
            job = JOBS.get(q.get("job", [""])[0])
            if not job or job["state"] != "done":
                return self._json({"error": "not ready"}, 409)
            return self._json({"requests": job["requests"], "cost": job["cost"]})

        if route == "/api/email":
            job = JOBS.get(q.get("job", [""])[0])
            eid = q.get("id", [""])[0]
            if not job or job["state"] != "done":
                return self._json({"error": "not ready"}, 409)
            if eid not in (job["parsed"] or {}):
                return self._json({"error": "no such email"}, 404)
            return self._json(email_detail(job, eid))

        if route == "/api/decisions":
            rows = []
            if DECISIONS.exists():
                for line in DECISIONS.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
            return self._json({"decisions": rows})

        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json({"error": "upload too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if u.path == "/api/upload":
            files = payload.get("files") or []
            if not files:
                return self._json({"error": "no files"}, 400)
            return self._json({"job": start_job(files)})

        if u.path == "/api/decision":
            # A rep accepting or overturning a line. These are the only labels that
            # exist for mail we have no answer key for, so they are kept.
            row = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "email": payload.get("email"),
                "raw": payload.get("raw"),
                "action": payload.get("action"),
                "was": payload.get("was"),
                "now": payload.get("now"),
                "note": payload.get("note"),
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
    print("drop .eml files on the page.  ctrl-c to stop.")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        for job in JOBS.values():
            shutil.rmtree(job["dir"], ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
