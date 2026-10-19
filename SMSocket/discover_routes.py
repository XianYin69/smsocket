"""Discovery routes: probe providers, cache the catalog, publish aliases.
  POST  /admin/discover                 probe one/every provider (sync or async job)
  GET   /admin/discover/status          progress of the running probe
  GET   /admin/models                   cached catalog (no upstream traffic)
  GET   /admin/provider-models          two-level tree: provider -> its models
  POST  /admin/provider-models/refresh  re-probe whatever went stale, by itself
  DELETE /admin/models                  drop the cache (?provider= to scope it)
  POST  /admin/models/apply             write probed ids into provider aliases + pricing
`apply` only ever *adds* aliases and refuses to touch a name already published,
so discovery can never silently break a client that is using an existing alias.
"""
from __future__ import annotations
import asyncio
import logging
import time
from fastapi import APIRouter, HTTPException, Request
from .discover import TEST_PROMPT, Catalog, Discoverer
from .providers import mask
log = logging.getLogger("smssocket")
def publish_aliases(st, want=None, prefix: str = "") -> tuple[list, list]:
    """Publish cached catalog rows as provider aliases (adds only, never overwrites).
    Shared by `POST /admin/models/apply` (operator clicks) and `auto_discover`
    (a provider was just added), so both paths obey the same rule: a name that
    already exists is never touched, because some client is already using it.
    Writes the config file only when something was actually added.
    """
    from .admin import cfg_path, read_raw, write_raw
    want = {str(x) for x in want} if want else None
    rows = [x for x in st.catalog.all() if x.get("ok")
            and (not want or x["provider"] in want)]
    if not rows:
        return [], []
    path = cfg_path()
    data = read_raw(path)
    provs = data.get("providers") or []
    added, skipped = [], []
    for p in provs:
        if not isinstance(p, dict):
            continue
        if want and str(p.get("name")) not in want:
            continue
        models = dict(p.get("models") or {})
        for row in [x for x in rows if x["provider"] == str(p.get("name"))]:
            alias = prefix + row["model"]
            if alias in models:
                skipped.append(alias)
                continue
            if not prefix and row["model"] in set(models.values()):
                skipped.append(alias)      # an existing alias already serves it
                continue
            if any(alias == str(k) for q in provs if isinstance(q, dict)
                   for k in (q.get("models") or {})):
                skipped.append(alias)
                continue
            models[alias] = row["model"]
            added.append(f"{p.get('name')}/{alias}")
        if models:
            p["models"] = models
    if added:
        data["providers"] = provs
        write_raw(path, data)
    return added, skipped
async def auto_discover(st, names: list[str]) -> dict:
    """Probe -> publish -> measure a provider that just appeared. Never raises.
    This is what makes "I added a model" enough: the operator does not press a
    probe button, does not copy ids into the alias box and does not schedule a
    reachability run - the gateway does all three and the results feed the
    automatic weight/priority tuning.
    """
    from .admin import apply_state
    cfg = st.settings.discover
    out = {"providers": list(names), "added": [], "skipped": [], "assessed": False,
           "rpm_learned": {}}
    try:
        want = set(names)
        provs = [p for p in st.settings.providers if p.name in want]
        if not provs or st.http is None:
            return out
        d = Discoverer(st.settings, st.http, st.catalog)
        kw = dict(models=None, test_prompt=TEST_PROMPT,
                  probe_params=cfg.probe_params, probe_stream=cfg.probe_stream,
                  probe_embeddings=False, concurrency=max(1, min(cfg.concurrency, 64)),
                  timeout=cfg.timeout, max_models=cfg.max_models, max_params=14)
        for p in provs:
            try:
                rep = await d.probe_provider(p, **kw)
                st.catalog.put(rep.get("models") or [])
                out.setdefault("probed", []).append(p.name)
                # A provider that just appeared has no learned RPM yet: fire a
                # controlled burst so the sliding-window limiter learns the
                # free-tier cap on its own.  Skipped when the config file
                # already names a cap - an explicit number always wins.
                if int(getattr(p, "max_rpm", 0) or 0) == 0:
                    rl = await d.probe_rate_limit(
                        p, burst=40, timeout=cfg.timeout,
                        model=next(iter(p.models.values()), None) if p.models else None)
                    out["rpm_learned"][p.name] = rl.get("max_rpm")
                    if rl.get("max_rpm"):
                        log.info("learned rpm cap %d for provider '%s'",
                                 rl["max_rpm"], p.name)
            except Exception as e:                            # noqa: BLE001
                log.warning("auto discover: probe of %s failed: %s", p.name, e)
        if cfg.apply_aliases:
            added, skipped = publish_aliases(st, want)
            out["added"], out["skipped"] = added, skipped
            if added:
                apply_state(st)
        if cfg.auto_assess and st.assessor is not None:
            mods = [a.split("/", 1)[1] for a in out["added"] if "/" in a] or None
            try:
                await st.assessor.sweep(models=mods)
                out["assessed"] = True
            except Exception as e:                            # noqa: BLE001
                log.warning("auto discover: assess sweep failed: %s", e)
        log.info("auto discover for %s: %d aliases added, %d skipped",
                 ",".join(names), len(out["added"]), len(out["skipped"]))
    except Exception as e:                                    # noqa: BLE001
        log.warning("auto discover failed: %s", e)
    return out
FRESH_SECONDS = 900          # a provider with no probe this fresh gets re-probed


def _by_provider(st) -> dict:
    """Catalog rows grouped by provider name (no upstream traffic)."""
    by: dict[str, list] = {}
    for row in st.catalog.all():
        by.setdefault(str(row.get("provider") or ""), []).append(row)
    for rows in by.values():
        rows.sort(key=lambda r: str(r.get("model") or ""))
    return by


def _freshness(rows: list) -> tuple:
    """(newest ts, how many ok) for one provider's cached rows."""
    ts = [float(r.get("ts") or 0.0) for r in rows]
    return (max(ts) if ts else 0.0, sum(1 for r in rows if r.get("ok")))


def _tree(st) -> list:
    """Provider -> model rows, merging what is published with what was probed.

    Level 1 is the provider as the operator typed it; level 2 is every model the
    gateway knows that provider serves - the ids a probe found plus the aliases
    already in the config. A model that was never probed shows `probed: False`
    so the console can ask for a refresh instead of the operator pressing a
    button.

    A model the operator rejected (deleted from the cache) is shown too, as a
    `denied` row: the next freshness pass would otherwise re-list and re-probe
    it, so the denial has to be visible and reversible from the same tree.
    """
    by = _by_provider(st)
    denied = st.catalog.denied()
    out = []
    for p in st.settings.providers:
        rows = by.get(p.name, [])
        index = {str(r.get("model")): r for r in rows}
        newest, ok_n = _freshness(rows)
        models = []
        seen = set()
        for alias, upstream in sorted((p.models or {}).items()):
            r = index.get(str(upstream))
            seen.add(str(upstream))
            models.append({
                "alias": alias, "upstream": str(upstream), "published": True,
                "denied": (p.name, str(upstream)) in denied,
                "embed": False, "probed": bool(r),
                "ok": bool(r and r.get("ok")), "status": (r or {}).get("status", 0),
                "latency_ms": (r or {}).get("latency_ms", 0.0),
                "context": (r or {}).get("context"),
                "params": (r or {}).get("params") or {},
                "stream": (r or {}).get("stream") or "",
                "error": (r or {}).get("error") or "",
                "ts": (r or {}).get("ts") or 0.0})
        for alias, upstream in sorted((p.embeddings or {}).items()):
            r = index.get(str(upstream))
            seen.add(str(upstream))
            models.append({
                "alias": alias, "upstream": str(upstream), "published": True,
                "denied": (p.name, str(upstream)) in denied,
                "embed": True, "probed": bool(r),
                "ok": bool(r and r.get("ok")), "status": (r or {}).get("status", 0),
                "latency_ms": (r or {}).get("latency_ms", 0.0),
                "context": (r or {}).get("context"), "params": {},
                "stream": "", "error": (r or {}).get("error") or "",
                "ts": (r or {}).get("ts") or 0.0})
        for mid, r in sorted(index.items()):
            if mid in seen:
                continue
            models.append({
                "alias": None, "upstream": mid, "published": False,
                "denied": (p.name, mid) in denied,
                "embed": str(r.get("embeddings") or "") == "supported",
                "probed": True, "ok": bool(r.get("ok")), "status": r.get("status", 0),
                "latency_ms": r.get("latency_ms", 0.0), "context": r.get("context"),
                "params": r.get("params") or {}, "stream": r.get("stream") or "",
                "error": r.get("error") or "", "ts": r.get("ts") or 0.0})
        # a rejected id that has no cached row at all: still worth showing
        for mid in sorted(m for (prov, m) in denied if prov == p.name):
            if mid in seen:
                continue
            seen.add(mid)
            models.append({
                "alias": None, "upstream": mid, "published": False, "denied": True,
                "embed": False, "probed": False, "ok": False, "status": 0,
                "latency_ms": 0.0, "context": None, "params": {}, "stream": "",
                "error": "operator rejected - skipped by the discoverer",
                "ts": 0.0})
        out.append({
            "name": p.name, "base_url": p.base_url, "style": p.style,
            "enabled": bool(p.enabled), "key_count": len([k for k in p.keys if k]),
            "priority": p.priority, "weight": p.weight, "auto": bool(p.auto),
            "timeout": p.timeout, "max_rpm": p.max_rpm,
            "probed_at": newest, "ok_models": ok_n, "model_count": len(models),
            "stale": (not rows) or (time.time() - newest > FRESH_SECONDS),
            "probe_note": _why_not(p),
            "error": (rows[0].get("error") if rows else "") or "",
            "models": models})
    out.sort(key=lambda x: (-int(bool(x["enabled"])), str(x["name"])))
    return out


def _why_not(p) -> str:
    """Why the console cannot show measured parameters for this provider yet."""
    if not p.enabled:
        return "已停用，不参与自动探测"
    if not [k for k in p.keys if k] and not getattr(p, "keyless", False):
        return "无密钥，无法自动探测"
    return ""


def _busy(st) -> bool:
    """True while a manual/async probe job is still running."""
    pr = getattr(st, "progress", None) or {}
    task = pr.get("task")
    return bool(task is not None and not task.done())


async def _probe_tick(st, gap: float) -> list:
    """One scheduled freshness pass: probe the stale providers, return their names.

    Returns [] (and touches nothing) when a probe is already going, when the
    gateway has no client yet, or when everything is fresh - so the loop can run
    forever without ever doubling up on a provider.
    """
    cfg = getattr(st.settings, "discover", None)
    if cfg is None or not getattr(cfg, "on_add", True):
        return []
    if getattr(st, "auto_probe", None) or _busy(st) or st.http is None:
        return []
    names = _stale_names(st, gap)
    if not names:
        return []
    st.auto_probe = set(names)
    log.info("scheduled probe: %s", ",".join(names))
    try:
        await auto_discover(st, names)
    except asyncio.CancelledError:
        raise
    except Exception as e:                                    # noqa: BLE001
        log.warning("scheduled probe failed: %s", e)
    finally:
        st.auto_probe = set()
    return names


def _probe_loop(st):
    """Background freshness loop: probe what went stale even if nobody opens
    the console. Returns the task so the lifespan can cancel it on shutdown.
    """
    async def loop():
        first = True
        while True:
            cfg = getattr(st.settings, "discover", None)
            if cfg is None or not getattr(cfg, "on_add", True):
                await asyncio.sleep(60.0)
                continue
            gap = max(60, int(getattr(cfg, "fresh_seconds", FRESH_SECONDS) or 0))
            # a restart leaves the catalog as it was; warm it up shortly instead
            # of making the operator stare at an empty tree for one whole interval
            await asyncio.sleep(5.0 if first else gap)
            first = False
            try:
                await _probe_tick(st, gap)
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                log.warning("probe loop failed: %s", e)
                await asyncio.sleep(5.0)
    return asyncio.create_task(loop(), name="sms-probe-loop")


def _stale_names(st, max_age: float) -> list:
    """Providers with no cached probe, or one older than `max_age` seconds."""
    by = _by_provider(st)
    want = []
    now = time.time()
    for p in st.settings.providers:
        if not p.enabled or (not [k for k in p.keys if k]
                             and not getattr(p, "keyless", False)):
            continue
        newest, _ = _freshness(by.get(p.name, []))
        if not newest or now - newest > max_age:
            want.append(p.name)
    return want


async def _auto_probe(st, names: list[str]) -> None:
    """auto_discover with the in-flight marker cleaned up either way."""
    try:
        await auto_discover(st, names)
    finally:
        st.auto_probe = set()


def build(st, dep) -> APIRouter:
    r = APIRouter(dependencies=dep)
    def discoverer() -> Discoverer:
        if st.http is None:
            raise HTTPException(503, detail={"error": {
                "message": "gateway not started", "type": "server_error"}})
        return Discoverer(st.settings, st.http)
    def targets(body: dict) -> list:
        names = body.get("providers") or body.get("provider")
        provs = list(st.settings.providers)
        if isinstance(names, str):
            names = [names]
        if names:
            want = {str(n) for n in names}
            provs = [p for p in provs if p.name in want]
            missing = want - {p.name for p in provs}
            if missing:
                raise HTTPException(404, detail={"error": {
                    "message": f"unknown provider(s): {', '.join(sorted(missing))}",
                    "type": "invalid_request_error"}})
        adhoc = body.get("target")
        if isinstance(adhoc, dict) and adhoc.get("base_url"):
            from .config import ProviderSpec
            keys = adhoc.get("keys") or []
            if not keys:
                p0 = next((p for p in st.settings.providers
                           if p.base_url.rstrip("/") == str(adhoc["base_url"]).rstrip("/")), None)
                keys = list(p0.keys) if p0 else []
            if not keys:
                raise HTTPException(400, detail={"error": {
                    "message": "target needs keys (or a matching configured provider)",
                    "type": "invalid_request_error"}})
            provs = [ProviderSpec(name=str(adhoc.get("name") or "adhoc"),
                                  base_url=str(adhoc["base_url"]), keys=[str(k) for k in keys],
                                  style=str(adhoc.get("style") or "openai"))]
        if not provs:
            raise HTTPException(400, detail={"error": {
                "message": "nothing to probe", "type": "invalid_request_error"}})
        return provs
    def opts(body: dict) -> dict:
        return dict(
            models=body.get("models") or None,
            test_prompt=str(body.get("test_prompt") or TEST_PROMPT),
            probe_params=bool(body.get("probe_params", True)),
            probe_stream=bool(body.get("probe_stream", True)),
            probe_embeddings=bool(body.get("probe_embeddings", False)),
            concurrency=max(1, min(int(body.get("concurrency", 8) or 8), 64)),
            timeout=float(body.get("timeout", 30.0) or 30.0),
            max_models=max(1, min(int(body.get("max_models", 200) or 200), 2000)),
            max_params=max(0, min(int(body.get("max_params", 14) or 14), 40)))
    @r.post("/admin/discover")
    async def discover(request: Request):
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        provs, kw = targets(body), opts(body)
        as_job = bool(body.get("async") or body.get("async_"))
        d = discoverer()
        st.progress = {"started": time.time(), "done": 0, "total": 0, "models": [],
                       "providers": [p.name for p in provs], "status": "running",
                       "concurrency": kw["concurrency"], "results": []}
        async def probe_one(p):
            rep = await d.probe_provider(p, progress=lambda r: _tick(r), **kw)
            st.progress["results"].append({k: v for k, v in rep.items() if k != "models"})
            st.catalog.put(rep["models"])
            # Learn the provider's real RPM on the same pass, but only when the
            # config has not pinned a cap by hand - an explicit number wins.
            if int(getattr(p, "max_rpm", 0) or 0) == 0 and rep.get("ok"):
                rl = await d.probe_rate_limit(p, burst=40, timeout=kw["timeout"],
                                               model=next(iter(p.models.values()), None)
                                               if p.models else None)
                st.progress.setdefault("rpm", {})[p.name] = rl.get("max_rpm")
            return rep
        async def run_all():
            reps = await asyncio.gather(*[probe_one(p) for p in provs],
                                        return_exceptions=True)
            out = []
            for rep in reps:
                if isinstance(rep, Exception):
                    out.append({"error": str(rep)[:300], "models": []})
                else:
                    out.append(rep)
            st.progress["status"] = "done"
            st.progress["finished"] = time.time()
            st.progress["report"] = out
            return out
        if as_job:
            st.progress["task"] = asyncio.create_task(run_all())
            return {"accepted": True, "providers": [p.name for p in provs],
                    "poll": "/admin/discover/status"}
        report = await run_all()
        out = {"providers": report, "catalog": {"cached": len(st.catalog.all()),
                                                "last_seen": st.catalog.last_seen()}}
        if st.progress.get("rpm"):
            out["rpm_learned"] = st.progress["rpm"]
        return out
    def _tick(row) -> None:
        st.progress["done"] += 1
        st.progress["models"].append({"provider": row.provider, "model": row.model,
                                      "ok": row.ok, "status": row.status,
                                      "latency_ms": row.latency_ms,
                                      "params": sum(1 for v in row.params.values()
                                                    if v == "supported")})
    @r.get("/admin/discover/status")
    async def discover_status():
        pr = getattr(st, "progress", None)
        if not pr:
            return {"status": "idle"}
        out = {k: v for k, v in pr.items() if k != "task"}
        out["running"] = bool(pr.get("task") and not pr["task"].done())
        out["elapsed"] = round(time.time() - pr["started"], 2)
        out["active"] = st.meter.active
        return out
    @r.get("/admin/models")
    async def models_cached(provider: str = ""):
        rows = st.catalog.all(provider)
        return {"object": "list", "count": len(rows), "last_seen": st.catalog.last_seen(),
                "published": sorted(st.settings.model_index()),
                "data": rows}
    @r.delete("/admin/models")
    async def models_clear(provider: str = ""):
        return {"cleared": st.catalog.clear(provider), "provider": provider or "*"}

    @r.delete("/admin/models/{provider}/{model}")
    async def models_delete(provider: str, model: str):
        """Delete one cached probe row - the console's "删除" on a 未发布 model.

        The tree shows every id the gateway ever probed, including ones that were
        never published as an alias (alias: null). Those rows live only in the
        catalog; this route drops the single one the operator picked, without
        touching the provider's config or any other cached model.

        Dropping the row alone would not stop the next freshness pass from
        re-listing and re-probing the same id, so the operator's rejection is
        also recorded in `denied` - the discoverer skips those pairs until the
        operator clears the denial with `POST /admin/models/{p}/{m}/undeny`.
        """
        n = st.catalog.delete(provider, model)
        if not n:
            raise HTTPException(404, detail={"error": {
                "message": f"no cached probe for {provider}/{model}",
                "type": "invalid_request_error"}})
        st.catalog.deny(provider, model)
        return {"ok": True, "deleted": n, "provider": provider, "model": model,
                "denied": True}

    @r.post("/admin/models/{provider}/{model}/undeny")
    async def models_undeny(provider: str, model: str):
        """Clear a denial so the next freshness pass may probe the id again."""
        n = st.catalog.undeny(provider, model)
        return {"ok": True, "cleared": n, "provider": provider, "model": model}
    @r.post("/admin/models/apply")
    async def models_apply(request: Request):
        """Publish probed models as aliases (adds only, never overwrites)."""
        from .admin import apply_state
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        names = body.get("providers") or body.get("provider")
        if isinstance(names, str):
            names = [names]
        want = {str(n) for n in (names or [])} or None
        rows = [x for x in st.catalog.all() if x.get("ok")
                and (not want or x["provider"] in want)]
        if not rows:
            raise HTTPException(404, detail={"error": {
                "message": "nothing probed yet - POST /admin/discover first",
                "type": "invalid_request_error"}})
        prefix = str(body.get("alias_prefix") or "")
        added, skipped = publish_aliases(st, want, prefix)
        if not added:
            return {"ok": True, "added": [], "skipped": skipped,
                    "note": "every probed model is already published",
                    "applied": {"providers": len(st.settings.providers),
                                "keys": len(st.pool.slots),
                                "models": len(st.settings.model_index())}}
        applied = None
        try:
            applied = apply_state(st)
        except HTTPException:
            raise
        return {"ok": True, "added": added, "skipped": skipped, "applied": applied}
    @r.get("/admin/provider-models")
    async def provider_models():
        """Two-level list for the console: providers, then the models under each."""
        tree = _tree(st)
        return {"object": "list", "count": len(tree),
                "models": sum(x["model_count"] for x in tree),
                "last_seen": st.catalog.last_seen(), "fresh_seconds": FRESH_SECONDS,
                "data": tree}

    @r.post("/admin/provider-models/refresh")
    async def provider_models_refresh(request: Request):
        """Auto-probe: the console asks on page load, nobody presses a probe button.

        Only the stale providers are touched, and a run that is already going is
        never doubled - so refreshing the page costs nothing.
        """
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        cfg = getattr(st.settings, "discover", None)
        max_age = float(body.get("max_age") or
                        getattr(cfg, "fresh_seconds", FRESH_SECONDS) or FRESH_SECONDS)
        names = _stale_names(st, max_age)
        running = _busy(st)
        inflight = getattr(st, "auto_probe", None) or set()
        if body.get("all"):
            names = [p.name for p in st.settings.providers
                     if p.enabled and [k for k in p.keys if k]]
        if body.get("providers"):
            req = body.get("providers")
            req = [req] if isinstance(req, str) else list(req)
            known = {p.name for p in st.settings.providers}
            bad = [str(n) for n in req if str(n) not in known]
            if bad:
                raise HTTPException(404, detail={"error": {
                    "message": f"unknown provider(s): {', '.join(sorted(bad))}",
                    "type": "invalid_request_error"}})
            names = sorted(set(str(n) for n in req))
        if not names:
            blocked = sorted({b for b in (_why_not(p) for p in st.settings.providers) if b})
            return {"queued": [], "skipped": [], "running": running,
                    "note": ("; ".join(blocked) if blocked
                             else "every provider is already fresh")}
        if running or inflight:
            return {"queued": [], "skipped": names, "running": True,
                    "note": "a probe is already in flight"}
        if cfg is not None and not getattr(cfg, "on_add", True):
            return {"queued": [], "skipped": names, "running": False,
                    "note": "discover.on_add is off"}
        if st.http is None:
            raise HTTPException(503, detail={"error": {
                "message": "gateway not started", "type": "server_error"}})
        st.auto_probe = set(names)
        try:
            asyncio.create_task(_auto_probe(st, names))
        except RuntimeError:
            st.auto_probe = set()
            return {"queued": [], "skipped": names, "running": False,
                    "note": "no event loop"}
        return {"queued": names, "skipped": [], "running": True,
                "poll": "/admin/discover/status"}

    return r
