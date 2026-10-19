"""OpenAI-compatible gateway built on FastAPI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import upstream as up
from . import dsm
from . import egc
from .autotune import AutoTuner
from .batch import BatchRunner, BatchTooLarge
from .billing import DEFAULT_RATES, SYMBOLS
from .clash import ClashController, EgressRegistry, NetPlane
from .net_routes import build as build_net
from .assess import Assessor
from .assess_routes import build as build_assess
from .concurrency import Gate, Meter, SMSocketBusy
from .config import Settings, envv, load_config
from .discover import Catalog
from .admin import build as build_admin
from .dashboard import PAGE
from .extensions import build as build_extensions
from .dsm_routes import build as build_dsm
from .providers import Pool, is_auto_alias, resolve_auto_alias
from .stacksched import StackScheduler
from .router import NoUpstream, Router, UpstreamError
from .usage import Usage

log = logging.getLogger("smssocket")

CONSOLE_COOKIE = "sms_console"
CONSOLE_TTL = 12 * 3600          # a console session is remembered for half a day
RESPONSE_STORE_MAX = 200         # GET /v1/responses/{id} can read back this many
RESPONSE_STORE_MAX_BYTES = 20 * 1024 * 1024   # ~20 MB 总闸：单条超限的整条丢弃
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testserver", "testclient"}


def _loopback(request) -> bool:
    """True for a browser on this machine (and TestClient in the tests)."""
    host = (request.client.host if getattr(request, "client", None) else "") or ""
    return host in LOOPBACK or host.startswith("127.")


def _upstream_error(e: UpstreamError) -> dict:
    """Error body for UpstreamError, including the optional machine code
    (e.g. "resend" when the request was sent but never received a response)."""
    err = {"message": str(e.detail), "type": "upstream_error"}
    if e.code:
        err["code"] = e.code
    return err


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = Pool(settings.providers)
        self.usage = Usage(settings.db_path)
        self.http: httpx.AsyncClient | None = None
        self.router: Router | None = None
        self.meter = Meter()
        self.batch = BatchRunner()
        self.catalog = Catalog(settings.db_path)
        self.progress: dict = {}
        self.gate = Gate(settings.max_concurrency,
                                 settings.queue_wait, settings.per_provider_concurrency,
                                 self.meter)
        # clash net plane - all None while settings.clash.enabled is false
        self.clash: ClashController | None = None
        self.egress: EgressRegistry | None = None
        self.net: NetPlane | None = None
        # model/net assessment - None while `assess.enabled` is false
        self.assessor: Assessor | None = None
        # background model-probe loop (v0.5b): keeps the catalog fresh on its own
        self.probe_task = None
        # stack scheduler - None while `stack.enabled` is false (plain 429s).
        # The `gate` setter above already built and attached it; a bare
        # annotation keeps the type visible without clobbering that attachment.
        self.stack: StackScheduler | None
        # autotune - None while `tune.enabled` is false
        self.tuner: AutoTuner | None = None
        # console sessions minted for a browser on this machine (token -> expiry)
        self.console: dict[str, float] = {}
        # GET /v1/responses/{id} reads back from here (bounded FIFO + byte cap)
        self.responses_store: dict[str, dict] = {}
        self._responses_store_bytes = 0

    def build_stack(self) -> None:
        """(Re)create the parking lot and hand it to the gate.

        The gate owns the admission decision, so the stack is attached to it
        rather than consulted per request; with the block disabled the gate
        keeps `stack = None` and behaves exactly like pre-v0.4b.

        Runs on every gate swap (lifespan, /admin/reload, admin.apply_state):
        the previous drain task is cancelled and its parked callers released,
        otherwise each reload would leak one background task and leave callers
        waiting on a scheduler that nothing wakes any more.
        """
        old = getattr(self, "stack", None)
        self.stack = None
        if old is not None:
            for entry in list(old.stack.items):
                old.stack.expire(entry)      # parked callers get their 429 now
            if old._task is not None:
                old._task.cancel()           # no public sync stop on the scheduler
        cfg = self.settings.stack
        if not cfg.enabled:
            self.gate.attach_stack(None)
            return
        self.stack = StackScheduler(cfg, self.gate, self.pool)
        self.gate.attach_stack(self.stack)
        try:
            asyncio.get_running_loop()       # start only where a loop is live
        except RuntimeError:                 # create_app outside a loop: lifespan
            pass
        else:
            self.stack.start()

    @property
    def gate(self) -> Gate:
        return self._gate

    @gate.setter
    def gate(self, value: Gate) -> None:
        # a gate replacement must re-attach the stack: a fresh Gate holding the
        # old scheduler (or none) silently loses the limiter on hot reload
        self._gate = value
        if getattr(self, "settings", None) is not None:
            self.build_stack()

    def build_net_plane(self) -> None:
        """(Re)create controller / egress registry / scoring plane from config.

        Disabled config leaves `st.http` as the only client, so the gateway
        behaves exactly like it did before v0.4a.
        """
        cfg = self.settings.clash
        self.clash = self.egress = self.net = None
        if not (cfg.enabled and self.http is not None):
            return
        self.clash = ClashController(cfg)
        self.egress = EgressRegistry(self.http, cfg)
        self.net = NetPlane(cfg, self.egress, self.clash)
        self.net.set_targets(self.settings.providers)

    def attach_router(self) -> Router:
        self.router = Router(self.settings, self.pool, self.usage, self.http,
                             self.gate, net=self.net, egress=self.egress)
        self.router.assessor = self.assessor      # mirrors live calls if enabled
        return self.router

    def build_assessor(self) -> None:
        """(Re)create the assessor from config. Disabled = no object at all,
        so live traffic is never mirrored into a table nobody asked for."""
        cfg = self.settings.assess
        self.assessor = Assessor(self.settings, self.router, self.net,
                                 self.usage) if (cfg.enabled and self.router) else None
        if self.router is not None:
            self.router.assessor = self.assessor

    def build_tuner(self) -> None:
        """(Re)create the weight/priority tuner from the measured window.

        Cancel-first: apply_state() runs inside a live loop, so a naive start
        would leave the previous scheduler task running and two tuners would
        fight over the same pool.
        """
        old = getattr(self, "tuner", None)
        if old is not None and old._task is not None:
            old._task.cancel()
        cfg = self.settings.tune
        self.tuner = AutoTuner(self.settings, self.usage, self.pool) \
            if (cfg.enabled and self.router is not None) else None
        if self.router is not None:
            self.router.tuner = self.tuner
        if self.assessor is not None:
            self.assessor.tuner = self.tuner
        if self.tuner is not None:
            self.tuner.start()          # no-op when no loop is running

    def rewire(self) -> None:
        """Re-point every dependent object at the current settings + pool.

        A settings swap that only rebuilds the Router silently loses the net
        plane (v0.3 regression) and leaves the assessor/tuner holding the old
        Settings object, so they keep measuring against stale configuration.
        """
        for obj in (self.assessor, getattr(self, "tuner", None)):
            if obj is not None and getattr(obj, "_task", None) is not None:
                obj._task.cancel()
        self.attach_router()
        self.build_assessor()
        if self.assessor is not None:
            self.assessor.start()
        self.build_tuner()


async def rebuild_net_plane(st: State) -> None:
    """Stop the old plane (task + proxy clients), build a fresh one, restart."""
    if st.net is not None:
        await st.net.stop()
    st.build_net_plane()
    if st.net is not None:
        st.net.start()
        log.info("net plane up: controller=%s paths=%s", st.settings.clash.controller,
                 len(st.net.registry.paths()))


def bind_dsm(st: "State") -> dict:
    """Point the DSM stores at this settings' paths/tiers (create_app + every reload).

    The stores are module-level singletons so a hot reload swaps file targets without
    orphaning in-flight requests; state() then answers "is DSM on?" from /healthz.
    """
    d = st.settings.dsm
    base = Path(st.settings.db_path).parent
    dsm.bind_settings(d)
    return dsm.configure(schema_path=d.abs_path(base),
                        session_path=d.abs_path(base, "dsm_sessions.json"),
                        budget_map=d.budget_map)


def bind_egc(st: "State") -> dict:
    """Point the EGC outbound-standard layer at this settings' profile.

    Same hot-reload discipline as bind_dsm: the profile is module-level and is
    swapped in place, so an in-flight request keeps the profile it was built
    with. `egc.enabled: false` returns egress to byte-for-byte legacy shape.
    """
    egc.bind_settings(st.settings.egc)
    return egc.state()


def create_app(settings: Settings | None = None) -> FastAPI:
    st = State(settings or load_config(envv("SMSSOCKET_CONFIG", "config.yaml")))
    bind_dsm(st)
    bind_egc(st)
    logging.basicConfig(level=getattr(logging, st.settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.http = httpx.AsyncClient(follow_redirects=True,
                                    limits=httpx.Limits(max_connections=100))
        st.build_net_plane()
        st.attach_router()
        st.build_assessor()
        if st.net is not None:
            st.net.start()
        if st.assessor is not None:
            st.assessor.start()
        st.build_stack()
        if st.stack is not None:
            st.stack.start()
        st.build_tuner()                 # measured weight/priority, if enabled
        from .discover_routes import _probe_loop
        if st.probe_task is None or st.probe_task.done():
            st.probe_task = _probe_loop(st)
        log.info("SMSocket ready: %d providers, %d keys, %d models",
                 len(st.settings.providers), len(st.pool.slots), len(st.settings.model_index()))
        yield
        if st.net is not None:
            await st.net.stop()
        if st.stack is not None:
            await st.stack.stop()
        if st.assessor is not None:
            await st.assessor.stop()
        if st.tuner is not None:
            await st.tuner.stop()
        if st.probe_task is not None:
            st.probe_task.cancel()
            st.probe_task = None
        if st.usage is not None:
            st.usage.flush()
        # DSM 会话态去抖收口：脏水位落盘（热重载/停机两条路）
        try:
            if dsm._sessions is not None:
                dsm._sessions.close()
            if dsm._schemas is not None:
                dsm._schemas.flush()
        except Exception:
            pass
        await st.http.aclose()
        st.usage.close()
        st.catalog.close()

    app = FastAPI(title="SMSocket", version="0.4.0", lifespan=lifespan)
    app.state.llm = st

    @app.exception_handler(SMSocketBusy)
    async def _saturated(request: Request, exc: SMSocketBusy):
        """Gate is full and the caller gave up waiting -> 429 + Retry-After."""
        return JSONResponse({"error": {"message": str(exc), "type": "rate_limited",
                                       "code": "gateway_saturated"}},
                            status_code=429,
                            headers={"retry-after": str(exc.retry_after)})

    def authorize(request: Request) -> None:
        keys = st.settings.master_keys
        if not keys:
            return
        auth = request.headers.get("authorization", "").strip()
        token = ""
        m = re.match(r"(?i)^bearer\s+(.+)$", auth)
        if m:
            token = m.group(1).strip()
        token = token or request.headers.get("x-api-key", "").strip() \
            or request.query_params.get("api_key", "")
        if token in keys:
            return
        # a browser on this machine gets a cookie when it opens the console,
        # so the operator never retypes the key; remote callers still need it
        now = time.time()
        for tok in [k for k, v in st.console.items() if v < now]:
            st.console.pop(tok, None)
        if request.cookies.get(CONSOLE_COOKIE, "") in st.console:
            return
        if token not in keys:
            raise HTTPException(401, detail={"error": {"message": "invalid api key",
                                                       "type": "authentication_error"}})

    dep = [Depends(authorize)]

    def egress_headers(egress: str) -> dict:
        """x-socket-egress only while the net plane is live (no new headers
        for a gateway that has clash disabled)."""
        if getattr(st, "net", None) is None:
            return {}
        return {"x-socket-egress": egress or "direct"}


    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "providers": len(st.settings.providers),
                "keys": len(st.pool.slots), "models": len(st.settings.model_index()),
                "active": st.meter.active, "currency": st.settings.billing.currency,
                "dsm": dsm.state(), "egc": egc.state()}

    # ---- parallel batch + async jobs --------------------------------------
    @app.post("/v1/batch", dependencies=dep)
    async def batch(request: Request, async_: int = Query(0, alias="async"),
                    wait: int = 1):
        """Send many chat payloads at once.

        {"requests": [{model, messages, ...}, ...], "concurrency": 8,
         "fail_fast": false, "async": true}
        Sync (default) returns every result; async returns a job id to poll.
        """
        body = await request.json()
        items = (body or {}).get("requests") or (body or {}).get("items") or []
        if not isinstance(items, list) or not items:
            raise HTTPException(400, detail={"error": {
                "message": "requests must be a non-empty list",
                "type": "invalid_request_error"}})
        known = st.settings.model_index()
        bad = [i for i, it in enumerate(items)
               if not isinstance(it, dict) or not str(it.get("model") or "")]
        if bad:
            raise HTTPException(400, detail={"error": {
                "message": f"item {bad[0]} needs a model", "type": "invalid_request_error"}})
        unknown = sorted({str(it.get("model")) for it in items
                          if isinstance(it, dict) and str(it.get("model") or "") not in known
                          and not is_auto_alias(str(it.get("model") or ""))})
        if unknown:
            raise HTTPException(404, detail={"error": {
                "message": "unknown model(s): " + ", ".join(unknown),
                "type": "invalid_request_error"}})
        # Resolve `auto` items the same way the single-call routes do, so a fan
        # out of N auto lanes spreads across providers instead of 404'ing.
        for it in items:
            a = str(it.get("model") or "")
            if is_auto_alias(a):
                picked = resolve_auto_alias(st.pool, st.settings.strategy, a)
                if not picked:
                    raise HTTPException(503, detail={"error": {
                        "message": "no models available", "type": "server_error"}})
                it["model"] = picked
        conc = int(body.get("concurrency", 8) or 8)
        if not 1 <= conc <= 256:
            raise HTTPException(400, detail={"error": {
                "message": "concurrency must be 1..256", "type": "invalid_request_error"}})
        fail_fast = bool(body.get("fail_fast", False))
        as_job = bool(async_ or body.get("async") or not wait)
        try:
            if as_job:
                job = st.batch.start(st.router, items, conc, fail_fast)
                return JSONResponse({"accepted": True, "job": job.id,
                                     "total": job.total, "concurrency": conc,
                                     "poll": f"/v1/batches/{job.id}"}, status_code=202)
            out = await st.batch.run(st.router, items, conc, fail_fast)
            out["concurrency_live"] = st.meter.snapshot(st.gate.limits())
            return JSONResponse(out)
        except BatchTooLarge as e:
            raise HTTPException(413, detail={"error": {"message": str(e),
                                                       "type": "invalid_request_error"}})

    @app.get("/v1/batches", dependencies=dep)
    async def batches():
        return {"object": "list", "running": st.batch.running(),
                "jobs": st.batch.list()}

    @app.get("/v1/batches/{job_id}", dependencies=dep)
    async def batch_job(job_id: str, results: int = 0, wait: int = 0):
        """Poll a job. wait=1 blocks until it finishes (bounded by job size)."""
        job = st.batch.get(job_id)
        if job is None:
            raise HTTPException(404, detail={"error": {
                "message": f"unknown job: {job_id}", "type": "invalid_request_error"}})
        if wait and job.task and job.status == "running":
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=float(wait))
            except asyncio.TimeoutError:
                pass
        view = job.view(with_results=True, limit=results or 0)
        view["summary"] = st.batch.summary(job.results, job.concurrency,
                                           round(view["elapsed"] * 1000, 1),
                                           st.settings.billing)["summary"]
        return view

    @app.delete("/v1/batches/{job_id}", dependencies=dep)
    async def batch_cancel(job_id: str):
        job = st.batch.get(job_id)
        if job is None:
            raise HTTPException(404, detail={"error": {
                "message": f"unknown job: {job_id}", "type": "invalid_request_error"}})
        return {"job": job_id, "cancelled": st.batch.cancel(job_id),
                "status": job.status}

    @app.get("/concurrency")
    async def concurrency():
        """Live in-flight count, peak, queue depth, per-provider breakdown."""
        snap = st.meter.snapshot(st.gate.limits())
        # one poll must answer "was that 429 refused outright or is it parked?",
        # so the stack view rides along even while the scheduler is disabled
        snap["stack"] = (st.stack.snapshot(entries=0) if st.stack is not None
                         else {"enabled": False, "depth": 0, "peak": 0,
                               "pushed": 0, "popped": 0, "expired": 0,
                               "dropped": 0, "policy": st.settings.stack.policy,
                               "wait": st.settings.stack.wait, "draining": False})
        return snap

    def busy(e: SMSocketBusy):
        return JSONResponse(
            {"error": {"message": str(e), "type": "rate_limited",
                      "code": "gateway_saturated"}},
            status_code=429, headers={"retry-after": str(e.retry_after)})

    @app.get("/v1/models", dependencies=dep)
    async def models():
        idx, eidx = st.settings.model_index(), st.settings.embed_index()
        data = [{"id": a, "object": "model", "created": 0,
                 "owned_by": ",".join(sorted({p.name for p in idx[a]}))} for a in sorted(idx)]
        data += [{"id": a, "object": "model", "created": 0,
                  "owned_by": ",".join(sorted({p.name for p in eidx[a]}))} for a in sorted(eidx)]
        return {"object": "list", "data": data}

    def legacy_closed() -> None:
        """dsm.openai_compat=false -> legacy endpoints answer 410 with the fix hint.

        A loud refusal, never a silent downgrade: FF is already refused at config
        load, so reaching here means the caller turned compat off on purpose.
        """
        if st.settings.dsm.openai_compat:
            return
        raise HTTPException(410, detail={"error": {
            "message": "legacy OpenAI endpoints are closed (dsm.openai_compat=false); "
                       "post a DSM envelope to /v1/dsm/chat or set dsm.openai_compat: true",
            "type": "legacy_disabled", "code": "dsm_openai_compat_false"}})

    def _resolve_auto_alias(alias: str, idx: dict) -> str:
        """Resolve 'smsocket'/'auto'/'smsocket/auto' by provider ranking.

        The pick goes through Pool.auto_alias so it respects priority, weight,
        autotune overrides and cooldown state. Returning the first alias of the
        first provider in config.yaml (the old behaviour) pinned every `auto`
        request onto one provider forever, and autotune then kept reinforcing
        that provider because it alone saw any traffic.
        """
        if not is_auto_alias(alias):
            return alias
        if not idx:
            raise HTTPException(503, detail={"error": {"message": "no models available",
                                                        "type": "server_error"}})
        picked = resolve_auto_alias(st.pool, st.settings.strategy, alias)
        if not picked:
            raise HTTPException(503, detail={"error": {"message": "no models available",
                                                        "type": "server_error"}})
        return picked

    @app.post("/v1/chat/completions", dependencies=dep)
    async def chat(request: Request):
        legacy_closed()
        payload = await request.json()
        alias = str(payload.get("model") or "")
        if not alias:
            raise HTTPException(400, detail={"error": {"message": "model is required",
                                                        "type": "invalid_request_error"}})
        idx = st.settings.model_index()
        if alias not in idx and not is_auto_alias(alias):
            raise HTTPException(404, detail={"error": {"message": f"unknown model '{alias}'",
                                                        "type": "invalid_request_error"}})
        # Resolve auto-aliases before routing
        resolved_alias = _resolve_auto_alias(alias, idx)
        if resolved_alias != alias:
            payload["model"] = resolved_alias
        out: dict = {}
        try:
            if payload.get("stream"):
                slot, alias, t0, resp = await st.router.open_stream(payload)
                hdr = {"x-socket-upstream": slot.provider.name,
                       "x-router-upstream": slot.provider.name,
                       **egress_headers(getattr(resp, "_sms_egress", ""))}
                return StreamingResponse(st.router.wrap_stream(slot, alias, t0, resp),
                                         media_type="text/event-stream", headers=hdr)
            return JSONResponse(await st.router.complete(payload, egress_out=out),
                                headers=egress_headers(out.get("egress", "")))
        except NoUpstream as e:
            raise HTTPException(503, detail={"error": {"message": str(e), "type": "server_error"}})
        except UpstreamError as e:
            raise HTTPException(e.status, detail={"error": _upstream_error(e)})

    # ---- Anthropic Messages API (the third surface) ------------------------
    # /v1/messages is a different shape again, not an alias: `system` + `messages`
    # with block content + `max_tokens` in, a content block list + `stop_reason`
    # out, and a message_start/content_block_delta/message_stop SSE stream. The
    # gateway serves it and translates against whatever the upstream speaks, so
    # Anthropic-style clients (opencode / kilocode) can point at it as their
    # anthropic baseURL and talk to any configured provider.
    @app.post("/v1/messages", dependencies=dep)
    async def messages(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, detail={"error": {
                "message": "body must be an object", "type": "invalid_request_error"}})
        payload = up.anthropic_request_to_chat(body)
        alias = str(payload.get("model") or "")
        if not alias:
            raise HTTPException(400, detail={"error": {
                "message": "model is required", "type": "invalid_request_error"}})
        idx = st.settings.model_index()
        if alias not in idx and not is_auto_alias(alias):
            raise HTTPException(404, detail={"error": {
                "message": f"unknown model '{alias}'",
                "type": "invalid_request_error"}})
        resolved_alias = _resolve_auto_alias(alias, idx)
        if resolved_alias != alias:
            payload["model"] = resolved_alias
        out: dict = {}
        try:
            if payload.get("stream"):
                slot, alias, t0, resp = await st.router.open_stream(payload)
                hdr = {"x-socket-upstream": slot.provider.name,
                       "x-router-upstream": slot.provider.name,
                       **egress_headers(getattr(resp, "_sms_egress", ""))}
                chat_sse = st.router.wrap_stream(slot, alias, t0, resp)
                return StreamingResponse(
                    up.chat_stream_to_anthropic_events(chat_sse, alias),
                    media_type="text/event-stream", headers=hdr)
            chat = await st.router.complete(payload, egress_out=out)
            obj = up.chat_to_anthropic(chat, alias)
            return JSONResponse(obj, headers=egress_headers(out.get("egress", "")))
        except NoUpstream as e:
            raise HTTPException(503, detail={"error": {"message": str(e),
                                                       "type": "server_error"}})
        except UpstreamError as e:
            raise HTTPException(e.status, detail={"error": _upstream_error(e)})

    # ---- OpenAI Responses API (the second OpenAI surface) -----------------
    # /v1/chat/completions and /v1/responses are different shapes, not aliases:
    # input+instructions+max_output_tokens in, an output item list + status out,
    # and a named-event SSE stream instead of chat.completion.chunk. The gateway
    # serves both and translates against whatever the upstream actually speaks.
    @app.post("/v1/responses", dependencies=dep)
    async def responses(request: Request):
        legacy_closed()
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, detail={"error": {
                "message": "body must be an object", "type": "invalid_request_error"}})
        payload = up.responses_to_chat_request(body)
        alias = str(payload.get("model") or "")
        if not alias:
            raise HTTPException(400, detail={"error": {
                "message": "model is required", "type": "invalid_request_error"}})
        idx = st.settings.model_index()
        if alias not in idx and not is_auto_alias(alias):
            raise HTTPException(404, detail={"error": {"message": f"unknown model '{alias}'",
                                                        "type": "invalid_request_error"}})
        # Resolve auto-aliases before routing
        resolved_alias = _resolve_auto_alias(alias, idx)
        if resolved_alias != alias:
            payload["model"] = resolved_alias
        out: dict = {}
        try:
            if payload.get("stream"):
                slot, alias, t0, resp = await st.router.open_stream(payload)
                hdr = {"x-socket-upstream": slot.provider.name,
                       "x-router-upstream": slot.provider.name,
                       **egress_headers(getattr(resp, "_sms_egress", ""))}
                # wrap_stream normalises *any* upstream style into chat chunks,
                # so one translation covers every provider style
                chat_sse = st.router.wrap_stream(slot, alias, t0, resp)
                return StreamingResponse(up.chat_stream_to_response_events(chat_sse, alias),
                                         media_type="text/event-stream", headers=hdr)
            chat = await st.router.complete(payload, egress_out=out)
            obj = up.chat_to_responses_obj(chat, alias)
            store = st.responses_store
            store_bytes = st._responses_store_bytes
            obj_size = len(json.dumps(obj, default=str).encode("utf-8"))
            if obj_size <= RESPONSE_STORE_MAX_BYTES and store_bytes + obj_size <= RESPONSE_STORE_MAX_BYTES:
                while len(store) >= RESPONSE_STORE_MAX:
                    old = store.pop(next(iter(store)))
                    store_bytes -= len(json.dumps(old, default=str).encode("utf-8"))
                store[obj["id"]] = obj
                store_bytes += obj_size
                st._responses_store_bytes = store_bytes
            return JSONResponse(obj, headers=egress_headers(out.get("egress", "")))
        except NoUpstream as e:
            raise HTTPException(503, detail={"error": {"message": str(e),
                                                       "type": "server_error"}})
        except UpstreamError as e:
            raise HTTPException(e.status, detail={"error": _upstream_error(e)})

    @app.get("/v1/responses/{response_id}", dependencies=dep)
    async def responses_get(response_id: str):
        obj = st.responses_store.get(response_id)
        if obj is None:
            raise HTTPException(404, detail={"error": {
                "message": f"unknown response '{response_id}'",
                "type": "invalid_request_error"}})
        return JSONResponse(obj)

    @app.delete("/v1/responses/{response_id}", dependencies=dep)
    async def responses_delete(response_id: str):
        gone = st.responses_store.pop(response_id, None)
        if gone is None:
            raise HTTPException(404, detail={"error": {
                "message": f"unknown response '{response_id}'",
                "type": "invalid_request_error"}})
        return {"id": response_id, "object": "response", "deleted": True}

    @app.get("/stack")
    async def stack_state(entries: int = 20):
        """Live parking lot: depth, peak, counters, parked callers."""
        if st.stack is None:
            return {"enabled": False, "policy": st.settings.stack.policy,
                    "wait": st.settings.stack.wait, "depth": 0, "peak": 0,
                    "pushed": 0, "popped": 0, "expired": 0, "dropped": 0,
                    "parked_ms_peak": 0.0, "avg_parked_ms": 0.0,
                    "by_provider": {}, "entries": [], "draining": False}
        return st.stack.snapshot(entries=max(0, min(entries, 200)))

    def need_stack():
        if st.stack is None:
            raise HTTPException(409, detail={"error": {
                "message": "stack scheduler is disabled (set stack.enabled: true)",
                "type": "stack_disabled"}})
        return st.stack

    @app.post("/stack/drain", dependencies=dep)
    async def stack_drain(request: Request, n: int = Query(0)):
        """Wake up to n parked callers (ops/tests): {"n": 5} or ?n=5."""
        stack = need_stack()
        try:
            body = await request.json()
        except Exception:                                   # noqa: BLE001
            body = {}
        # the query string wins so a shell one-liner needs no body at all
        want = n if n else (int((body or {}).get("n", 1) or 1)
                            if isinstance(body, dict) else 1)
        return await stack.drain_now(max(0, want))

    @app.delete("/stack/{index}", dependencies=dep)
    async def stack_drop(index: int):
        """Release one parked caller without a grant (it gets its 429)."""
        out = need_stack().drop_at(index)
        if not out.get("dropped"):
            raise HTTPException(404, detail={"error": {
                "message": out.get("reason", "no such entry"),
                "type": "invalid_request_error"}})
        return out

    def money(currency: str | None = None) -> dict:
        """Spend totals: per pricing currency, converted into the display one."""
        b = st.settings.billing
        disp = (currency or b.currency).strip().upper() or b.currency
        rows = []
        for row in st.usage.by_currency():
            cur = (row["currency"] or b.base).upper()
            rows.append({"currency": cur, "symbol": b.symbol(cur),
                         "calls": row["calls"] or 0, "tokens": row["tokens"] or 0,
                         "amount": round(row["cost"] or 0.0, 8),
                         "rate_to_display": round(b.rate(cur, disp), 8),
                         "in_display": b.convert(row["cost"] or 0.0, cur, disp)})
        total = round(sum(r["in_display"] for r in rows), b.precision)
        return {"display_currency": disp, "symbol": b.symbol(disp), "total": total,
                "base": b.base, "rates": b.rates, "by_currency": rows}

    @app.get("/stats")
    async def stats(currency: str = None):
        s = st.usage.summary()
        m = money(currency)
        return {"by_provider": s["providers"], "by_model": s["models"],
                "calls": s["total"]["c"], "tokens": s["total"]["t"] or 0,
                "cost": m["total"], "currency": m["display_currency"],
                "symbol": m["symbol"], "money": m,
                "billing": st.settings.billing.table(),
                "recent": st.usage.recent(30)}
    @app.get("/v1/usage", dependencies=dep)
    async def usage(days: int = 14, currency: str = None):
        s = st.usage.summary()
        b = st.settings.billing
        m = money(currency)
        daily = st.usage.daily(max(1, min(days, 365)))
        for row in daily:
            row["cost"] = b.convert(row.get("display") or 0.0,
                                    row.get("currency") or b.currency,
                                    m["display_currency"])
            row["currency"] = m["display_currency"]
        return {"currency": m["display_currency"], "symbol": m["symbol"],
                "totals": {"calls": s["total"]["c"], "tokens": s["total"]["t"] or 0,
                           "cost": m["total"]},
                "money": m, "billing": b.table(),
                "by_provider": s["providers"], "by_model": s["models"],
                "daily": daily}
    @app.post("/v1/embeddings", dependencies=dep)
    async def embeddings(request: Request):
        payload = await request.json()
        alias = str(payload.get("model") or "")
        if not alias:
            raise HTTPException(400, detail={"error": {"message": "model is required"}})
        out: dict = {}
        try:
            return JSONResponse(await st.router.embeddings(payload, egress_out=out),
                                headers=egress_headers(out.get("egress", "")))
        except NoUpstream as e:
            raise HTTPException(404, detail={"error": {"message": str(e), "type": "invalid_request_error"}})
        except SMSocketBusy as e:
            return busy(e)
        except UpstreamError as e:
            raise HTTPException(e.status, detail={"error": _upstream_error(e)})

    @app.get("/pool")
    async def pool():
        return {"strategy": st.settings.strategy, "cooldown": st.settings.cooldown,
                "slots": st.pool.stats()}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # __NET_ON__ drives the discreet footer glyph: 0 -> the panel is absent
        # from the DOM entirely, so a disabled net plane leaves no trace.
        # __ASSESS_ON__ does the same for the reachability block: a gateway that
        # measures nothing must not advertise a measuring tool.
        # __SKIN__ is the *server-side* theme (config `ui.skin`): a theme chosen
        # in one browser survives a new profile, a private window or a reinstall.
        # __CUR__/__SYM__ render the currency list into the page so the billing
        # selects are never empty, even before the first authenticated call.
        b = st.settings.billing
        currencies = sorted(set(b.rates) | set(DEFAULT_RATES))
        html = (PAGE.replace("__NET_ON__", "1" if st.settings.clash.enabled else "0")
                .replace("__ASSESS_ON__", "1" if st.settings.assess.enabled else "0")
                .replace("__SKIN__", st.settings.ui.skin)
                .replace("__CUR__", json.dumps(currencies))
                .replace("__SYM__", json.dumps(SYMBOLS, ensure_ascii=False)))
        resp = HTMLResponse(html)
        tok = request.cookies.get(CONSOLE_COOKIE, "")
        if tok in st.console and st.console[tok] > time.time():
            return resp
        if _loopback(request):
            tok = secrets.token_urlsafe(32)
            st.console[tok] = time.time() + CONSOLE_TTL
            resp.set_cookie(CONSOLE_COOKIE, tok, max_age=CONSOLE_TTL,
                            httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/admin/reload", dependencies=dep)
    async def reload_config():
        # Never swap in an empty/broken config: a bad reload must leave live state intact.
        path = envv("SMSSOCKET_CONFIG", "config.yaml")
        if not Path(path).exists():
            raise HTTPException(409, detail={"error": {
                "message": f"config not found: {path}", "type": "invalid_request_error"}})
        try:
            new = load_config(path)
        except Exception as exc:
            raise HTTPException(400, detail={"error": {
                "message": f"config rejected: {exc}", "type": "invalid_request_error"}})
        if not new.providers:
            raise HTTPException(409, detail={"error": {
                "message": "config has no providers, refusing to reload",
                "type": "invalid_request_error"}})
        st.settings = new
        st.pool = st.pool.rebase(new.providers) if st.pool else Pool(new.providers)
        st.gate = Gate(new.max_concurrency, new.queue_wait,
                       new.per_provider_concurrency, st.meter)
        if st.assessor is not None:
            await st.assessor.stop()          # old router is about to be replaced
        parked = 0 if st.stack is None else len(st.stack.stack)
        st.build_stack()                       # fresh gate -> fresh attachment
        if st.stack is not None:
            st.stack.start()
        await rebuild_net_plane(st)
        st.attach_router()
        st.build_assessor()                    # after the router exists again
        if st.assessor is not None:
            st.assessor.start()
        st.build_tuner()                       # cancel-first, then start
        # DSM 去抖收口：热重载前把旧 store 的脏水位/schema 落盘
        try:
            if dsm._sessions is not None:
                dsm._sessions.close()
            if dsm._schemas is not None:
                dsm._schemas.flush()
        except Exception:
            pass
        bind_dsm(st)                           # dsm: block may have moved paths/tiers
        bind_egc(st)                           # egc: profile / lane whitelist may have moved
        log.info("reloaded: %d providers, %d keys", len(new.providers), len(st.pool.slots))
        return {"ok": True, "providers": len(new.providers), "keys": len(st.pool.slots),
                "net_plane": bool(st.net is not None), "dsm": dsm.state(),
                "egc": egc.state(),
                "stack": bool(st.stack is not None), "parked_dropped": parked,
                "assess": bool(st.assessor is not None)}

    # ---- measured weight / priority --------------------------------------
    @app.get("/tune", dependencies=dep)
    async def tune_state():
        """What the gateway currently believes about each provider, and why."""
        t = getattr(st, "tuner", None)
        if t is None:
            return {"enabled": False, "scores": [], "in_force": {},
                    "config": st.settings.tune.as_config(),
                    "note": "tuning is disabled (set tune.enabled: true)"}
        return t.status()

    @app.post("/tune/apply", dependencies=dep)
    async def tune_apply():
        """Re-rank right now from the measured window (no timer wait)."""
        t = getattr(st, "tuner", None)
        if t is None:
            raise HTTPException(409, detail={"error": {
                "message": "tuning is disabled (set tune.enabled: true)",
                "type": "tuning_disabled"}})
        return t.apply()

    app.include_router(build_admin(st, dep))   # includes /admin/billing*
    app.include_router(build_extensions(st, dep))   # 扩展程序：导入 · 识别码验证 · 自带面板
    # Discreet operator surface: not in /openapi.json, not in /docs, not in the nav.
    app.include_router(build_net(st, dep), prefix="/internal/net",
                       include_in_schema=False)
    app.include_router(build_assess(st, dep))
    # DSM envelope endpoints: always mounted, gated per-request by dsm.enabled
    # (404 when off = the exact signal the client uses to downgrade, contract §1)
    app.include_router(build_dsm(st, dep, egress_headers))
    return app
