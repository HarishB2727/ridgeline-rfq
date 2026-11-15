"""The model layer: one call site, content-addressed cache, cost ledger, replay.

Everything a model is ever asked goes through `ModelRunner.complete_json`, which:

  * hashes the request (model + prompt version + rendered text + image bytes),
  * returns a committed cached answer if one exists — so a second run costs zero
    and a run with no API key at all reproduces the first run exactly,
  * otherwise calls OpenAI with a strict JSON schema, retries transient failures,
    writes the answer to the cache and bills it to the ledger.

Cache keys are content addresses, not filenames.  Two emails with identical
bytes share an answer; editing a prompt invalidates every answer that used it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_VERSION = "v1"

# USD per 1M tokens.  Override with RFQ_PRICE_<MODEL>_IN / _OUT if these drift;
# the run always reports which table it used.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "o4-mini": (1.10, 4.40),
}


# We are run with someone else's key.  If it cannot reach the model we named, fall
# through to the next comparable one rather than failing the email.  The cache entry
# is always written under the model we ASKED for, so a later replay finds it whatever
# the account could actually serve.
FALLBACKS: dict[str, list[str]] = {
    "gpt-4.1": ["gpt-4o", "gpt-4.1-mini", "gpt-4o-mini"],
    "gpt-4o": ["gpt-4.1", "gpt-4o-mini", "gpt-4.1-mini"],
    "gpt-4.1-mini": ["gpt-4o-mini", "gpt-4.1", "gpt-4o"],
    "gpt-4o-mini": ["gpt-4.1-mini", "gpt-4o", "gpt-4.1"],
}
_MISSING_MODEL = ("model_not_found", "does not exist", "do not have access", "unsupported model")


class CacheMiss(RuntimeError):
    """No recorded answer, and no key to go and get one."""


class ModelUnavailable(RuntimeError):
    """The provider could not be reached after retries."""


@dataclass
class Ledger:
    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    by_email: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, email_id: str, *, model: str, pt: int, ct: int, cost: float, cached: bool) -> None:
        with self._lock:
            row = self.by_email.setdefault(
                email_id,
                {"calls": 0, "cache_hits": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "models": {}},
            )
            row["models"][model] = row["models"].get(model, 0) + 1
            if cached:
                row["cache_hits"] += 1
                self.cache_hits += 1
                return
            row["calls"] += 1
            row["prompt_tokens"] += pt
            row["completion_tokens"] += ct
            row["cost_usd"] = round(row["cost_usd"] + cost, 6)
            self.calls += 1
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.cost_usd = round(self.cost_usd + cost, 6)

    def summary(self, n_emails: int) -> dict:
        return {
            "emails": n_emails,
            "model_calls": self.calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "cost_per_email_usd": round(self.cost_usd / n_emails, 5) if n_emails else 0.0,
            "pricing_table_usd_per_1m": PRICING,
            "per_email": self.by_email,
        }


def price(model: str, pt: int, ct: int) -> float:
    key = model.lower()
    env_in = os.getenv(f"RFQ_PRICE_{key.replace('-', '_').replace('.', '_').upper()}_IN")
    env_out = os.getenv(f"RFQ_PRICE_{key.replace('-', '_').replace('.', '_').upper()}_OUT")
    if env_in and env_out:
        rin, rout = float(env_in), float(env_out)
    else:
        rin, rout = next(
            (v for k, v in PRICING.items() if key == k),
            next((v for k, v in PRICING.items() if key.startswith(k)), (0.0, 0.0)),
        )
    return (pt / 1_000_000) * rin + (ct / 1_000_000) * rout


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(data: bytes, media_type: str = "image/png") -> dict:
    return {"type": "image", "data": data, "media_type": media_type}


class ModelRunner:
    def __init__(
        self,
        cache_dir: Path,
        *,
        api_key: str | None = None,
        refresh: bool = False,
        prompt_version: str = PROMPT_VERSION,
        max_retries: int = 4,
        write_cache: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.prompt_version = prompt_version
        self.refresh = refresh
        self.max_retries = max_retries
        self.write_cache = write_cache
        self.ledger = Ledger()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or None
        self._client = None
        self._client_lock = threading.Lock()
        self._io_lock = threading.Lock()

    @property
    def offline(self) -> bool:
        return not self.api_key

    def _client_or_die(self):
        with self._client_lock:
            if self._client is None:
                from openai import OpenAI

                kwargs = {"api_key": self.api_key}
                if os.getenv("OPENAI_BASE_URL"):
                    kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
                self._client = OpenAI(**kwargs)
            return self._client

    # ------------------------------------------------------------------ cache

    def _key(self, model: str, system: str, parts: list[dict], schema_name: str) -> str:
        fp = {
            "prompt_version": self.prompt_version,
            "model": model,
            "schema": schema_name,
            "system": system,
            "parts": [
                {"t": "text", "v": p["text"]}
                if p["type"] == "text"
                else {"t": "image", "v": hashlib.sha256(p["data"]).hexdigest(), "m": p["media_type"]}
                for p in parts
            ],
        }
        blob = json.dumps(fp, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _path(self, model: str, key: str) -> Path:
        slug = model.replace("/", "_")
        return self.cache_dir / slug / key[:2] / f"{key}.json"

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, payload: dict) -> None:
        if not self.write_cache:
            return
        with self._io_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

    # ------------------------------------------------------------------- call

    def complete_json(
        self,
        *,
        email_id: str,
        model: str,
        system: str,
        parts: list[dict],
        schema: dict,
        schema_name: str,
        max_tokens: int = 8000,
    ) -> dict:
        key = self._key(model, system, parts, schema_name)
        path = self._path(model, key)

        if not self.refresh:
            hit = self._read(path)
            if hit and "response" in hit:
                u = hit.get("usage", {})
                self.ledger.record(
                    email_id,
                    model=model,
                    pt=u.get("prompt_tokens", 0),
                    ct=u.get("completion_tokens", 0),
                    cost=0.0,
                    cached=True,
                )
                return hit["response"]

        if self.offline:
            raise CacheMiss(
                f"no recorded model answer for this input ({schema_name}, {model}) and no OPENAI_API_KEY set"
            )

        content = []
        for p in parts:
            if p["type"] == "text":
                content.append({"type": "text", "text": p["text"]})
            else:
                b64 = base64.b64encode(p["data"]).decode()
                # "high" forces the full-resolution tiling path.  On a dense scanned
                # table the default downsamples enough that the model drifts between
                # rows and pairs a quantity with the wrong line.
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{p['media_type']};base64,{b64}", "detail": "high"},
                    }
                )

        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client_or_die().chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": content},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                    },
                    max_completion_tokens=max_tokens,
                )
                break
            except Exception as exc:  # transient: rate limit, 5xx, connection reset
                last = exc
                if attempt == self.max_retries - 1:
                    raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc
                time.sleep((2**attempt) + random.random())
        else:  # pragma: no cover
            raise ModelUnavailable(str(last))

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ModelUnavailable("model response was truncated (hit max_completion_tokens)")
        try:
            answer = json.loads(choice.message.content)
        except (TypeError, ValueError) as exc:
            raise ModelUnavailable(f"model returned unparseable JSON: {exc}") from exc

        usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
        }
        cost = price(model, usage["prompt_tokens"], usage["completion_tokens"])
        self.ledger.record(
            email_id, model=model, pt=usage["prompt_tokens"], ct=usage["completion_tokens"], cost=cost, cached=False
        )

        self._write(
            path,
            {
                "key": key,
                "prompt_version": self.prompt_version,
                "model": model,
                "schema": schema_name,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request": {
                    "system": system,
                    "parts": [
                        p["text"] if p["type"] == "text" else f"<image {hashlib.sha256(p['data']).hexdigest()[:16]} {p['media_type']}>"
                        for p in parts
                    ],
                },
                "usage": usage,
                "cost_usd": round(cost, 6),
                "response": answer,
            },
        )
        return answer
