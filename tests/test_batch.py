"""Batch tests: parallel sending, order, fail_fast, async jobs, gate interaction.

Everything that involves a background job runs inside ONE event loop (the job's
asyncio.Task cannot survive across loops), so post/poll/cancel share `session()`.
"""
import asyncio
import json as _json

import httpx
import pytest

from SMSocket.batch import BatchRunner, BatchTooLarge
from SMSocket.config import ProviderSpec, Settings
from SMSocket.gateway import create_app
from SMSocket.router import Router

H = {"Authorization": "Bearer sk-test"}


def make_settings(tmp_path, **kw):
    kw.setdefault("cooldown", 0.0)          # intentional failures must not park the key
    return Settings(listen="127.0.0.1:8000", master_keys=["sk-test"], strategy="priority", db_path=str(tmp_path / "usage.sqlite3"),
                    pricing={"demo": {"prompt": 2.0, "completion": 8.0, "currency": "USD"}},
                    providers=[ProviderSpec(name="slow", base_url="http://mock/v1",
                                            keys=["k1"],
                                            models={"demo": "demo-x", "flaky": "flaky"},
                                            priority=10)], **kw)


def handler(delay=0.02, live=None, fail_on=None):
    async def h(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        if live is not None:
            live["live"] += 1
            live["peak"] = max(live["peak"], live["live"])
        await asyncio.sleep(delay)
        if live is not None:
            live["live"] -= 1
        model = str(body.get("model") or "")
        if fail_on and model in fail_on:
            return httpx.Response(400, json={"error": {"message": "bad model"}})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000}})
    return h


def make_app(tmp_path, h, **kw):
    settings = make_settings(tmp_path, **kw)
    app = create_app(settings)
    st = app.state.llm
    st.http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
    return app, st


def session(app, script):
    """Run `script(c)` against the app inside one event loop."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as c:
            return await script(c)
    return asyncio.run(go())


def items(n, model="demo"):
    return [{"custom_id": f"c{i}", "model": model,
             "messages": [{"role": "user", "content": f"hi {i}"}]} for i in range(n)]


# ---- sync batch -----------------------------------------------------------
def test_batch_sends_in_parallel_and_keeps_order(tmp_path):
    live = {"live": 0, "peak": 0}
    app, st = make_app(tmp_path, handler(0.05, live), max_concurrency=0)

    async def script(c):
        return await c.post("/v1/batch", json={"requests": items(12), "concurrency": 6},
                            headers=H)
    body = session(app, script).json()
    assert [x["index"] for x in body["results"]] == list(range(12))
    assert [x["custom_id"] for x in body["results"]] == [f"c{i}" for i in range(12)]
    assert body["summary"]["ok"] == 12 and body["summary"]["failed"] == 0
    assert live["peak"] > 1, live                       # actually parallel
    assert live["peak"] <= 6, live                      # batch semaphore honoured
    assert body["summary"]["wall_ms"] < 12 * 50, body["summary"]   # not serialised


def test_batch_costs_use_billing_conversion(tmp_path):
    app, st = make_app(tmp_path, handler(), max_concurrency=0)
    st.settings.billing.currency = "CNY"
    st.settings.billing.rates["CNY"] = 7.5

    async def script(c):
        return await c.post("/v1/batch", json={"requests": items(3), "concurrency": 3},
                            headers=H)
    body = session(app, script).json()
    per = body["results"][0]["cost"]
    assert per["currency"] == "USD" and per["amount"] == 0.010   # 1k*2 + 1k*8 per 1M
    assert per["display"] == 0.075 and per["display_currency"] == "CNY"
    assert body["summary"]["currency"] == "CNY"
    assert abs(body["summary"]["cost"] - 0.225) < 1e-9, body["summary"]


def test_batch_reports_per_item_errors_without_failing_the_call(tmp_path):
    app, st = make_app(tmp_path, handler(fail_on={"flaky"}), max_concurrency=0)
    req = items(3) + [{"custom_id": "bad", "model": "flaky",
                       "messages": [{"role": "user", "content": "x"}]}]

    async def script(c):
        r = await c.post("/v1/batch", json={"requests": req, "concurrency": 4}, headers=H)
        return r.status_code, r.json()
    code, body = session(app, script)
    assert code == 200
    assert body["summary"]["ok"] == 3 and body["summary"]["failed"] == 1, body["summary"]
    # Router never converges, so the batch layer bounds each item (5s) instead
    # of letting one stuck model hang the whole batch.
    assert body["results"][3]["status"] == 504
    assert "did not answer" in body["results"][3]["error"]["message"]


def test_batch_rejects_unknown_models_up_front(tmp_path):
    app, st = make_app(tmp_path, handler(), max_concurrency=0)

    async def script(c):
        r = await c.post("/v1/batch", json={"requests": items(2) + [
            {"model": "ghost", "messages": []}], "concurrency": 2}, headers=H)
        return r.status_code, r.json()
    code, body = session(app, script)
    assert code == 404
    assert "ghost" in body["detail"]["error"]["message"]


def test_batch_fail_fast_skips_remaining(tmp_path):
    app, st = make_app(tmp_path, handler(fail_on={"flaky"}), max_concurrency=0)

    async def script(c):
        return await c.post("/v1/batch", json={"requests": items(8, "flaky"),
                                               "concurrency": 1, "fail_fast": True}, headers=H)
    body = session(app, script).json()
    assert body["summary"]["failed"] >= 1
    assert body["summary"]["skipped"] >= 1, body["summary"]




def test_batch_validates_input(tmp_path):
    app, st = make_app(tmp_path, handler(), max_concurrency=0)

    async def script(c):
        return [
            (await c.post("/v1/batch", json={"requests": []}, headers=H)).status_code,
            (await c.post("/v1/batch", json={"requests": [{"messages": []}]},
                          headers=H)).status_code,
            (await c.post("/v1/batch", json={"requests": items(2), "concurrency": 999},
                          headers=H)).status_code,
            (await c.post("/v1/batch", json={"requests": items(2)})).status_code,
        ]
    assert session(app, script) == [400, 400, 400, 401]


def test_batch_accounts_usage_and_concurrency(tmp_path):
    app, st = make_app(tmp_path, handler(), max_concurrency=0)

    async def script(c):
        return await c.post("/v1/batch", json={"requests": items(5), "concurrency": 5},
                            headers=H)
    session(app, script)
    assert len(st.usage.recent(20)) == 5
    snap = st.meter.snapshot(st.gate.limits())
    assert snap["total"] == 5 and snap["active"] == 0


# ---- async jobs -----------------------------------------------------------
def test_async_job_reports_progress_and_results(tmp_path):
    app, st = make_app(tmp_path, handler(0.05), max_concurrency=0)

    async def script(c):
        r = await c.post("/v1/batch", json={"requests": items(6), "concurrency": 2},
                         params={"async": 1}, headers=H)
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        assert r.json()["poll"] == f"/v1/batches/{job}"
        seen = []
        for _ in range(80):
            view = (await c.get(f"/v1/batches/{job}", headers=H)).json()
            seen.append(view["status"])
            if view["status"] == "done":
                return view, seen
            await asyncio.sleep(0.02)
        return view, seen

    final, seen = session(app, script)
    assert final["status"] == "done" and final["completed"] == 6
    assert final["progress"] == 1.0
    assert [x["index"] for x in final["results"]] == list(range(6))
    assert final["summary"]["ok"] == 6
    assert "running" in seen, seen          # polling really observed progress


def test_job_list_wait_and_delete(tmp_path):
    app, st = make_app(tmp_path, handler(0.03), max_concurrency=0)

    async def script(c):
        job = (await c.post("/v1/batch", json={"requests": items(3), "concurrency": 1},
                            params={"async": 1}, headers=H)).json()["job"]
        listed = (await c.get("/v1/batches", headers=H)).json()
        assert any(j["job"] == job for j in listed["jobs"])
        done = (await c.get(f"/v1/batches/{job}", params={"wait": 10}, headers=H)).json()
        gone = await c.delete(f"/v1/batches/{job}", headers=H)
        missing = await c.get("/v1/batches/nope", headers=H)
        missing_del = await c.delete("/v1/batches/nope", headers=H)
        return done, gone.json(), missing.status_code, missing_del.status_code

    done, gone, m1, m2 = session(app, script)
    assert done["status"] == "done" and done["completed"] == 3
    assert gone["cancelled"] is False        # already finished -> nothing to cancel
    assert (m1, m2) == (404, 404)


def test_cancel_stops_a_running_job(tmp_path):
    app, st = make_app(tmp_path, handler(0.1), max_concurrency=0)

    async def script(c):
        job = (await c.post("/v1/batch", json={"requests": items(20), "concurrency": 1},
                            params={"async": 1}, headers=H)).json()["job"]
        await asyncio.sleep(0.05)
        ack = (await c.delete(f"/v1/batches/{job}", headers=H)).json()
        await asyncio.sleep(0.05)
        view = (await c.get(f"/v1/batches/{job}", headers=H)).json()
        return ack, view

    ack, view = session(app, script)
    assert ack["cancelled"] is True
    assert view["status"] == "cancelled", view
    assert view["completed"] < 20 and view["cancelled"] >= 1, view
    assert len(st.usage.recent(50)) == view["completed"]   # finished work is accounted


def test_jobs_are_bounded_and_evicted(tmp_path):
    runner = BatchRunner(keep=3)

    class FakeRouter:
        s = None

        async def complete(self, payload):
            return {"usage": {}}

    FakeRouter.s = make_settings(tmp_path)

    async def go():
        for _ in range(6):
            await runner.run(FakeRouter(), [{"model": "demo"}], 1)
        job = runner.start(FakeRouter(), [{"model": "demo"}], 1)
        await asyncio.sleep(0.05)
        return len(runner.jobs), job

    n, job = asyncio.run(go())
    assert n <= 4, n                                  # finished jobs evicted
    assert job.status in ("running", "done")
    assert runner.running() <= 1


def test_batch_size_limit(tmp_path):
    runner = BatchRunner(max_items=2)

    class FakeRouter:
        s = None

    FakeRouter.s = make_settings(tmp_path)

    async def go():
        with pytest.raises(BatchTooLarge):
            await runner.run(FakeRouter(), [{"model": "m"}] * 3, 1)

    asyncio.run(go())
