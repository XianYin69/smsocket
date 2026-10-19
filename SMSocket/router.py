"""Routing engine: candidate plan, upstream call, failover, usage accounting."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

from . import egc
from . import upstream as up
from .upstream import RESPONSES_STYLE
from .clash import DIRECT
from .config import Settings
from .concurrency import Gate, SMSocketBusy
from .providers import KeySlot, Pool, learn_rate_limit
from .usage import Usage

log = logging.getLogger("smssocket")
RETRY_STATUS = {401, 403, 408, 429, 500, 502, 503, 504, 529}

# The router never gives up on a request: it polls the candidate pool round
# after round until one upstream answers. Each round tries every live candidate
# exactly once (requested alias first, then cross-model fallbacks). A transport
# timeout cools the failed alias only and the router moves to the next candidate
# immediately - no waiting, no backoff pause between rounds. When the whole
# round fails the router waits only for the nearest cooldown to expire (never a
# fixed pace) and starts the next round. There is no convergence ceiling and no
# `resend`/exhaustion error: a request only returns when some upstream succeeds,
# or the caller tears the connection down.


def _is_model_not_exist(detail: str) -> bool:
    """True when an upstream answered 422 because it does not know this model.

    That is a *routing* error, not a key failure: the key itself is healthy,
    so it must NOT be cooled and the caller must NOT be parked on it.  A
    provider whose catalog is stale/probe-failed can still answer 422 for an
    alias it was never supposed to serve; cooling its key for 60s would then
    block every model the provider *does* carry (e.g. SCNet-Max).
    """
    if not detail:
        return False
    d = str(detail).lower()
    return ("model not exist" in d or "model_not_exist" in d
            or "does not exist" in d or "unknown model" in d
            or "no such model" in d or "model_not_found" in d)


class NoUpstream(Exception):
    """Nothing available for this model right now."""


class UpstreamError(Exception):
    def __init__(self, status: int, detail, slot: KeySlot | None = None,
                 upstream_status: int = 0, code: str = ""):
        super().__init__(str(detail))
        # `status` is what the client sees (a failover is reported as 502);
        # `upstream_status` is what the provider actually answered, which is
        # the difference between "blocked here" and "having a bad day".
        # `code` is an optional machine-readable hint for the caller: when
        # every candidate failed to answer at all (sent but never received a
        # response) it is "resend", i.e. the caller should re-issue the request.
        self.status, self.detail, self.slot = status, detail, slot
        self.upstream_status = int(upstream_status or 0)
        self.code = code


class Router:
    """Turns one request into one successful upstream call.

    `net` (clash.NetPlane) and `egress` (clash.EgressRegistry) are optional:
    without them every call leaves through the shared client exactly as before.
    With them, each attempt asks the plane which path is currently best and
    sends through that path's client, recording the choice in the usage row and
    in the `x-socket-egress` response header.
    """

    def __init__(self, settings: Settings, pool: Pool, usage: Usage,
                 client: httpx.AsyncClient, gate: Gate | None = None,
                 net=None, egress=None):
        self.s, self.pool, self.usage, self.http = settings, pool, usage, client
        self.gate = gate or Gate()
        self.net = net
        self.egress = egress if egress is not None else getattr(net, "registry", None)
        self.assessor = None        # assess.Assessor, set by State.attach_router
        self.tuner = None           # autotune.AutoTuner, set by State.build_tuner
        # Sticky slot removed: each round randomizes its start instead.

    def _serving_aliases(self, alias: str) -> list[str]:
        """All OTHER aliases that currently have at least one available key slot.

        These are the cross-model failover pool: when the requested model is
        rate-limited everywhere (or simply down), the gateway walks every other
        available model instead of handing a 429 back to the client.
        """
        now = time.time()
        aliases: set[str] = set()
        for slot in self.pool.slots:
            if not slot.provider.enabled:
                continue
            # A keyless provider (OpenCode free models) has no credential: its
            # single slot carries an empty key and still counts as available.
            if slot.key or getattr(slot.provider, "keyless", False):
                for a in slot.provider.aliases():
                    if a != alias and slot.available(now, a):
                        aliases.add(a)
        return sorted(aliases)

    def _round_candidates(self, alias: str) -> list[tuple[KeySlot, str]]:
        """Candidate plan for one request: ``[(slot, send_alias), ...]``.

        Primary entries serve the requested ``alias`` and send it verbatim.
        Fallback entries serve a *different* available alias and send THAT one
        upstream - this is cross-model failover: when the requested model is
        exhausted the gateway jumps to the next available model, then the next,
        each tried exactly once. A slot that carries several aliases is tried
        once under the requested alias (its primary entry) and not re-tried
        under a fallback, so the per-request "each model tried once" contract
        holds and a (provider, key) is never hit twice.

        The start of the primary rotation is re-randomised: the first attempt is
        uniformly random among the slots that serve the requested alias, so one
        unlucky high-priority provider is not pounded first on every request.
        Fallback models stay at the tail - a cross-model jump only happens after
        every slot of the requested model has been visited, never instead of it.
        """
        pairs: list[tuple[KeySlot, str]] = []
        seen: set[tuple] = set()

        def add(target: list[tuple[KeySlot, str]], slot: KeySlot, send_alias: str) -> None:
            k = (slot.provider.name, slot.key)
            if k in seen:
                return
            seen.add(k)
            target.append((slot, send_alias))

        try:
            primary = self.pool.candidates(alias, self.s.strategy, net=self._net())
        except Exception:                                    # noqa: BLE001
            primary = []
        for s in primary:
            add(pairs, s, alias)
        # Randomize only within the requested alias: a fallback model is only
        # reached after every slot of the requested model has been visited.
        if len(pairs) > 1:
            start = random.randrange(len(pairs))
            pairs = pairs[start:] + pairs[:start]
        for fb_alias in self._serving_aliases(alias):
            try:
                fb_cands = self.pool.candidates(fb_alias, self.s.strategy, net=self._net())
            except Exception:                                # noqa: BLE001
                fb_cands = []
            for s in fb_cands:
                add(pairs, s, fb_alias)
        return pairs

    def _net(self):
        """The plane only influences ranking when it is enabled *and* smart."""
        return self.net if getattr(self.net, "smart", False) else None

    def _wait_until_any_available(self, now: float) -> float:
        """Seconds until at least one slot leaves cooldown for any alias.

        0.0 when nothing is cooling - used to pace retries between rounds when
        all live candidates failed hard (403/400/etc.) instead of being cooled.
        """
        soonest: float | None = None
        for s in self.pool.slots:
            if not s.provider.enabled:
                continue
            for exp in s.dead_until.values():
                if exp > now and (soonest is None or exp < soonest):
                    soonest = exp
        if soonest is None:
            return 0.0
        return max(0.0, soonest - now)

    def pick_egress(self, provider_name: str,
                    force: str = "") -> tuple[str, httpx.AsyncClient]:
        """(egress id, client) for a provider - ("direct", st.http) by default.

        `force` names one path and pins the call to it: that is how a probe
        measures "can this model be reached over *this* egress" instead of
        whatever the plane happens to consider best right now.
        """
        if force and force != DIRECT:
            if self.egress is not None:
                return force, self.egress.client_for(force)
            log.debug("cannot force egress '%s': no egress registry", force)
            return DIRECT, self.http
        if force == DIRECT:
            return DIRECT, self.http
        if self.net is None or not getattr(self.net.cfg, "enabled", False):
            return DIRECT, self.http
        try:
            eid = self.net.pick(provider_name)
        except Exception as e:                            # noqa: BLE001
            log.warning("net plane pick failed: %s", e)
            return DIRECT, self.http
        if self.egress is None or eid == DIRECT:
            return DIRECT, self.http
        return eid, self.egress.client_for(eid)

    def _request(self, slot: KeySlot, alias: str, payload: dict, stream: bool,
                 client: httpx.AsyncClient | None = None, lane: str = ""):
        """Build the outbound request **through the EGC standard layer**.

        Returns ``(request, egc.Wire)``. This is the one choke point every egress
        path passes, so it is where the spec's red line is enforced: EGC runs
        after `build_payload` (i.e. after DSM materialisation upstream) and
        before serialisation. The caller must feed `wire.ids` back through
        `egc.restore_ids()` on the response, or tool-call ids come home
        shortened and the tool loop breaks.
        """
        p = slot.provider
        body = up.build_payload(p, payload, self.pool.upstream_model(slot, alias))
        if stream and p.style != "anthropic":
            body["stream"] = True
            if p.style != RESPONSES_STYLE:
                body.setdefault("stream_options", {"include_usage": True})
        wire = egc.egress(body, style="anthropic" if p.style == "anthropic" else "openai",
                          lane=lane)
        # 关档时 wire is None：交回 httpx 的 json=，出网字节与接 EGC 之前逐字节相同
        payload_kw = ({"content": wire.wire} if wire.wire is not None else {"json": body})
        return ((client or self.http).build_request(
            "POST", up.chat_url(p), headers=up.headers(p, slot.key),
            timeout=p.timeout, **payload_kw), wire)


    def _detail(self, alias: str, prompt: int, completion: int, cached: int,
                cache_write: int) -> dict:
        """cost_detail with the DSM cache args, tolerating 3-arg test stubs."""
        try:
            return self.s.cost_detail(alias, prompt, completion, cached, cache_write)
        except TypeError:
            return self.s.cost_detail(alias, prompt, completion)

    def _account(self, slot: KeySlot, alias: str, status: int, t0: float,
                 usage: dict | None, stream: int, error: str = "", upstream: str = "",
                 egress: str = "", dsm: dict | None = None) -> None:
        u = usage or {}
        pd = u.get("prompt_tokens_details") or {}
        cd_ = u.get("completion_tokens_details") or {}
        prompt = int(u.get("prompt_tokens", 0) or 0)
        completion = int(u.get("completion_tokens", 0) or 0)
        cached = int(pd.get("cached_tokens", u.get("cache_read", 0)) or 0)
        cw = int(pd.get("cache_creation_input_tokens", u.get("cache_write", 0)) or 0)
        reason = int(cd_.get("reasoning_tokens", u.get("reasoning_tokens", 0)) or 0)
        answer = max(0, completion - reason)
        d = dsm or {}
        cd = self._detail(alias, prompt, completion, cached, cw)
        self.usage.log(ts=time.time(), alias=alias, upstream=upstream or alias,
                       provider=slot.provider.name, key=slot.label.split("/")[-1],
                       status=status, ms=round((time.time() - t0) * 1000, 1),
                       prompt=int(u.get("prompt_tokens", 0) or 0),
                       completion=int(u.get("completion_tokens", 0) or 0),
                       total=int(u.get("total_tokens", 0) or 0),
                       stream=stream, error=error[:300],
                       cost=cd["amount"], cost_currency=cd["currency"],
                       cost_display=cd["display"],
                       display_currency=cd["display_currency"], egress=egress,
                       sid=str(d.get("sid") or ""), cid=str(d.get("cid") or ""),
                       lane=str(d.get("lane") or ""), skill=str(d.get("skill") or ""),
                       **{"in": prompt, "out_reason": reason, "out_answer": answer,
                          "cache_read": cached, "cache_write": cw})
        a = getattr(self, "assessor", None)
        if a is not None:
            # "daily conversation" evaluation: real traffic is the sample, so
            # measuring what users feel costs no extra upstream call.
            a.observe(alias=alias, provider=slot.provider.name, egress=egress,
                      ms=round((time.time() - t0) * 1000, 1), status=status,
                      usage=u, stream=stream, error=error)

    async def embeddings(self, payload: dict, egress_out: dict | None = None) -> dict:
        # OpenAI-compatible /v1/embeddings, same pool + failover rules as chat.
        alias = str(payload.get("model") or "")
        # For embeddings, we only try the requested alias (no fallback to other models)
        # since embeddings are model-specific
        cands = self.pool.candidates(alias, self.s.strategy, embed=True, net=self._net())
        if not cands:
            raise NoUpstream(f"no embedding upstream for model '{alias}'")
        no_response = False
        for slot in cands:
            t0 = time.time()
            up_model = self.pool.upstream_model(slot, alias, embed=True)
            lease = await self.gate.acquire(alias, slot.provider.name)
            eid, client = self.pick_egress(slot.provider.name)
            if egress_out is not None:
                egress_out["egress"] = eid
            emb = dict(payload, model=up_model)
            w = egc.egress(emb, style="openai")
            req = client.build_request("POST", up.embed_url(slot.provider),
                                       headers=up.headers(slot.provider, slot.key),
                                       timeout=slot.provider.timeout,
                                       **({"content": w.wire} if w.wire is not None
                                          else {"json": emb}))
            try:
                r = await client.send(req)
                lease.release(error=r.status_code >= 400)
            except httpx.RequestError as e:
                no_response = True
                lease.release(error=True)
                slot.note_fail(self.s.cooldown)
                self._account(slot, alias, 502, t0, None, 0, str(e), egress=eid)
                continue
            if r.status_code >= 400:
                if r.status_code == 429:
                    slot.note_fail(self.s.cooldown, alias)
                else:
                    slot.note_fail(self.s.cooldown)
                detail = _safe_detail(r)
                if r.status_code in RETRY_STATUS or r.status_code == 429:
                    self._account(slot, alias, r.status_code, t0, None, 0, str(detail),
                                  egress=eid)
                    log.warning("failover from %s (%s)", slot.label, detail)
                    continue
                self._account(slot, alias, r.status_code, t0, None, 0, str(detail),
                              egress=eid)
                raise UpstreamError(r.status_code, detail, slot)
            data = r.json()
            slot.note_ok(alias)
            data["model"] = alias
            self._account(slot, alias, 200, t0, data.get("usage"), 0, upstream=up_model,
                          egress=eid)
            return data
        if no_response:
            raise UpstreamError(503, "request sent but never received a response - please resend",
                                None, code="resend")
        raise UpstreamError(502, "all embedding upstreams exhausted")

    async def complete(self, payload: dict, egress_out: dict | None = None,
                       force_egress: str = "", dsm: dict | None = None) -> dict:
        alias = str(payload.get("model") or "")
        round_no = 0
        while True:  # poll until some upstream answers - never converge to an error
            round_no += 1
            candidates = self._round_candidates(alias)
            if not candidates:
                now = time.time()
                wait = self._wait_until_any_available(now)
                if wait <= 0.0:
                    wait = 1.0
                wait = min(wait, 5.0)
                log.warning("no live slot for model '%s' (round %d): all keys cooled or unavailable, retrying in %.1fs",
                            alias, round_no, wait)
                await asyncio.sleep(wait)
                continue
            no_response_slot: KeySlot | None = None
            real_bad = False
            first_real_bad: tuple[int, str, KeySlot] | None = None
            for slot, send_alias in candidates:
                t0 = time.time()
                lease = await self.gate.acquire(send_alias, slot.provider.name)
                eid, client = self.pick_egress(slot.provider.name, force_egress)
                if egress_out is not None:
                    egress_out["egress"] = eid
                try:
                    req, wire = self._request(slot, send_alias, payload, False, client,
                                              lane=str((dsm or {}).get("lane") or ""))
                    r = await client.send(req)
                    lease.release(error=r.status_code >= 400)
                    if r.status_code >= 400:
                        detail = _safe_detail(r)
                        if r.status_code == 422 and _is_model_not_exist(detail):
                            # Routing error, not a key failure: the provider
                            # does not carry this alias.  Do not cool the key
                            # (it is healthy for every model it *does* serve)
                            # and do not count it as a bad attempt - just skip
                            # it the same way a slot that never offered the
                            # alias would be skipped.
                            log.debug("upstream %s has no model '%s' (422), skipping",
                                      slot.label, send_alias)
                            continue
                        if r.status_code == 429:
                            learn_rate_limit(slot.provider, r.headers, detail)
                            slot.note_fail(self.s.cooldown, send_alias)
                            self._account(slot, send_alias, r.status_code, t0, None, 0, str(detail),
                                          egress=eid, dsm=dsm)
                            log.warning("429 from %s (%s), cooling down and trying next",
                                        slot.label, detail)
                            continue
                        real_bad = True
                        if first_real_bad is None:
                            first_real_bad = (r.status_code, detail, slot)
                        slot.note_fail(self.s.cooldown)
                        self._account(slot, send_alias, r.status_code, t0, None, 0, str(detail),
                                      egress=eid, dsm=dsm)
                        log.warning("upstream %s failed: %s (%s), trying next",
                                    slot.label, r.status_code, detail)
                        continue
                    body = r.json()
                    if slot.provider.style == "anthropic":
                        body = up.from_anthropic(body, send_alias)
                    elif slot.provider.style == RESPONSES_STYLE:
                        body = up.from_responses(body, send_alias)
                    egc.restore_ids(body, wire.ids)
                    slot.note_ok(send_alias)
                    body["model"] = send_alias
                    self._account(slot, send_alias, r.status_code, t0, body.get("usage"), 0,
                                  upstream=self.pool.upstream_model(slot, send_alias), egress=eid,
                                  dsm=dsm)

                    # Auto-continuation on finish_reason=length (max 3 continuations)
                    messages = payload.get("messages", [])
                    if isinstance(messages, list):
                        continuation_count = 0
                        MAX_CONTINUATIONS = 3
                        while continuation_count < MAX_CONTINUATIONS:
                            choices = body.get("choices") or []
                            if not choices:
                                break
                            ch = choices[0]
                            finish = ch.get("finish_reason")
                            if finish != "length":
                                break
                            msg = ch.get("message") or {}
                            messages.append(msg)
                            next_payload = dict(payload, messages=messages)
                            next_payload.pop("max_tokens", None)
                            next_payload.pop("max_completion_tokens", None)
                            cont_t0 = time.time()
                            cont_req, cont_wire = self._request(
                                slot, send_alias, next_payload, False, client,
                                lane=str((dsm or {}).get("lane") or ""))
                            try:
                                cont_r = await client.send(cont_req)
                                if cont_r.status_code >= 400:
                                    if cont_r.status_code == 429:
                                        slot.note_fail(self.s.cooldown, send_alias)
                                        break  # treat as handled per attempt
                                    else:
                                        slot.note_fail(self.s.cooldown)
                                        real_bad = True
                                    detail = _safe_detail(cont_r)
                                    self._account(slot, send_alias, cont_r.status_code, cont_t0, None, 0,
                                                  str(detail), egress=eid, dsm=dsm)
                                    break
                                cont_body = cont_r.json()
                                if slot.provider.style == "anthropic":
                                    cont_body = up.from_anthropic(cont_body, send_alias)
                                elif slot.provider.style == RESPONSES_STYLE:
                                    cont_body = up.from_responses(cont_body, send_alias)
                                egc.restore_ids(cont_body, cont_wire.ids)
                                slot.note_ok(send_alias)
                                cont_body["model"] = send_alias
                                self._account(slot, send_alias, cont_r.status_code, cont_t0,
                                              cont_body.get("usage"), 0,
                                              upstream=self.pool.upstream_model(slot, send_alias),
                                              egress=eid, dsm=dsm)
                                cont_choices = cont_body.get("choices") or []
                                if cont_choices:
                                    cont_msg = cont_choices[0].get("message") or {}
                                    if msg.get("content") and cont_msg.get("content"):
                                        msg["content"] += cont_msg["content"]
                                    if cont_msg.get("tool_calls"):
                                        msg.setdefault("tool_calls", []).extend(cont_msg["tool_calls"])
                                    msg["finish_reason"] = cont_choices[0].get("finish_reason", "stop")
                                body = cont_body
                                continuation_count += 1
                            except httpx.RequestError:
                                slot.note_fail(self.s.cooldown)
                                no_response_slot = slot
                                break
                            except ValueError:
                                slot.note_fail(self.s.cooldown)
                                real_bad = True
                                break
                    return body
                except httpx.RequestError as e:
                    no_response_slot = slot
                    lease.release(error=True)
                    slot.note_fail(self.s.cooldown, send_alias)
                    self._account(slot, send_alias, 502, t0, None, 0, str(e), egress=eid, dsm=dsm)
                    log.warning("upstream %s error (sent, no response): %s, trying next",
                                slot.label, e)
                    continue
                except ValueError as e:
                    real_bad = True
                    if first_real_bad is None:
                        first_real_bad = (502, str(e), slot)
                    lease.release(error=True)
                    slot.note_fail(self.s.cooldown, send_alias)
                    self._account(slot, send_alias, 502, t0, None, 0, str(e), egress=eid, dsm=dsm)
                    log.warning("upstream %s error: %s, trying next", slot.label, e)
                    continue
            # Round over: every candidate failed. Cooldowns were recorded per
            # alias above, so the next _round_candidates() skips cooled keys.
            # Pace the next round only by cooldown expiry - no fixed backoff,
            # no waiting when nothing is cooling - and poll again. Never error.
            now = time.time()
            wait = max(0.0, min(self._wait_until_any_available(now), 5.0))
            if wait > 0:
                log.warning("all %d candidate(s) failed for model '%s' (round %d): retrying in %.1fs",
                            len(candidates), alias, round_no, wait)
                await asyncio.sleep(wait)

    async def open_stream(self, payload: dict, force_egress: str = "",
                          dsm: dict | None = None):
        """Try each available model until one opens a stream.

        Polls round after round until some upstream opens a stream - never
        converges to an error. Returns ``(slot, send_alias, t0, response)``.
        """
        alias = str(payload.get("model") or "")
        round_no = 0
        while True:  # poll until some upstream opens a stream
            round_no += 1
            candidates = self._round_candidates(alias)
            if not candidates:
                now = time.time()
                wait = self._wait_until_any_available(now)
                if wait <= 0.0:
                    wait = 1.0
                wait = min(wait, 5.0)
                log.warning("no live slot for model '%s' (round %d): all keys cooled or unavailable, retrying in %.1fs",
                            alias, round_no, wait)
                await asyncio.sleep(wait)
                continue
            no_response_slot: KeySlot | None = None
            real_bad = False
            first_real_bad: tuple[int, str, KeySlot] | None = None
            for slot, send_alias in candidates:
                t0 = time.time()
                lease = await self.gate.acquire(send_alias, slot.provider.name)
                eid, client = self.pick_egress(slot.provider.name, force_egress)
                try:
                    req, wire = self._request(slot, send_alias, payload, True, client,
                                              lane=str((dsm or {}).get("lane") or ""))
                    r = await client.send(req, stream=True)
                    r._sms_egc = wire
                    if r.status_code >= 400:
                        await r.aread()
                        lease.release(error=True)
                        detail = _safe_detail(r)
                        if r.status_code == 422 and _is_model_not_exist(detail):
                            log.debug("upstream %s has no model '%s' (422), skipping",
                                      slot.label, send_alias)
                            continue
                        if r.status_code == 429:
                            learn_rate_limit(slot.provider, r.headers, detail)
                            slot.note_fail(self.s.cooldown, send_alias)
                            log.warning("429 from %s (%s), cooling down and trying next",
                                        slot.label, detail)
                            continue
                        real_bad = True
                        if first_real_bad is None:
                            first_real_bad = (r.status_code, detail, slot)
                        slot.note_fail(self.s.cooldown)
                        log.warning("upstream %s failed: %s (%s), trying next",
                                    slot.label, r.status_code, detail)
                        continue
                    r._sms_lease = lease
                    r._sms_egress = eid
                    r._sms_dsm = dsm or {}
                    r._sms_original_payload = payload
                    r._sms_messages = list(payload.get("messages") or [])
                    r._sms_force_egress = force_egress
                    return slot, send_alias, t0, r
                except httpx.RequestError as e:
                    no_response_slot = slot
                    lease.release(error=True)
                    slot.note_fail(self.s.cooldown, send_alias)
                    log.warning("upstream %s error (sent, no response): %s, trying next",
                                slot.label, e)
                    continue
            # Round over: every candidate failed to open. Cooldowns recorded
            # per alias above; pace only by cooldown expiry - no fixed backoff,
            # no waiting when nothing is cooling - and poll the next round.
            # Never error out.
            now = time.time()
            wait = max(0.0, min(self._wait_until_any_available(now), 5.0))
            if wait > 0:
                log.warning("all %d candidate(s) failed to open a stream for model '%s' (round %d): retrying in %.1fs",
                            len(candidates), alias, round_no, wait)
                await asyncio.sleep(wait)

    async def wrap_stream(self, slot: KeySlot, alias: str, t0: float, r: httpx.Response):
        usage: dict = {}
        # R6 反查表随请求存活：流里每个 tool_calls delta 都要换回原 id
        idmap = getattr(getattr(r, "_sms_egc", None), "ids", None)
        # Auto-continuation state
        messages = getattr(r, "_sms_messages", None)
        if messages is None:
            messages = []
        cont_count = 0
        MAX_CONTINUATIONS = 3
        force_egress = getattr(r, "_sms_force_egress", "")
        dsm = getattr(r, "_sms_dsm", {})
        original_payload = getattr(r, "_sms_original_payload", {})
        current_r = r
        current_idmap = idmap
        current_slot = slot
        opened = [r]
        leases = [getattr(r, "_sms_lease", None)]
        ok_slot: KeySlot | None = None

        class StreamState:
            __slots__ = ("finish_reason", "finish_seen", "accumulated_content",
                         "accumulated_tool_calls", "local_usage")
            def __init__(self):
                self.finish_reason = "stop"
                self.finish_seen = False
                self.accumulated_content = ""
                self.accumulated_tool_calls: dict[int, dict] = {}
                self.local_usage = {}

        def _finish_chunk(reason: str) -> bytes:
            return up.sse({"id": "chatcmpl-resume", "object": "chat.completion.chunk",
                           "created": int(time.time()), "model": alias,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})

        async def _resume_attempt():
            cont_payload = dict(original_payload, messages=messages)
            cont_payload.pop("max_tokens", None)
            cont_payload.pop("max_completion_tokens", None)
            cont_payload["stream"] = True
            # Stream resume stays on the SAME model that opened the stream
            # (alias == send_alias here): switching models mid-stream would
            # break context continuity. So pull slots serving this one alias
            # only, not the cross-model fallback pool.
            try:
                candidates = list(self.pool.candidates(alias, self.s.strategy,
                                                       net=self._net()))
            except Exception:                                # noqa: BLE001
                candidates = []
            # A dropped stream is resumed by trying the slot that just served
            # it first (it already has the context in-flight on the provider
            # side); only then fall back to the rest of the round.  This is
            # per-stream resume scope, not the cross-request sticky preference
            # that was removed from complete/open_stream.
            if current_slot is not None and not any(c is current_slot for c in candidates):
                candidates = [current_slot] + candidates
            for cand in candidates:
                lease = await self.gate.acquire(alias, cand.provider.name)
                eid, client = self.pick_egress(cand.provider.name, force_egress)
                try:
                    req, wire = self._request(cand, alias, cont_payload, True, client,
                                              lane=str(dsm.get("lane") or ""))
                    cont_r = await client.send(req, stream=True)
                    cont_r._sms_egc = wire
                    if cont_r.status_code >= 400:
                        await cont_r.aread()
                        lease.release(error=True)
                        if cont_r.status_code == 429:
                            cand.note_fail(self.s.cooldown, alias)
                        else:
                            cand.note_fail(self.s.cooldown)
                        continue
                    cont_r._sms_lease = lease
                    cont_r._sms_egress = eid
                    return cont_r, wire.ids, cand
                except httpx.RequestError as e:
                    lease.release(error=True)
                    cand.note_fail(self.s.cooldown)
                    log.warning("stream resume to %s failed: %s", cand.label, e)
                    continue
            return None, None, None

        async def _stream_once(current_slot, current_alias, current_r, current_idmap, state):
            """Stream from a single response, yield chunks, update state."""
            if current_slot.provider.style == "anthropic":
                async for c in up.anthropic_stream_to_openai(current_r.aiter_bytes(), current_alias):
                    if c.get("usage"):
                        state.local_usage = c["usage"]
                    egc.restore_ids(c, current_idmap)
                    yield up.sse(c)
                    ch = (c.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        state.accumulated_content += delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        e = state.accumulated_tool_calls.setdefault(idx,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        e["id"] = e["id"] or (tc.get("id") or "")
                        fn = tc.get("function") or {}
                        e["function"]["name"] += fn.get("name") or ""
                        e["function"]["arguments"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        state.finish_reason = ch["finish_reason"]
                        state.finish_seen = True
                yield b"data: [DONE]\n\n"
            elif current_slot.provider.style == RESPONSES_STYLE:
                async for c in up.responses_stream_to_openai(current_r.aiter_bytes(), current_alias):
                    if c.get("usage"):
                        state.local_usage = c["usage"]
                    egc.restore_ids(c, current_idmap)
                    yield up.sse(c)
                    ch = (c.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        state.accumulated_content += delta["content"]
                    if ch.get("finish_reason"):
                        state.finish_reason = ch["finish_reason"]
                        state.finish_seen = True
                yield b"data: [DONE]\n\n"
            else:
                buf = b""
                async for raw in current_r.aiter_raw():
                    buf += raw
                    while b"\n\n" in buf:
                        evt, buf = buf.split(b"\n\n", 1)
                        out, u = _normalize_event(evt, current_alias, current_idmap)
                        state.local_usage = u or state.local_usage
                        yield out
                        for line in out.split(b"\n"):
                            s = line.strip()
                            if s.startswith(b"data:"):
                                payload = s[5:].strip()
                                if payload == b"[DONE]":
                                    state.finish_seen = True
                                    continue
                                if payload not in (b"", b"[DONE]"):
                                    try:
                                        obj = json.loads(payload)
                                        ch = (obj.get("choices") or [{}])[0]
                                        delta = ch.get("delta") or {}
                                        if delta.get("content"):
                                            state.accumulated_content += delta["content"]
                                        for tc in delta.get("tool_calls") or []:
                                            idx = int(tc.get("index") or 0)
                                            e = state.accumulated_tool_calls.setdefault(idx,
                                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                            e["id"] = e["id"] or (tc.get("id") or "")
                                            fn = tc.get("function") or {}
                                            e["function"]["name"] += fn.get("name") or ""
                                            e["function"]["arguments"] += fn.get("arguments") or ""
                                        if ch.get("finish_reason"):
                                            state.finish_reason = ch["finish_reason"]
                                            state.finish_seen = True
                                    except Exception:
                                        pass
                if buf.strip():
                    out, u = _normalize_event(buf, current_alias, current_idmap)
                    state.local_usage = u or state.local_usage
                    yield out
                    for line in out.split(b"\n"):
                        s = line.strip()
                        if s.startswith(b"data:"):
                            payload = s[5:].strip()
                            if payload == b"[DONE]":
                                state.finish_seen = True
                            elif payload not in (b"", b"[DONE]"):
                                try:
                                    obj = json.loads(payload)
                                    ch = (obj.get("choices") or [{}])[0]
                                    if ch.get("finish_reason"):
                                        state.finish_reason = ch["finish_reason"]
                                        state.finish_seen = True
                                except Exception:
                                    pass

        try:
            while cont_count <= MAX_CONTINUATIONS:
                state = StreamState()
                dropped = False
                stream_gen = _stream_once(current_slot, alias, current_r, current_idmap, state)
                try:
                    async for chunk in stream_gen:
                        yield chunk
                except httpx.RequestError as e:
                    dropped = True
                    log.warning("upstream %s stream dropped mid-way: %s", current_slot.label, e)
                    if getattr(current_r, "_sms_lease", None) is not None:
                        getattr(current_r, "_sms_lease", None).release(error=True)
                    current_slot.note_fail(self.s.cooldown)
                    self._account(current_slot, alias, 502, t0, None, 1, str(e),
                                  egress=getattr(current_r, "_sms_egress", ""), dsm=dsm)
                    await current_r.aclose()
                else:
                    ok_slot = current_slot
                usage = state.local_usage or usage
                truncated = dropped or (not state.finish_seen and bool(
                    state.accumulated_content or state.accumulated_tool_calls))
                if state.finish_reason != "length" and not truncated:
                    break
                if cont_count >= MAX_CONTINUATIONS:
                    if truncated:
                        yield _finish_chunk("length")
                    break
                if truncated:
                    state.finish_reason = "length"
                tool_calls_list = [state.accumulated_tool_calls[i] for i in sorted(state.accumulated_tool_calls)]
                if state.accumulated_content or tool_calls_list:
                    cont_msg = {"role": "assistant", "content": state.accumulated_content}
                    if tool_calls_list:
                        cont_msg["tool_calls"] = tool_calls_list
                    messages.append(cont_msg)
                cont_r, nidmap, cand = await _resume_attempt()
                if cont_r is None:
                    log.warning("stream resume exhausted for alias=%s after %d attempt(s)",
                                alias, cont_count + 1)
                    yield _finish_chunk("length")
                    break
                leases.append(getattr(cont_r, "_sms_lease", None))
                opened.append(cont_r)
                current_r, current_idmap = cont_r, nidmap
                current_slot = cand
                cont_count += 1
        finally:
            for resp in opened:
                try:
                    await resp.aclose()
                except Exception:
                    pass
            for lease in leases:
                if lease is not None:
                    try:
                        lease.release()
                    except Exception:
                        pass
            last = ok_slot or slot
            last.note_ok(alias)
            self._account(last, alias, 200, t0, usage, 1,
                          upstream=self.pool.upstream_model(last, alias),
                          egress=getattr(current_r, "_sms_egress", ""),
                          dsm=dsm)


def _normalize_event(evt: bytes, alias: str, ids=None) -> tuple:
    """Rewrite one SSE event: guarantee object + public model, sniff usage."""
    out, usage = [], {}
    for line in evt.split(b"\n"):
        s = line.strip()
        if not s.startswith(b"data:"):
            out.append(line)
            continue
        payload = s[5:].strip()
        if payload in (b"", b"[DONE]"):
            out.append(line)
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            out.append(line)
            continue
        obj.setdefault("object", "chat.completion.chunk")
        obj["model"] = alias
        if ids:
            egc.restore_ids(obj, ids)
        if obj.get("usage"):
            usage = obj["usage"]
        out.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    return b"\n".join(out) + b"\n\n", usage

def _sniff_usage(chunk: bytes, cur: dict) -> dict:
    for line in chunk.split(b"\n"):
        s = line.decode("utf-8", "ignore").strip()
        if s.startswith("data:") and "usage" in s:
            try:
                u = json.loads(s[5:].strip()).get("usage")
                if u:
                    return u
            except Exception:
                pass
    return cur


def _safe_detail(r) -> str:
    try:
        j = r.json()
        err = j.get("error") if isinstance(j.get("error"), dict) else {}
        return str(err.get("message") or j.get("detail") or j)[:200] if j else r.text[:200]
    except Exception:
        return r.text[:200]
