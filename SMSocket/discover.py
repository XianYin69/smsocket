"""Model discovery: ask a provider what it really serves, then prove it.

Flow per provider (all of it parallel, bounded by `concurrency`):

  1. GET /models            -> upstream model ids (+ context_length / created if given)
  2. POST a tiny chat per id -> is the key allowed to use it, how fast, how many tokens
  3. optional param probe    -> for each accepted model, send one request per parameter
     (temperature, top_p, tools, response_format, ...) and classify the answer:
        supported  2xx
        rejected   4xx whose message names the parameter
        unknown    5xx / network (cannot tell)
  4. optional stream + embeddings probe

Nothing here mutates the gateway config; `apply` in the route does that after a
report exists. Results are cached in the usage database (`models` table) so the
dashboard can show the catalog without re-spending tokens.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from . import upstream as up
from .config import ProviderSpec, Settings
from .providers import mask, learn_rate_limit

log = logging.getLogger("smssocket")

# param -> sample value that is valid for a chat completion
CHAT_PARAMS: dict[str, object] = {
    "temperature": 0.3,
    "top_p": 0.9,
    "max_tokens": 16,
    "max_completion_tokens": 16,
    "n": 1,
    "stop": ["\n"],
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "seed": 1234,
    "logprobs": True,
    "response_format": {"type": "text"},
    "reasoning_effort": "low",
    "tools": [{"type": "function", "function": {
        "name": "get_time", "description": "Current time",
        "parameters": {"type": "object", "properties": {}}}}],
}
TEST_PROMPT = "Reply with exactly one word: ok"
MAX_MODELS = 200


@dataclass
class ProbeResult:
    provider: str
    model: str
    ok: bool = False
    status: int = 0
    latency_ms: float = 0.0
    usage: dict = field(default_factory=dict)
    reply: str = ""
    error: str = ""
    context: object = None
    created: object = None
    owned_by: str = ""
    params: dict = field(default_factory=dict)      # name -> supported|rejected|unknown
    param_detail: dict = field(default_factory=dict)
    stream: str = ""                                 # supported|rejected|unknown
    embeddings: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "ok": self.ok,
                "status": self.status, "latency_ms": self.latency_ms,
                "usage": self.usage, "reply": self.reply, "error": self.error,
                "context": self.context, "created": self.created,
                "owned_by": self.owned_by, "params": self.params,
                "param_detail": self.param_detail, "stream": self.stream,
                "embeddings": self.embeddings}


import re

_V = re.compile(r"/v\d+$")


def models_url(p: ProviderSpec) -> str:
    base = p.base_url.rstrip("/")
    return base + "/models" if _V.search(base) else base + "/v1/models"


def list_headers(p: ProviderSpec, key: str) -> dict:
    """Headers for GET /models - keyless providers (OpenCode free models) need no auth."""
    if p.style == "anthropic":
        h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        if key:
            h["x-api-key"] = key
        return h
    if p.style == "opencode":
        # the /models listing is also behind the CLI identity gate
        h = up._opencode_headers(key)
        h["content-type"] = "application/json"
        return h
    h = {"content-type": "application/json"}
    if key:
        h["authorization"] = f"Bearer {key}"
    return h


def parse_model_list(data) -> list[dict]:
    """Normalise OpenAI / Anthropic model listings into [{id, context, created, owned_by}]."""
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = row.get("id") or row.get("model") or row.get("name")
        if not mid:
            continue
        meta = row.get("model_metadata") or row.get("meta") or {}
        ctx = (row.get("context_length") or row.get("context_window")
               or (meta.get("context_window") if isinstance(meta, dict) else None)
               or row.get("max_context_window"))
        out.append({"id": str(mid), "context": ctx,
                    "created": row.get("created"),
                    "owned_by": str(row.get("owned_by") or row.get("owner") or "")})
    return out


def _err_msg(data) -> str:
    """从响应里取错误消息，兼容 dict / str / None 三种 error 格式。"""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")
    if isinstance(err, str):
        return err
    return str(data.get("message") or "")


def classify(status: int, message: str, param: str) -> str:
    """supported / rejected / unknown for one probed parameter."""
    m = (message or "").lower()
    if 200 <= status < 300:
        return "supported"
    if status in (400, 404, 422):
        if param.replace("_", "") in m.replace("_", "").replace(" ", "") or \
           param in m or "unsupport" in m or "not supported" in m or \
           "invalid" in m or "unknown" in m or "unexpected" in m or "extra" in m:
            return "rejected"
        return "rejected"          # 4xx on a param-only payload => provider refused it
    return "unknown"


class Discoverer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient,
                 catalog: Catalog | None = None) -> None:
        self.s = settings
        self.http = client
        self.catalog = catalog

    # ---- low level ---------------------------------------------------------
    async def _call(self, p: ProviderSpec, key: str, body: dict,
                    timeout: float) -> tuple[int, dict, str, float]:
        t0 = time.time()
        try:
            r = await self.http.post(up.chat_url(p), headers=up.headers(p, key),
                                     json=body, timeout=timeout)
            text = r.text[:400]
            try:
                data = r.json()
            except Exception:
                data = {}
            return r.status_code, data, text, round((time.time() - t0) * 1000, 1)
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            return 0, {}, f"{type(e).__name__}: {e}", round((time.time() - t0) * 1000, 1)

    async def list_models(self, p: ProviderSpec, key: str, timeout: float) -> list[dict]:
        try:
            r = await self.http.get(models_url(p), headers=list_headers(p, key),
                                    timeout=timeout)
            if r.status_code >= 400:
                return []
            return parse_model_list(r.json())
        except (httpx.RequestError, asyncio.TimeoutError, ValueError):
            return []

    def _denied(self, provider: str) -> set:
        """Model ids the operator rejected - the discoverer never probes them again.

        The catalog lives in the same sqlite file as the usage log, so the
        denial set is read fresh on every probe; a denial outlives the process.
        """
        if self.catalog is None:
            return set()
        try:
            return {m for (_prov, m) in self.catalog.denied(provider)}
        except Exception:                                            # noqa: BLE001
            return set()

    # ---- one model ---------------------------------------------------------
    async def probe_model(self, p: ProviderSpec, key: str, model: str, *,
                          test_prompt: str, probe_params: bool, probe_stream: bool,
                          timeout: float, max_params: int) -> ProbeResult:
        res = ProbeResult(provider=p.name, model=model)
        base = {"model": model, "messages": [{"role": "user", "content": test_prompt}]}
        if p.style == "anthropic":
            base = up.build_payload(p, base, model)
        status, data, text, ms = await self._call(p, key, base, timeout)
        res.status, res.latency_ms = status, ms
        if 200 <= status < 300:
            res.ok = True
            if p.style == "anthropic":
                oai = up.from_anthropic(data, model)
                res.usage = oai.get("usage") or {}
                res.reply = (oai["choices"][0]["message"]["content"] or "")[:80]
            else:
                res.usage = data.get("usage") or {}
                try:
                    res.reply = ((data["choices"][0]["message"]["content"]) or "")[:80]
                except Exception:
                    res.reply = ""
        else:
            res.error = _err_msg(data) or text[:200]
            return res                      # no point probing a model we cannot call

        if probe_params:
            for name, value in list(CHAT_PARAMS.items())[:max_params]:
                body = dict(base)
                if p.style == "anthropic" and name in ("max_tokens",):
                    body["max_tokens"] = 16
                    continue
                body[name] = value
                st2, d2, t2, ms2 = await self._call(p, key, body, timeout)
                msg = ""
                if isinstance(d2, dict):
                    msg = str((d2.get("error") or {}).get("message") or d2.get("message") or "")
                verdict = classify(st2, msg, name)
                res.params[name] = verdict
                res.param_detail[name] = {"status": st2, "ms": ms2,
                                          **({"message": msg[:200]} if msg else {})}
        if probe_stream and p.style != "anthropic":
            body = dict(base)
            body["stream"] = True
            try:
                async with self.http.stream("POST", up.chat_url(p),
                                            headers=up.headers(p, key), json=body,
                                            timeout=timeout) as r:
                    if r.status_code >= 400:
                        await r.aread()
                        res.stream = classify(r.status_code, r.text[:200], "stream")
                    else:
                        got = False
                        async for chunk in r.aiter_bytes():
                            if b"data:" in chunk:
                                got = True
                                break
                        res.stream = "supported" if got else "unknown"
            except (httpx.RequestError, asyncio.TimeoutError):
                res.stream = "unknown"
        return res

    # ---- one provider ------------------------------------------------------
    async def probe_provider(self, p: ProviderSpec, *, models: list[str] | None = None,
                             only_listed: bool = True, test_prompt: str = TEST_PROMPT,
                             probe_params: bool = True, probe_stream: bool = True,
                             probe_embeddings: bool = False, concurrency: int = 8,
                             timeout: float = 30.0, max_models: int = MAX_MODELS,
                             max_params: int = len(CHAT_PARAMS),
                             progress=None) -> dict:
        keys = [k for k in p.keys if k]
        # A keyless provider (OpenCode serves its free models without any
        # credential) is probed with an empty key - headers() then omits the
        # Authorization header entirely instead of sending a bogus "Bearer ".
        if not keys and getattr(p, "keyless", False):
            keys = [""]
        if not keys:
            return {"provider": p.name, "error": "no api key configured", "models": []}
        sem = asyncio.Semaphore(max(1, concurrency))
        denied = self._denied(p.name)
        listed = await self.list_models(p, keys[0], timeout)
        ids = [m["id"] for m in listed if m["id"] not in denied]
        meta = {m["id"]: m for m in listed}
        if models:
            ids = [i for i in ids if i in set(models)] or list(models)
        elif only_listed and not ids:
            ids = [i for i in dict.fromkeys(list(p.models.values()) + list(p.embeddings.values()))
                   if i not in denied]
        else:
            ids = [i for i in dict.fromkeys(ids + list(p.models.values())) if i not in denied]
        ids = ids[:max_models]

        async def one(model_id: str) -> ProbeResult:
            async with sem:
                r = await self.probe_model(
                    p, keys[0], model_id, test_prompt=test_prompt,
                    probe_params=probe_params, probe_stream=probe_stream,
                    timeout=timeout, max_params=max_params)
                m = meta.get(model_id, {})
                r.context, r.created, r.owned_by = (m.get("context"), m.get("created"),
                                                    m.get("owned_by") or "")
                if probe_embeddings and r.ok:      # already inside `sem`
                    r.embeddings = await self._probe_embed(p, keys[0], model_id, timeout)
                if progress:
                    progress(r)
                return r

        t0 = time.time()
        results = await asyncio.gather(*[one(i) for i in ids], return_exceptions=True)
        rows = []
        for r in results:
            if isinstance(r, Exception):
                rows.append(ProbeResult(provider=p.name, model="-", error=str(r)[:200]).as_dict())
            else:
                rows.append(r.as_dict())
        return {"provider": p.name, "base_url": p.base_url, "style": p.style,
                "listed": len(listed), "probed": len(rows),
                "ok": sum(1 for r in rows if r["ok"]),
                "failed": sum(1 for r in rows if not r["ok"]),
                "wall_ms": round((time.time() - t0) * 1000, 1),
                "key": mask(keys[0]), "models": rows}

    async def _probe_embed(self, p: ProviderSpec, key: str, model: str,
                            timeout: float) -> str:
        try:
            r = await self.http.post(up.embed_url(p), headers=up.headers(p, key),
                                     json={"model": model, "input": "ping"},
                                     timeout=timeout)
            return classify(r.status_code, r.text[:200], "embeddings")
        except (httpx.RequestError, asyncio.TimeoutError):
            return "unknown"

    # ---- rate limit (RPM) learning -----------------------------------------
    async def probe_rate_limit(self, p: ProviderSpec, *,
                               burst: int = 40,
                               timeout: float = 30.0,
                               model: str | None = None,
                               test_prompt: str = TEST_PROMPT) -> dict:
        """Fire a controlled burst; when the provider answers 429, learn its RPM.

        A free-tier cap of 20/min needs ~21 requests to hit, so `burst` (default
        40) comfortably crosses it.  Every 429 is fed into `learn_rate_limit`,
        which reads `x-ratelimit-limit*` headers first, then a figure named in
        the error body, and writes the result onto `p.max_rpm` - the shared
        ProviderSpec every key slot reads.  Returns a report:

            {"provider": name, "burst": n, "limit_hits": k,
             "max_rpm": learned or None, "detail": "..."}

        When no 429 arrives within the burst the provider is below the cap at
        this moment; the cap is left untouched (`max_rpm: None`), and live 429s
        learned later by the router still take effect on their own.
        """
        keys = [k for k in p.keys if k]
        # A keyless provider (OpenCode serves its free models without any
        # credential) is probed with an empty key - headers() then omits the
        # Authorization header entirely instead of sending a bogus "Bearer ".
        if not keys and getattr(p, "keyless", False):
            keys = [""]
        if not keys:
            return {"provider": p.name, "burst": 0, "limit_hits": 0,
                    "max_rpm": None, "detail": "no api key configured"}
        m = model or (next(iter(p.models.values()), p.name) if p.models else p.name)
        body = {"model": m, "messages": [{"role": "user", "content": test_prompt}],
                "max_tokens": 1}
        sem = asyncio.Semaphore(8)

        async def one(i: int):
            async with sem:
                t0 = time.time()
                try:
                    r = await self.http.post(up.chat_url(p), headers=up.headers(p, keys[0]),
                                             json=body, timeout=timeout)
                    detail = ""
                    if r.status_code == 429:
                        try:
                            detail = str((r.json().get("error") or {}).get("message")
                                         or r.text[:200])
                        except ValueError:
                            detail = r.text[:200]
                        return {"i": i, "limit": True, "ms": round((time.time() - t0) * 1000, 1),
                                "headers": r.headers, "detail": detail}
                    return {"i": i, "limit": False,
                            "ms": round((time.time() - t0) * 1000, 1)}
                except (httpx.RequestError, asyncio.TimeoutError) as e:
                    return {"i": i, "limit": False, "error": str(e)[:100]}

        t0 = time.time()
        out = await asyncio.gather(*[one(i) for i in range(burst)])
        hits = [o for o in out if o.get("limit")]
        learned: int | None = None
        detail_msg = ""
        for h in hits:
            learned = learn_rate_limit(p, h.get("headers"), h.get("detail", ""))
            detail_msg = str(h.get("detail", ""))[:200]
            if learned is not None:
                break
        return {"provider": p.name, "burst": len(out), "limit_hits": len(hits),
                "max_rpm": learned, "detail": detail_msg,
                "wall_ms": round((time.time() - t0) * 1000, 1)}

    # ---- whole fleet -------------------------------------------------------
    async def run(self, providers: list[ProviderSpec], **kw) -> list[dict]:
        out = []
        for p in providers:
            if not p.enabled:
                continue
            out.append(await self.probe_provider(p, **kw))
        return out


# ---- catalog (sqlite, same file as the usage log) -------------------------
SCHEMA = """CREATE TABLE IF NOT EXISTS models(
  provider TEXT, model TEXT, data TEXT, ts REAL,
  PRIMARY KEY(provider, model));
CREATE TABLE IF NOT EXISTS denied(
  provider TEXT, model TEXT,
  PRIMARY KEY(provider, model))"""


class Catalog:
    def __init__(self, path: str) -> None:
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.execute("PRAGMA synchronous=NORMAL")
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                self.con.execute(stmt)
        self.con.commit()

    def put(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            self.con.execute("INSERT OR REPLACE INTO models(provider,model,data,ts) VALUES(?,?,?,?)",
                             (r["provider"], r["model"], json.dumps(r, ensure_ascii=False),
                              time.time()))
            n += 1
        self.con.commit()
        return n

    def all(self, provider: str = "") -> list[dict]:
        """Cached rows, newest write time folded in as `ts`.

        The row json comes from `ProbeResult.as_dict()`, which has no timestamp
        of its own; the console needs one to decide what counts as stale, so the
        column value is merged here rather than duplicated into every writer.
        """
        q = "SELECT data, ts FROM models" + (" WHERE provider=?" if provider else "") + \
            " ORDER BY provider,model"
        cur = self.con.execute(q, (provider,) if provider else ())
        out = []
        for r in cur.fetchall():
            d = json.loads(r["data"])
            d.setdefault("ts", float(r["ts"] or 0.0))
            out.append(d)
        return out

    def last_seen(self) -> float:
        row = self.con.execute("SELECT max(ts) t FROM models").fetchone()
        return float(row["t"] or 0.0)

    def clear(self, provider: str = "") -> int:
        if provider:
            n = self.con.execute("DELETE FROM models WHERE provider=?", (provider,)).rowcount
        else:
            n = self.con.execute("DELETE FROM models").rowcount
        self.con.commit()
        return n

    def delete(self, provider: str, model: str) -> int:
        """Drop one cached probe row (provider, model) - the "未发布" delete path.

        Unlike `clear` this is surgical: it removes the single id the operator
        picked from the tree, leaving every other probed model of that provider
        (and every other provider) alone.
        """
        n = self.con.execute(
            "DELETE FROM models WHERE provider=? AND model=?", (provider, model)).rowcount
        self.con.commit()
        return n

    def deny(self, provider: str, model: str) -> int:
        """Remember that the operator rejected this model id.

        Deleting the cached probe row is not enough: the next freshness pass
        would re-list and re-probe it.  A row in `denied` makes the discoverer
        skip that (provider, model) pair for good, until the operator clears
        the denial with `undeny`.
        """
        self.con.execute(
            "INSERT OR REPLACE INTO denied(provider, model) VALUES(?, ?)",
            (provider, model))
        self.con.commit()
        return 1

    def undeny(self, provider: str, model: str) -> int:
        n = self.con.execute(
            "DELETE FROM denied WHERE provider=? AND model=?", (provider, model)).rowcount
        self.con.commit()
        return n

    def denied(self, provider: str = "") -> set:
        """{(provider, model)} the operator has rejected - read once per probe."""
        q = "SELECT provider, model FROM denied" + (" WHERE provider=?" if provider else "")
        return {(r["provider"], r["model"]) for r in self.con.execute(
            q, (provider,) if provider else ())}

    def close(self) -> None:
        self.con.close()
