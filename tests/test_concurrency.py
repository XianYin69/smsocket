"""Concurrency tests: in-flight meter, admission gate, saturation -> 429.

No pytest-asyncio in this project, so async cases are driven with asyncio.run.
Upstreams are faked with an async httpx.MockTransport that sleeps, so several
requests are genuinely in flight at once and peak concurrency is measurable.
"""
import asyncio
import time

import httpx
import pytest

from SMSocket.concurrency import Gate, Meter, SMSocketBusy
from SMSocket.config import ProviderSpec, Settings, load_config
from SMSocket.gateway import create_app
from SMSocket.router import Router  # noqa: F401 - hand-wired in make_app

H = {"Authorization": "Bearer sk-test"}
PAYLOAD = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}


def make_settings(tmp_path, **kw) -> Settings:
    return Settings(listen="127.0.0.1:8000", master_keys=["sk-test"], strategy="priority", cooldown=1, db_path=str(tmp_path / "usage.sqlite3"),
                    pricing={"demo": {"prompt": 1.0, "completion": 1.0}},
                    providers=[ProviderSpec(name="slow", base_url="http://mock/v1",
                                            keys=["k1"], models={"demo": "demo-x"},
                                            priority=10)],
                    **kw)


def ok_handler(delay=0.03, live=None, release=None):
    """Async mock upstream: sleeps so requests overlap, can track its own peak."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if live is not None:
            live["live"] += 1
            live["peak"] = max(live["peak"], live["live"])
        if release is not None:
            await release.wait()
        else:
            await asyncio.sleep(delay)
        if live is not None:
            live["live"] -= 1
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "model": "demo-x",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})
    return handler


def make_app(tmp_path, handler, **kw):
    """Build the app and wire router state by hand (what the lifespan would do)."""
    settings = make_settings(tmp_path, **kw)
    app = create_app(settings)
    st = app.state.llm
    st.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
    return app, st


def fire(app, n, payload=PAYLOAD, url="/v1/chat/completions", headers=H):
    """Send n requests through the ASGI app at the same time."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as c:
            return await asyncio.gather(*[c.post(url, headers=headers, json=payload)
                                          for _ in range(n)], return_exceptions=True)
    return asyncio.run(go())


def codes(rs):
    return [getattr(r, "status_code", repr(r)) for r in rs]


# ---- meter ----------------------------------------------------------------
def test_meter_counts_active_peak_and_errors():
    m = Meter()
    m.enter("p1"); m.enter("p1"); m.enter("p2")
    assert m.active == 3 and m.peak == 3
    m.exit("p1", 10.0)
    m.exit("p1", 20.0, error=True)
    assert m.active == 1 and m.errors == 1
    assert m.by_provider["p1"]["active"] == 0 and m.by_provider["p2"]["active"] == 1
    snap = m.snapshot({"max_concurrency": 5})
    assert snap["total"] == 3 and snap["max_ms"] == 20.0 and snap["max_concurrency"] == 5


def test_meter_never_goes_negative_on_double_release():
    m = Meter()
    m.enter("p"); m.exit("p", 1.0); m.exit("p", 1.0)
    assert m.active == 0


# ---- gate -----------------------------------------------------------------
def test_gate_serialises_to_max_concurrency():
    async def go():
        gate = Gate(max_concurrency=2, queue_wait=5.0)
        live = {"live": 0, "peak": 0}

        async def worker():
            lease = await gate.acquire("demo", "p")
            live["live"] += 1
            live["peak"] = max(live["peak"], live["live"])
            await asyncio.sleep(0.02)
            live["live"] -= 1
            lease.release()

        await asyncio.gather(*[worker() for _ in range(10)])
        return gate, live

    gate, live = asyncio.run(go())
    assert live["peak"] == 2, live
    assert gate.meter.active == 0 and gate.meter.total == 10 and gate.meter.peak == 2


def test_gate_rejects_immediately_when_queue_wait_zero():
    async def go():
        gate = Gate(max_concurrency=1, queue_wait=0.0)
        held = await gate.acquire("demo", "p")
        with pytest.raises(SMSocketBusy):
            await gate.acquire("demo", "p")
        assert gate.meter.rejected == 1 and gate.meter.queued == 0
        held.release()
        (await gate.acquire("demo", "p")).release()
        return gate

    gate = asyncio.run(go())
    assert gate.meter.active == 0


def test_gate_times_out_waiting_and_counts_rejections():
    async def go():
        gate = Gate(max_concurrency=1, queue_wait=0.05)
        held = await gate.acquire("demo", "p")
        t0 = time.perf_counter()
        with pytest.raises(SMSocketBusy) as ei:
            await gate.acquire("demo", "p")
        waited = time.perf_counter() - t0
        held.release()
        return waited, ei.value, gate

    waited, err, gate = asyncio.run(go())
    assert 0.04 < waited < 1.0, waited
    assert err.retry_after > 0 and "queue wait exceeded" in str(err)
    assert gate.meter.rejected == 1 and gate.meter.queued == 0


def test_per_provider_cap_is_independent_of_global():
    async def go():
        gate = Gate(max_concurrency=10, queue_wait=1.0, per_provider=1)
        a = await gate.acquire("demo", "A")
        b = await gate.acquire("demo", "B")            # other provider -> allowed
        with pytest.raises(SMSocketBusy):
            await gate.acquire("demo", "A")
        a.release(); b.release()
        assert gate.meter.active == 0
        (await gate.acquire("demo", "A")).release()

    asyncio.run(go())


def test_double_release_does_not_leak_or_lose_slots():
    async def go():
        gate = Gate(max_concurrency=1, queue_wait=0.05)
        lease = await gate.acquire("demo", "A")
        lease.release()
        lease.release()                                  # idempotent
        assert gate.meter.active == 0
        again = await gate.acquire("demo", "A")          # slot really is free
        with pytest.raises(SMSocketBusy):                # and really is single
            await gate.acquire("demo", "A")
        again.release()

    asyncio.run(go())


# ---- gateway integration --------------------------------------------------
def test_concurrency_endpoint_reports_counters(tmp_path):
    app, st = make_app(tmp_path, ok_handler(), max_concurrency=4, queue_wait=5.0)
    assert st.gate.max_concurrency == 4
    rs = fire(app, 1)
    assert codes(rs) == [200]
    j = st.meter.snapshot(st.gate.limits())
    assert j["total"] == 1 and j["active"] == 0 and j["peak"] == 1
    assert j["by_provider"]["slow"]["total"] == 1
    assert j["max_concurrency"] == 4 and j["saturated"] is False


def test_parallel_load_respects_the_gate(tmp_path):
    """8 simultaneous clients behind a gate of 2 -> upstream never sees more."""
    live = {"live": 0, "peak": 0}
    app, st = make_app(tmp_path, ok_handler(0.04, live), max_concurrency=2, queue_wait=10.0)
    rs = fire(app, 8)
    assert codes(rs) == [200] * 8, codes(rs)
    assert live["peak"] <= 2, live                       # measured at the upstream
    snap = st.meter.snapshot(st.gate.limits())
    assert snap["total"] == 8 and snap["active"] == 0 and snap["peak"] <= 2, snap
    assert snap["avg_wait_ms"] > 0                       # the rest queued for a slot
    assert len(st.usage.recent(20)) == 8                 # every call accounted


def test_unlimited_by_default(tmp_path):
    """max_concurrency=0 -> no gate, meter still counts."""
    live = {"live": 0, "peak": 0}
    app, st = make_app(tmp_path, ok_handler(0.03, live))
    rs = fire(app, 6)
    assert codes(rs) == [200] * 6
    assert live["peak"] > 1, live                        # genuinely parallel
    assert st.meter.snapshot(st.gate.limits())["max_concurrency"] is None


def test_saturated_gateway_returns_429_with_retry_after(tmp_path):
    release = asyncio.Event()
    app, st = make_app(tmp_path, ok_handler(release=release),
                       max_concurrency=1, queue_wait=0.1)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as c:
            first = asyncio.create_task(c.post("/v1/chat/completions", headers=H, json=PAYLOAD))
            await asyncio.sleep(0.05)                    # first holds the only slot
            rest = await asyncio.gather(*[c.post("/v1/chat/completions", headers=H,
                                                 json=PAYLOAD) for _ in range(3)],
                                        return_exceptions=True)
            release.set()
            done = await first
        return done, rest

    done, rest = asyncio.run(go())
    assert done.status_code == 200
    assert codes(rest) == [429, 429, 429], codes(rest)
    assert rest[0].json()["error"]["code"] == "gateway_saturated"
    assert rest[0].headers.get("retry-after")
    assert st.meter.rejected == 3 and st.meter.active == 0


def test_gate_limits_come_from_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "listen: 127.0.0.1:8011\nmax_concurrency: 7\nqueue_wait: 2.5\n"
        "per_provider_concurrency: 3\ndb_path: db.sqlite3\nproviders:\n"
        "  - name: A\n    base_url: https://a.example/v1\n    keys: [sk-a]\n"
        "    models: {m: m}\n", encoding="utf-8")
    s = load_config(cfg)
    assert (s.max_concurrency, s.queue_wait, s.per_provider_concurrency) == (7, 2.5, 3)
    app = create_app(s)
    lim = app.state.llm.gate.limits()
    assert lim["max_concurrency"] == 7 and lim["per_provider_concurrency"] == 3
