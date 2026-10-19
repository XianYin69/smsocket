"""Stack scheduler tests: parking, pop order, waking, 429 shape, /stack routes.

All offline. The gate is driven directly (no sockets); the gateway part uses the
same MockTransport trick as test_smoke.py. Note `StackScheduler.start()` creates
a task, so it must happen *inside* a running loop - hence `with_stack()`.
"""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket.batch import BatchRunner
from SMSocket.config import ProviderSpec, Settings, StackConfig
from SMSocket.concurrency import Gate, Meter, SMSocketBusy
from SMSocket.gateway import create_app
from SMSocket.providers import Pool
from SMSocket.stacksched import Parking, Stack, StackScheduler


def with_stack(max_conc=1, queue_wait=0.0, per_provider=0, **stack_kw):
    """Gate + scheduler, both started inside the caller's event loop."""
    base = dict(enabled=True, wait=5.0, interval=0.02)
    base.update(stack_kw)
    holder = {}

    async def go(fn):
        meter = Meter()
        gate = Gate(max_conc, queue_wait, per_provider, meter)
        sched = StackScheduler(StackConfig(**base), gate, holder.get("pool"))
        gate.attach_stack(sched)
        sched.start()
        try:
            return await fn(gate, sched, meter)
        finally:
            await sched.stop()

    return go


# ---------------------------------------------------------------- plain ----
def test_disabled_stack_keeps_the_old_429_behaviour():
    gate = Gate(1, 0.0, 0, Meter())

    async def body():
        a = await gate.acquire("m", "p")
        with pytest.raises(SMSocketBusy) as e:
            await gate.acquire("m", "p")
        a.release()
        return e.value

    busy = asyncio.run(body())
    assert busy.parked_ms == 0.0 and busy.reason == "saturated"
    assert gate.meter.rejected == 1
    assert "stack" not in gate.limits()          # disabled payload unchanged


# ---------------------------------------------------------------- park -----
def test_saturated_caller_is_parked_then_woken_by_a_release():
    async def body(gate, sched, meter):
        a = await gate.acquire("m", "p")                  # takes the only slot
        task = asyncio.create_task(gate.acquire("m", "p"))
        await asyncio.sleep(0.12)
        assert len(sched.stack) == 1 and meter.rejected == 0
        a.release()                                       # frees capacity
        b = await task
        assert b.parked_ms > 0 and meter.active == 1      # one slot, not two
        b.release()
        return sched.stack.snapshot(entries=0), meter.active, meter.rejected

    snap, active, rejected = asyncio.run(with_stack()(body))
    assert snap["pushed"] == 1 and snap["popped"] == 1 and snap["expired"] == 0
    assert active == 0 and rejected == 0


def test_park_timeout_still_returns_429_with_parked_ms():
    async def body(gate, sched, meter):
        held = await gate.acquire("m", "p")
        with pytest.raises(SMSocketBusy) as e:
            await gate.acquire("m", "p")
        held.release()
        return e.value, sched.stack.snapshot(entries=0)

    busy, snap = asyncio.run(with_stack(wait=0.05)(body))
    assert busy.parked_ms >= 20 and "parked" in str(busy)
    assert busy.reason == "saturated" and snap["expired"] == 1
    assert snap["depth"] == 0 and snap["pushed"] == 1


def test_park_budget_never_exceeds_queue_wait():
    """A client that refuses to queue past queue_wait must not be parked longer."""
    async def body(gate, sched, meter):
        held = await gate.acquire("m", "p")
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(SMSocketBusy):
            await gate.acquire("m", "p")
        held.release()
        return asyncio.get_running_loop().time() - t0

    waited = asyncio.run(with_stack(queue_wait=0.05, wait=30.0)(body))
    assert 0.04 <= waited < 0.5, waited


# ------------------------------------------------------------- pop order ---
def test_stack_pop_order_follows_policy():
    def entry(tag, prio=0, ts=0.0):
        return Parking(ts=ts, clock=0.0, alias=tag, provider="", prio=prio,
                       event=asyncio.Event(), reason="saturated")

    st = Stack("lifo")
    for i, tag in enumerate("abc"):
        st.push(entry(tag, ts=float(i)))
    assert [e.alias for e in st.candidates()] == ["c", "b", "a"]
    assert st.pop().alias == "c"

    st = Stack("fifo")
    for i, tag in enumerate("abc"):
        st.push(entry(tag, ts=float(i)))
    assert [e.alias for e in st.candidates()] == ["a", "b", "c"]
    assert st.pop().alias == "a"

    st = Stack("priority")
    for prio, tag in ((1, "low"), (5, "high"), (3, "mid")):
        st.push(entry(tag, prio=prio, ts=1.0))
    assert [e.alias for e in st.candidates()] == ["high", "mid", "low"]
    assert st.pop().alias == "high"


def test_lifo_wakes_the_newest_parked_caller_first():
    async def body(gate, sched, meter):
        held = await gate.acquire("m", "p")
        got = []

        async def park(tag):
            lease = await gate.acquire(tag, "p")
            got.append(tag)
            lease.release()                       # frees the slot for the next

        tasks = [asyncio.create_task(park(f"c{i}")) for i in range(3)]
        await asyncio.sleep(0.1)
        order = [e.alias for e in sched.stack.candidates()]
        held.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 5.0)
        return order, got

    order, got = asyncio.run(with_stack(policy="lifo")(body))
    assert order == ["c2", "c1", "c0"] and got[0] == "c2"


def test_fifo_wakes_the_oldest_parked_caller_first():
    async def body(gate, sched, meter):
        held = await gate.acquire("m", "p")
        got = []

        async def park(tag):
            lease = await gate.acquire(tag, "p")
            got.append(tag)
            lease.release()

        tasks = [asyncio.create_task(park(f"c{i}")) for i in range(3)]
        await asyncio.sleep(0.1)
        held.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 5.0)
        return got

    assert asyncio.run(with_stack(policy="fifo")(body)) == ["c0", "c1", "c2"]


# ------------------------------------------------------------------ rpm ----
def test_rpm_limit_parks_the_caller_instead_of_refusing_it():
    prov = ProviderSpec(name="p", base_url="http://api.test/v1", keys=["k1"],
                        models={"m": "m"}, max_rpm=1)
    pool = Pool([prov])
    slot = pool.slots[0]

    async def body(gate, sched, meter):
        first = await gate.acquire("m", "p")
        slot.touch()                                      # rpm window now full
        assert sched.rpm_blocked("p") is True
        task = asyncio.create_task(gate.acquire("m", "p"))
        await asyncio.sleep(0.1)
        assert len(sched.stack) == 1 and sched.stack.items[0].reason == "rpm"
        slot._window = []                                 # minute rolls over
        second = await task
        assert second.parked_ms > 0
        first.release()
        second.release()
        return sched.stack.snapshot(entries=0), meter.rejected

    sched_stack = with_stack(max_conc=0, per_provider=0)
    # pool must reach the scheduler: rebuild with it explicitly
    async def body2(gate, sched, meter):
        sched.pool = pool
        return await body(gate, sched, meter)

    snap, rejected = asyncio.run(sched_stack(body2))
    assert snap["pushed"] == 1 and snap["popped"] == 1 and rejected == 0


def test_rpm_headroom_reads_the_key_window():
    prov = ProviderSpec(name="p", base_url="http://x/v1", keys=["k"],
                        models={"m": "m"}, max_rpm=3)
    pool = Pool([prov])

    async def body(gate, sched, meter):
        sched.pool = pool
        assert sched.rpm_headroom("p") == 3
        for _ in range(3):
            pool.slots[0].touch()
        assert sched.rpm_headroom("p") == 0 and sched.rpm_blocked("p") is True
        assert sched.rpm_headroom("other") is None        # uncapped provider
        return True

    assert asyncio.run(with_stack(max_conc=0)(body))


# --------------------------------------------------------------- cooldown --
def test_provider_cooling_flags_all_keys_in_cooldown():
    prov = ProviderSpec(name="p", base_url="http://x/v1", keys=["k1", "k2"],
                        models={"m": "m"})
    pool = Pool([prov])

    async def body(gate, sched, meter):
        sched.pool = pool
        assert sched.provider_cooling("p") is False
        for s in pool.slots:
            s.note_fail(cooldown=999.0)                   # both keys cooling
        assert sched.provider_cooling("p") is True
        assert sched.provider_cooling("other") is False   # unknown provider
        pool.slots[0].dead_until = {}                   # one key recovers
        assert sched.provider_cooling("p") is False        # not all cooling
        return True

    assert asyncio.run(with_stack(max_conc=0)(body))


def test_wake_once_holds_a_cooling_provider_parked():
    """A caller parked for `cooldown` is not woken while the provider is cold,
    and goes through as soon as the last key leaves its cooldown window."""
    prov = ProviderSpec(name="p", base_url="http://x/v1", keys=["k"],
                        models={"m": "m"})
    pool = Pool([prov])

    async def body(gate, sched, meter):
        sched.pool = pool
        pool.slots[0].note_fail(cooldown=999.0)           # provider is cold
        assert sched.provider_cooling("p") is True
        # Park without waiting: push the entry, then probe wake_once directly.
        entry = Parking(ts=time.time(), clock=time.perf_counter(), alias="m",
                        provider="p", prio=0, event=asyncio.Event(),
                        reason="cooldown", grab=False)
        assert sched.stack.push(entry)
        woke = await sched.wake_once()                    # cold: nobody may go
        assert woke == 0 and len(sched.stack) == 1
        pool.slots[0].dead_until = {}                    # cooldown expires
        woke = await sched.wake_once()
        assert woke == 1 and entry.granted is True        # woken, no grab held
        return True

    assert asyncio.run(with_stack(max_conc=0, wait=5.0)(body))


# ----------------------------------------------------------------- grab ----
def test_take_slot_refuses_when_capacity_is_gone():
    async def body(gate, sched, meter):
        a = await gate.acquire("m", "p")
        b = await gate.acquire("m", "p")
        assert await sched.take_slot("p") is False        # both slots busy
        a.release()
        assert await sched.take_slot("p") is True         # one freed
        assert gate.grabbed == 1
        sched.release_slot("p")
        assert gate.grabbed == 0
        b.release()
        return meter.active

    assert asyncio.run(with_stack(max_conc=2)(body)) == 0


def test_drop_at_releases_a_parked_caller_without_a_grant():
    async def body(gate, sched, meter):
        held = await gate.acquire("m", "p")
        task = asyncio.create_task(gate.acquire("m", "p"))
        await asyncio.sleep(0.1)
        out = sched.drop_at(0)
        with pytest.raises(SMSocketBusy):
            await task
        held.release()
        return out, meter.active, meter.rejected

    out, active, rejected = asyncio.run(with_stack()(body))
    assert out["dropped"] is True and out["alias"] == "m"
    assert active == 0 and rejected == 1


def test_stack_full_keeps_the_429():
    async def body(gate, sched, meter):
        sched.stack.max_depth = 0
        held = await gate.acquire("m", "p")
        with pytest.raises(SMSocketBusy) as e:
            await gate.acquire("m", "p")
        held.release()
        return e.value

    busy = asyncio.run(with_stack()(body))
    assert busy.reason == "stack_full" and "parked" in str(busy)


# ---------------------------------------------------------------- batch ----
class FlakyRouter:
    """429 on the first attempt, then answers - exercises batch re-parking."""

    class s:
        billing = None

        @staticmethod
        def cost_detail(alias, prompt, completion):
            return {"amount": 0.0, "currency": "USD", "display": 0.0,
                    "display_currency": "USD", "rate": 1.0}

    def __init__(self, gate):
        self.gate = gate
        self.tried = 0

    async def complete(self, item):
        self.tried += 1
        if self.tried == 1:
            raise SMSocketBusy("gateway saturated (1 in flight)", 0.5)
        return {"id": "x", "model": item["model"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}


def test_batch_reparks_a_busy_item():
    async def body(gate, sched, meter):
        router = FlakyRouter(gate)
        held = await gate.acquire("m", "p")               # keep it busy
        task = asyncio.create_task(
            BatchRunner().run_items(router, [{"model": "m"}], concurrency=1))
        await asyncio.sleep(0.1)
        held.release()                                    # drain wakes the batch
        res = await task
        return res, router.tried, sched.stack.snapshot(entries=0)

    res, tried, snap = asyncio.run(with_stack(repark=2)(body))
    assert res[0]["status"] == 200 and tried == 2
    assert res[0]["reparks"] == 1 and snap["pushed"] >= 1


def test_batch_repark_gives_up_after_the_budget():
    async def body(gate, sched, meter):
        router = FlakyRouter(gate)

        async def always_busy(item):
            raise SMSocketBusy("still saturated", 0.5)

        router.complete = always_busy
        held = await gate.acquire("m", "p")
        res = await asyncio.wait_for(
            BatchRunner().run_items(router, [{"model": "m"}], concurrency=1), 10.0)
        held.release()
        return res

    res = asyncio.run(with_stack(repark=1, wait=0.05)(body))
    assert res[0]["status"] == 429 and res[0]["reparks"] == 1


def test_batch_without_a_stack_reports_429_as_before():
    gate = Gate(1, 0.0, 0, Meter())

    async def body():
        router = FlakyRouter(gate)
        held = await gate.acquire("m", "p")
        res = await BatchRunner().run_items(router, [{"model": "m"}], concurrency=1)
        held.release()
        return res

    res = asyncio.run(body())
    assert res[0]["status"] == 429 and res[0]["reparks"] == 0


# --------------------------------------------------------------- routes ----
def _handler(request):
    return httpx.Response(200, json={
        "id": "c", "object": "chat.completion", "model": "demo",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def _settings(tmp_path, stack=None):
    return Settings(master_keys=["sk-test"], db_path=str(tmp_path / "u.sqlite3"),
                    stack=stack or StackConfig(), providers=[
                        ProviderSpec(name="openai", base_url="http://api.test/v1",
                                     keys=["sk-up"], models={"demo": "demo"})])


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(_handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    with TestClient(create_app(_settings(tmp_path))) as c:
        yield c


H = {"Authorization": "Bearer sk-test"}


def test_stack_routes_report_disabled_state(app_client):
    body = app_client.get("/stack").json()
    assert body["enabled"] is False and body["depth"] == 0
    view = app_client.get("/concurrency").json()["stack"]
    assert view["enabled"] is False and view["depth"] == 0
    assert app_client.post("/stack/drain", headers=H, json={"n": 1}).status_code == 409
    assert app_client.delete("/stack/0", headers=H).status_code == 409
    assert app_client.post("/stack/drain", json={"n": 1}).status_code == 401


def test_stack_routes_show_counters_when_enabled(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(_handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = StackConfig(enabled=True, wait=1.0, policy="fifo", repark=2)
    with TestClient(create_app(_settings(tmp_path, st))) as c:
        body = c.get("/stack").json()
        assert body["enabled"] is True and body["policy"] == "fifo"
        assert body["reparks"] == 2 and body["draining"] is True
        live = c.get("/concurrency").json()["stack"]
        assert live["enabled"] is True and live["policy"] == "fifo"
        assert c.post("/stack/drain", headers=H, json={"n": 3}).json()["woken"] == 0
        assert c.delete("/stack/0", headers=H).status_code == 404
        r = c.post("/v1/chat/completions", headers=H, json={
            "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        after = c.get("/stack").json()
        assert after["depth"] == 0 and after["pushed"] == 0
