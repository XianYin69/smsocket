"""Clash net-plane: external-controller client, per-egress clients, scoring.

A "net plane" is the layer between SMSocket and the wire: which egress path
(direct, or a clash proxy) a provider call leaves through. Everything here is
optional - with `clash.enabled: false` (the default) no controller is
contacted, no extra client is built, and the gateway behaves exactly as before.

Config block (all keys optional, see config.example.yaml):
  clash:
    enabled: false
    controller: http://127.0.0.1:9090      # clash external-controller REST API
    secret: ${CLASH_SECRET}                # Bearer token, kept out of the file
    mixed_port: 7890                       # clash mixed-port -> http proxy url
    proxy_url: ""                          # explicit override of mixed_port
    health_url: https://www.google.com/generate_204
    timeout: 5                             # seconds per controller call
    interval: 60                           # seconds between background re-probes
    mode: auto                             # auto | direct | proxy
    groups: []                             # proxy-provider groups to track
    provider_proxy: {openai: "PROXY-GRP"}  # static hint, provider -> group/node
    smart: true                            # score paths and pick the best one
    min_delay_ms: 0                        # hysteresis before leaving direct
    fail_ratio: 0.5                        # above this a path counts as dead

Only stdlib + httpx are used. Clash not running is a normal state, never an
exception that reaches a client request: controller failures raise
ClashUnavailable inside the plane, and the plane degrades to the direct path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

log = logging.getLogger("smssocket")
DIRECT = "direct"
MAX_BODY = 200


class ClashUnavailable(Exception):
    """Clash's external-controller is unreachable, unauthenticated or broken.

    Raised inside the net plane only - it must never cross into a request path,
    where the correct behaviour is to fall back to `direct`.
    """

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class EgressScore:
    """How good one egress path proved to be for one provider."""

    egress: str
    provider: str
    ok: bool = False
    delay_ms: float = 0.0
    fail_ratio: float = 0.0
    consecutive_fail: int = 0
    probes: int = 0
    error: str = ""
    updated: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"egress": self.egress, "provider": self.provider, "ok": self.ok,
                "delay_ms": round(self.delay_ms, 1),
                "fail_ratio": round(self.fail_ratio, 3),
                "consecutive_fail": self.consecutive_fail, "probes": self.probes,
                "error": self.error, "updated": round(self.updated, 1)}


def proxy_id(url: str) -> str:
    """Egress key for a plain proxy url (clash mixed-port, or any http proxy)."""
    return f"proxy:{url}"


def node_id(group: str, node: str) -> str:
    """Egress key for a named clash node inside a proxy-provider group.

    Traffic still leaves through the mixed port; the node is pinned first via
    PUT /proxies/{group}, which is what makes "egress = a specific node" real.
    """
    return f"node:{group}/{node}"


def split_node(egress: str) -> tuple[str, str]:
    """'node:GROUP/NODE' -> (GROUP, NODE); anything else -> ('', '')."""
    if not egress.startswith("node:"):
        return "", ""
    ref = egress[len("node:"):]
    group, _, node = ref.partition("/")
    return group, node


class ClashController:
    """Thin async client for clash's `external-controller` REST API.

    Every method is tolerant by construction: transport errors, timeouts and
    non-2xx answers become ClashUnavailable (or False from alive()), so a
    machine without clash running costs one failed connect per probe.
    """

    def __init__(self, cfg, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.base = (cfg.controller or "").rstrip("/")
        self._own = client is None
        self.http = client or httpx.AsyncClient(timeout=max(float(cfg.timeout), 0.5),
                                                trust_env=False)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.cfg.secret}"} if self.cfg.secret else {}

    async def _call(self, method: str, path: str, *, params=None,
                    json_body=None) -> object:
        if not self.base:
            raise ClashUnavailable("clash controller url not configured")
        try:
            r = await self.http.request(method, self.base + path, params=params,
                                        json=json_body, headers=self._headers(),
                                        timeout=max(float(self.cfg.timeout), 0.5))
        except httpx.RequestError as e:
            raise ClashUnavailable(f"clash unreachable at {self.base}: "
                                   f"{type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise ClashUnavailable(f"clash rejected the secret ({r.status_code})",
                                   r.status_code)
        if r.status_code >= 400:
            raise ClashUnavailable(f"clash {method} {path} -> {r.status_code} "
                                   f"{r.text[:MAX_BODY]}", r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text[:MAX_BODY]}

    async def version(self) -> dict:
        return await self._call("GET", "/version")        # type: ignore[return-value]

    async def alive(self) -> bool:
        """Cheap liveness check - never raises."""
        try:
            await self.version()
            return True
        except ClashUnavailable as e:
            log.debug("clash controller down: %s", e)
            return False

    async def proxies(self) -> dict:
        """GET /proxies -> {"proxies": {name: {type, now, hidden, all, ...}}}."""
        data = await self._call("GET", "/proxies")
        return data if isinstance(data, dict) else {}

    async def history(self, group: str) -> list:
        data = await self._call("GET", f"/proxies/{quote(group, safe='')}/history")
        return data if isinstance(data, list) else []

    async def rules(self) -> dict:
        return await self._call("GET", "/rules")          # type: ignore[return-value]

    async def connections(self) -> dict:
        return await self._call("GET", "/connections")    # type: ignore[return-value]

    async def memory(self) -> dict:
        return await self._call("GET", "/memory")         # type: ignore[return-value]

    async def delay(self, name: str, expected: int = 204,
                    timeout: float | None = None) -> float:
        """Measure one group/node through clash; returns the reported delay_ms.

        clash answers {"delay": 123}; `expected` is the status code the health
        check must produce (204 for generate_204).
        """
        params = {"url": self.cfg.health_url,
                  "timeout": int((timeout or self.cfg.timeout) * 1000)}
        if expected:
            params["expected"] = int(expected)
        data = await self._call("GET", f"/proxies/{quote(name, safe='')}/delay",
                                params=params)
        if not isinstance(data, dict) or "delay" not in data:
            raise ClashUnavailable(f"no delay reported for '{name}': "
                                   f"{str(data)[:MAX_BODY]}")
        try:
            return float(data.get("delay") or 0.0)
        except (TypeError, ValueError):
            raise ClashUnavailable(f"bad delay value for '{name}': {data!r}") from None

    async def select(self, group: str, node: str) -> dict:
        """Pin a proxy-provider group onto one node (PUT /proxies/{group})."""
        await self._call("PUT", f"/proxies/{quote(group, safe='')}",
                         json_body={"name": node})
        return {"group": group, "node": node, "ok": True}

    async def flush_dns(self) -> dict:
        """Drop clash's DNS cache. clash builds vary, so try both verbs."""
        for method in ("PUT", "POST"):
            try:
                await self._call(method, "/dns/flush")
                return {"flushed": True, "method": method}
            except ClashUnavailable as e:
                if e.status not in (404, 405):
                    raise
        raise ClashUnavailable("clash has no /dns/flush endpoint")

    async def close(self) -> None:
        if self._own:
            await self.http.aclose()


class EgressRegistry:
    """One httpx.AsyncClient per egress path, built lazily and reused.

    `direct` is *not* a new client - it is the gateway's existing shared
    `st.http`, so every current test and behaviour stays untouched. Proxy paths
    get their own client because a proxy is a per-client transport, not a
    per-request option.
    """

    def __init__(self, shared: httpx.AsyncClient, cfg,
                 client_factory=None) -> None:
        self.cfg = cfg
        self.shared = shared
        self._factory = client_factory
        self._urls: dict[str, str] = {DIRECT: ""}
        self._clients: dict[str, httpx.AsyncClient] = {DIRECT: shared}
        self._stale: list[httpx.AsyncClient] = []

    def add(self, egress: str, proxy_url: str = "") -> str:
        """Register a path id; returns it. Re-registering with a new url drops
        the cached client so a changed clash port takes effect on next use."""
        if egress in self._urls and self._urls[egress] == (proxy_url or ""):
            return egress
        old = self._clients.pop(egress, None)
        if old is not None and old is not self.shared:
            self._forget(old)
        self._urls[egress] = proxy_url or ""
        return egress

    def paths(self) -> list[str]:
        return list(self._urls)

    def url_of(self, egress: str) -> str:
        return self._urls.get(egress, "")

    def client_for(self, egress: str) -> httpx.AsyncClient:
        if egress in self._clients:
            return self._clients[egress]
        proxy = self._urls.get(egress)
        if proxy is None:                      # unknown path -> direct, never 500
            log.warning("unknown egress '%s', using direct", egress)
            return self.shared
        client = self._build(proxy)
        self._clients[egress] = client
        return client

    def _build(self, proxy: str) -> httpx.AsyncClient:
        if self._factory is not None:
            return self._factory(proxy)
        return httpx.AsyncClient(proxy=proxy or None, follow_redirects=True,
                                 trust_env=False,
                                 limits=httpx.Limits(max_connections=32))

    def _forget(self, client: httpx.AsyncClient) -> None:
        """Park a superseded client; aclose() reaps it (sync add() can't await)."""
        self._stale.append(client)

    async def aclose(self) -> None:
        """Close every proxy client; the shared one belongs to the gateway."""
        doomed = [c for c in self._clients.values() if c is not self.shared]
        doomed += self._stale
        for client in doomed:
            try:
                await client.aclose()
            except Exception as e:                    # shutdown best-effort
                log.debug("egress client close failed: %s", e)
        self._stale = []
        self._clients = {DIRECT: self.shared}
        self._urls = {DIRECT: ""}

    def info(self) -> list[dict]:
        return [{"egress": k, "proxy": v or None, "shared": k == DIRECT}
                for k, v in self._urls.items()]


class NetProbe:
    """Measures how long a provider takes to answer over a given egress path."""

    def __init__(self, cfg, controller: ClashController | None,
                 registry: EgressRegistry) -> None:
        self.cfg, self.controller, self.registry = cfg, controller, registry

    async def measure(self, egress: str, url: str,
                      timeout: float | None = None) -> tuple[bool, float, str]:
        """-> (ok, delay_ms, error). Never raises."""
        to = float(timeout if timeout is not None else self.cfg.timeout)
        group, node = split_node(egress)
        if node and self.controller is not None:
            try:
                return True, await self.controller.delay(node, timeout=to), ""
            except ClashUnavailable as e:
                return False, 0.0, str(e)[:MAX_BODY]
        client = self.registry.client_for(egress)
        t0 = time.perf_counter()
        try:
            r = await client.head(url, timeout=to,
                                  headers={"user-agent": "SMSocket/net-probe"})
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code in (400, 401, 403, 404, 405, 501):   # HEAD refused -> GET
                r = await client.get(url, timeout=to, headers={
                    "user-agent": "SMSocket/net-probe"})
                ms = (time.perf_counter() - t0) * 1000
            # Any HTTP answer means the path carries traffic: a 404 is a missing
            # route, a 5xx is the provider's day, and neither is a network fault.
            # Only a proxy that demands auth (407) is a path we cannot use.
            ok = r.status_code != 407
            return ok, ms, "" if ok else "HTTP 407 (proxy auth required)"
        except httpx.RequestError as e:
            return False, (time.perf_counter() - t0) * 1000, \
                f"{type(e).__name__}: {e}"[:MAX_BODY]
        except Exception as e:                          # noqa: BLE001 - probe
            return False, (time.perf_counter() - t0) * 1000, \
                f"{type(e).__name__}: {e}"[:MAX_BODY]


class NetPlane:
    """The live network picture: paths, scores, picks, background refresher.

    `pick()` is the only thing the request path touches, and it is synchronous
    and cheap - it reads the last measured scores, never the network. A plane
    with no scores yet (or a dead controller) always answers `direct`, so
    enabling clash can never make a call fail that would have succeeded.
    """

    def __init__(self, cfg, registry: EgressRegistry,
                 controller: ClashController | None = None,
                 probe: NetProbe | None = None) -> None:
        self.cfg = cfg
        self.registry = registry
        self.controller = controller
        self.probe = probe or NetProbe(cfg, controller, registry)
        self.scores: dict[tuple[str, str], EgressScore] = {}
        self.mode = cfg.mode or "auto"
        self.last_refresh: float = 0.0
        self.last_error: str = ""
        self.alive: bool | None = None
        self.refreshing: bool = False
        self._task: asyncio.Task | None = None
        self._targets: list[tuple[str, str]] = []      # provider -> base_url
        self.seed_paths()

    @property
    def smart(self) -> bool:
        """Scoring on - `enabled` alone only registers paths, never reranks."""
        return bool(self.cfg.enabled and self.cfg.smart)

    # ---- paths -----------------------------------------------------------
    def seed_paths(self) -> None:
        """Register direct + the configured proxy + every tracked group/node."""
        self.registry.add(DIRECT, "")
        url = self.cfg.proxy_url_for()
        if url:
            self.registry.add(proxy_id(url), url)
        for group, ref in (self.cfg.provider_proxy or {}).items():
            node = str(ref or "").strip()
            if not node:
                continue
            if "/" in node:
                g, _, n = node.partition("/")
                self.registry.add(node_id(g, n), url)
            else:
                self.registry.add(node_id(group, node), url)
        for group in (self.cfg.groups or []):
            if group:
                self.registry.add(node_id(str(group), ""), url)

    async def discover_paths(self) -> list[str]:
        """Add one path per node of every tracked clash group (best effort)."""
        if self.controller is None:
            return []
        added: list[str] = []
        try:
            data = await self.controller.proxies()
        except ClashUnavailable as e:
            self.last_error = str(e)[:MAX_BODY]
            self.alive = False
            return []
        self.alive = True
        groups = {g for g in (self.cfg.groups or []) if g}
        groups |= {str(v) for v in (self.cfg.provider_proxy or {}).values() if v}
        for name, info in (data.get("proxies") or {}).items():
            if not isinstance(info, dict):
                continue
            if info.get("hidden") or info.get("type") == "Direct":
                continue
            want = name in groups or any(str(n) in groups
                                         for n in (info.get("all") or []))
            url = self.cfg.proxy_url_for()
            if not want or not url:
                continue
            for node in (info.get("all") or []):
                node = str(node or "")
                if not node:
                    continue
                eid = node_id(name, node)
                self.registry.add(eid, url)
                added.append(eid)
        return added

    # ---- scoring ---------------------------------------------------------
    def set_targets(self, providers) -> None:
        self._targets = [(p.name, p.base_url) for p in providers if p.enabled]

    def _record(self, provider: str, egress: str, ok: bool, ms: float,
                error: str = "") -> EgressScore:
        key = (provider, egress)
        sc = self.scores.get(key) or EgressScore(egress=egress, provider=provider)
        sc.updated = time.time()
        sc.probes += 1
        sc.ok = ok
        sc.delay_ms = ms if ok else max(ms, sc.delay_ms)
        sc.consecutive_fail = 0 if ok else sc.consecutive_fail + 1
        fails = sc.consecutive_fail if not ok else 0
        sc.fail_ratio = round(min(1.0, fails / max(sc.probes, 1)), 3)
        # Sustained failure over the configured ratio makes a path unhealthy
        # even when the newest probe looked fine; one success clears it again,
        # so a recovered proxy can serve traffic without waiting for a reprobe.
        if sc.fail_ratio > float(self.cfg.fail_ratio or 1.0):
            sc.ok = False
        if not ok and error:
            sc.error = error[:MAX_BODY]
        self.scores[key] = sc
        return sc

    async def probe_provider(self, provider: str, base_url: str) -> list[EgressScore]:
        """Measure every registered path against one provider's base url."""
        out: list[EgressScore] = []
        for egress in self.registry.paths():
            ok, ms, err = await self.probe.measure(egress, base_url)
            out.append(self._record(provider, egress, ok, ms, err))
        return out

    async def refresh(self, providers=None) -> dict:
        """Re-probe all (provider x path) pairs; safe to call concurrently."""
        if providers is not None:
            self.set_targets(providers)
        self.refreshing = True
        n = 0
        try:
            if self.controller is not None:
                self.alive = await self.controller.alive()
                if self.alive:
                    await self.discover_paths()
            for name, base in list(self._targets):
                for _ in await self.probe_provider(name, base):
                    n += 1
            self.last_refresh = time.time()
            self.last_error = "" if self.alive is not False else self.last_error
        finally:
            self.refreshing = False
        return {"probes": n, "paths": len(self.registry.paths()),
                "alive": self.alive, "at": round(self.last_refresh, 1)}

    def ranked(self, provider: str) -> list[dict]:
        """Paths for one provider, best first (ok, then delay, then health)."""
        rows = [s for (p, _e), s in self.scores.items() if p == provider]
        rows.sort(key=lambda s: (not s.ok, s.delay_ms or 1e12, s.fail_ratio))
        return [s.as_dict() for s in rows]

    def best_egress(self, provider: str) -> list[dict]:
        """Spec name for ranked(): every path for a provider, best first."""
        return self.ranked(provider)

    def pick(self, provider: str) -> str:
        """Best egress id for a provider - `direct` unless something beat it."""
        if not self.cfg.enabled or not self.cfg.smart or self.mode == "direct":
            return DIRECT
        rows = self.ranked(provider)
        if not rows:
            return DIRECT
        best = rows[0]
        if not best["ok"]:
            return DIRECT
        if self.mode == "proxy":           # operator forced the proxy plane
            if proxy_id(self.cfg.proxy_url_for()) in [r["egress"] for r in rows]:
                forced = [r for r in rows if r["egress"] != DIRECT and r["ok"]]
                return forced[0]["egress"] if forced else DIRECT
            return best["egress"] if best["ok"] else DIRECT
        direct = next((r for r in rows if r["egress"] == DIRECT), None)
        if direct and direct["ok"]:
            dms, bms = direct["delay_ms"], best["delay_ms"]
            floor = float(self.cfg.min_delay_ms or 0)
            if bms + max(floor, 1.0) >= dms or best["egress"] == DIRECT:
                return DIRECT
            return best["egress"]
        return best["egress"]

    def client_for(self, provider: str):
        return self.registry.client_for(self.pick(provider))

    def sort_key(self, provider: str) -> tuple:
        """Stable re-rank key for Pool.candidates (unknown stays neutral)."""
        rows = self.ranked(provider)
        if not rows:
            return (0, 0.0, 0.0)
        best = rows[0]
        if not best["ok"]:
            return (1, 1e12, 1.0)
        return (0, float(best["delay_ms"]), float(best["fail_ratio"]))

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Background refresher (only when enabled + interval sane)."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop(), name="sms-net-plane")

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:                        # noqa: BLE001
                self.last_error = str(e)[:MAX_BODY]
                log.warning("net plane refresh failed: %s", e)
            await asyncio.sleep(max(float(self.cfg.interval or 60), 5.0))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
            self._task = None
        if self.controller is not None:
            await self.controller.close()
        await self.registry.aclose()

    def state(self) -> dict:
        return {"enabled": bool(self.cfg.enabled), "mode": self.mode,
                "smart": bool(self.cfg.smart), "controller": self.cfg.controller,
                "alive": self.alive, "paths": self.registry.paths(),
                "proxy": self.cfg.proxy_url_for(),
                "interval": self.cfg.interval, "groups": list(self.cfg.groups or []),
                "scores": len(self.scores),
                "last_refresh": round(self.last_refresh, 1),
                "refreshing": self.refreshing, "error": self.last_error}
