"""Assessment tests: live mirroring, probe sweeps, verdicts, schedule, routes.

Offline throughout: the upstream is an httpx.MockTransport (SSE-capable), so a
probe really streams through the router and the metrics are measured, not faked.
"""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket.assess import Assessor
from SMSocket.config import AssessConfig, ProviderSpec, Settings, assess_from_raw
from SMSocket.gateway import create_app
from SMSocket.usage import Usage

BODIES: list[dict] = []


class SSEStream(httpx.AsyncByteStream):
    """Two events with a gap: time-to-first-byte must be measurable, not 0."""

    def __init__(self, parts, gap=0.0, lead=0.0):
        self.parts = parts
        self.gap = gap
        self.lead = lead          # stall before the first byte = real TTFT

    async def __aiter__(self):
        import asyncio
        for i, p in enumerate(self.parts):
            if i and self.gap:
                await asyncio.sleep(self.gap)
            if i == 0 and self.lead:
                await asyncio.sleep(self.lead)
            yield p.encode()


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    BODIES.append({"url": str(request.url), "body": body})
    if body["model"] == "blocked-up":
        return httpx.Response(403, json={"error": {"message": "region blocked"}})
    if body.get("stream"):
        c = lambda o: "data: " + json.dumps(o) + "\n\n"
        first = c({"choices": [{"index": 0, "delta": {"content": "o"}}]})
        rest = (c({"choices": [], "usage": {"prompt_tokens": 4,
                                            "completion_tokens": 3,
                                            "total_tokens": 7}})
                + "data: [DONE]\n\n")
        return httpx.Response(200, stream=SSEStream([first, rest], lead=0.04))
    return httpx.Response(200, json={
        "id": "c", "object": "chat.completion", "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}})


def _settings(tmp_path, assess=None):
    return Settings(master_keys=["sk-test"], db_path=str(tmp_path / "u.sqlite3"),
                    assess=assess or AssessConfig(), providers=[
                        ProviderSpec(name="openai", base_url="http://api.test/v1",
                                     keys=["sk-up"],
                                     models={"demo": "demo-up",
                                             "blocked": "blocked-up"})])


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(_handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    def build(assess=None):
        BODIES.clear()
        return TestClient(create_app(_settings(tmp_path, assess)))

    return build


def cfg(**kw):
    base = dict(enabled=True, interval_s=3600, max_tokens=5, prompt="ping",
                timeout=5.0, concurrency=2)
    base.update(kw)
    return AssessConfig(**base)


H = {"Authorization": "Bearer sk-test"}


# ---------------------------------------------------------------- config ---
def test_assess_is_off_by_default_and_leaves_no_trace(make_client):
    with make_client() as c:
        assert c.get("/healthz").json()["ok"] is True
        body = c.get("/assess", headers=H).json()
        assert body["enabled"] is False and body["rows"] == []
        assert c.get("/assess/status", headers=H).json()["enabled"] is False
        assert c.post("/assess/run", headers=H, json={}).status_code == 409
        assert c.put("/assess/schedule", headers=H,
                     json={"interval_s": 5}).status_code == 409
        assert c.get("/assess/history", headers=H).status_code == 409
        c.post("/v1/chat/completions", headers=H, json={
            "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert c.get("/assess", headers=H).json()["counts"] == {}


def test_assess_config_validates():
    for bad in [{"at": "24:00"}, {"concurrency": 0}, {"timeout": 0},
                {"max_tokens": 0}, {"live_sample": 1.5}, {"window_s": 0},
                {"interval_s": 0}]:
        with pytest.raises(ValueError):
            assess_from_raw(bad)
    assert assess_from_raw({"at": "3:05"}).at == "03:05"
    assert assess_from_raw({}).enabled is False


# ------------------------------------------------------------------ live ---
def test_live_traffic_is_mirrored_without_extra_calls(make_client):
    with make_client(cfg()) as c:
        for _ in range(2):
            r = c.post("/v1/chat/completions", headers=H, json={
                "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200
        rows = c.get("/assess", headers=H, params={"sources": "live"}).json()["rows"]
        assert len(rows) == 1 and rows[0]["model"] == "demo"
        assert rows[0]["egress"] == "direct" and rows[0]["calls"] == 2
        assert rows[0]["ok_ratio"] == 1.0 and rows[0]["verdict"] == "healthy"
        assert rows[0]["avg_tok_s"] > 0 and rows[0]["p50_ms"] > 0
        hist = c.get("/assess/history", headers=H, params={"model": "demo"}).json()
        assert len(hist["rows"]) == 2
        assert hist["rows"][0]["prompt"] == 4
        assert hist["rows"][0]["completion"] == 3


def test_live_failures_show_up_as_blocked(make_client):
    # With poll-until-success the router never converges on a chat request:
    # a live chat to a fully-blocked model polls forever instead of returning
    # 403/502, so complete() never returns and no live row is written.  The
    # assess probe still reaches a verdict: blocked (timeout, not 403).
    with make_client(cfg(models=["blocked"])) as c:
        out = c.post("/assess/run", headers=H, json={}).json()
        assert out["failed"] == 1 and out["ok"] == 0
        rep = c.get("/assess", headers=H).json()["rows"]
        assert rep[0]["model"] == "blocked" and rep[0]["ok"] == 0
        assert rep[0]["verdict"] == "blocked"
        assert rep[0]["errors"] == [0], rep[0]["errors"]
        assert rep[0]["last_error"].startswith("timeout after"), rep[0]["last_error"]


def test_live_sample_rate_thins_the_harvest(make_client):
    with make_client(cfg(live_sample=0.0)) as c:
        c.post("/v1/chat/completions", headers=H, json={
            "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert c.get("/assess", headers=H, params={"sources": "live"}).json()["rows"] == []


# ----------------------------------------------------------------- probe ---
def test_probe_sweep_records_measured_metrics(make_client):
    with make_client(cfg(models=["demo"])) as c:
        out = c.post("/assess/run", headers=H, json={}).json()
        assert out["status"] == "done" and out["total"] == 1 and out["ok"] == 1
        row = out["rows"][0]
        assert row["model"] == "demo" and row["egress"] == "direct"
        assert row["ok"] == 1 and row["completion"] == 3
        assert row["ttft_ms"] >= 30, row          # the mocked first-byte stall
        assert row["latency_ms"] >= row["ttft_ms"]
        assert row["tok_s"] > 0
        sent = BODIES[-1]["body"]
        assert sent["model"] == "demo-up" and sent["max_tokens"] == 5
        assert sent["messages"][0]["content"] == "ping"
        assert sent["stream"] is True


def test_probe_blocked_egress_gets_a_verdict(make_client):
    with make_client(cfg(models=["blocked"])) as c:
        out = c.post("/assess/run", headers=H, json={}).json()
        assert out["failed"] == 1 and out["ok"] == 0
        rep = c.get("/assess", headers=H).json()["rows"]
        assert rep[0]["verdict"] == "blocked"
        # router never converges: the probe waits for the assess timeout, so
        # the recorded error is a timeout, not the provider's 403.  The
        # verdict is still "blocked" (nothing ever got through).
        assert rep[0]["errors"] == [0], rep[0]["errors"]
        assert rep[0]["last_error"].startswith("timeout after"), rep[0]["last_error"]


def test_probe_rows_are_not_mirrored_twice(make_client):
    with make_client(cfg(models=["demo"])) as c:
        c.post("/assess/run", headers=H, json={})
        assert c.get("/assess", headers=H, params={"sources": "live"}).json()["rows"] == []
        probes = c.get("/assess", headers=H, params={"sources": "probe"}).json()["rows"]
        assert probes and probes[0]["calls"] == 1


def test_async_sweep_reports_progress(make_client):
    with make_client(cfg(models=["demo", "blocked"])) as c:
        r = c.post("/assess/run", headers=H, json={"async": True}).json()
        assert r["accepted"] is True and r["total"] == 2
        for _ in range(200):
            st = c.get("/assess/status", headers=H).json()
            if st["progress"].get("status") == "done":
                break
            time.sleep(0.05)
        assert st["progress"]["done"] == 2 and st["runs"] == 1
        assert st["last_run"] and st["scheduled"] is True


def test_unknown_model_is_rejected_before_any_traffic(make_client):
    with make_client(cfg()) as c:
        r = c.post("/assess/run", headers=H, json={"models": ["nope"]})
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]["error"]["message"]
        assert BODIES == []


# ------------------------------------------------------------- schedule ----
def _bare_assessor(**kw):
    s = Settings(assess=cfg(**kw))
    stub = type("U", (), {"assess_log": staticmethod(lambda **k: None),
                          "assess_counts": lambda self: {},
                          "assess_report": lambda self, **k: []})()
    return Assessor(s, None, None, stub)


def test_next_slot_uses_interval_then_daily_at():
    asr = _bare_assessor(interval_s=900)
    assert asr.next_slot(now=1000.0) == 1900.0
    asr.cfg.at = "03:30"
    noon = time.mktime(time.strptime("2026-10-05 12:00:00", "%Y-%m-%d %H:%M:%S"))
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(asr.next_slot(now=noon))) \
        == "2026-10-06 03:30"
    early = time.mktime(time.strptime("2026-10-05 01:00:00", "%Y-%m-%d %H:%M:%S"))
    assert time.strftime("%Y-%m-%d", time.localtime(asr.next_slot(now=early))) \
        == "2026-10-05"
    asr.cfg.enabled = False
    assert asr.next_slot() == 0.0


def test_schedule_loop_fires_a_sweep(make_client, monkeypatch):
    seen = []
    from SMSocket import assess as mod

    async def fake_sweep(self, models=None, egress=None, concurrency=None):
        seen.append(time.time())
        self.last_run = time.time()
        self.runs += 1
        return {"status": "done"}

    monkeypatch.setattr(mod.Assessor, "sweep", fake_sweep)
    with make_client(cfg(interval_s=1)) as c:
        st = c.app.state.llm
        assert st.assessor is not None and st.assessor.enabled
        for _ in range(160):
            if seen:
                break
            time.sleep(0.05)
        assert seen, "the scheduled sweep never fired"
        assert c.get("/assess/status", headers=H).json()["scheduled"] is True


def test_schedule_can_be_changed_and_stopped_live(make_client):
    with make_client(cfg()) as c:
        st = c.app.state.llm
        out = c.put("/assess/schedule", headers=H, json={
            "interval_s": 90, "at": "22:15", "models": ["demo"],
            "live_sample": 0.5}).json()
        assert out["interval_s"] == 90 and out["at"] == "22:15"
        assert out["models"] == ["demo"]
        assert st.assessor.cfg.live_sample == 0.5
        assert c.put("/assess/schedule", headers=H,
                     json={"at": "99:00"}).status_code == 400
        assert c.put("/assess/schedule", headers=H,
                     json={"live_sample": 3}).status_code == 400
        off = c.put("/assess/schedule", headers=H, json={"enabled": False}).json()
        assert off["enabled"] is False and off["scheduled"] is False
        assert st.assessor._task is None
        back = c.put("/assess/schedule", headers=H, json={"enabled": True}).json()
        assert back["scheduled"] is True


# ---------------------------------------------------------------- units ----
def test_egress_paths_follow_the_net_plane():
    asr = _bare_assessor()
    assert asr.egress_paths() == ["direct"]          # no plane -> direct only

    class Reg:
        @staticmethod
        def paths():
            return ["direct", "node:P/hk", "proxy:http://127.0.0.1:7890"]

    class Net:
        registry = Reg()

    asr.net = Net()
    assert asr.egress_paths() == ["direct", "node:P/hk",
                                  "proxy:http://127.0.0.1:7890"]
    asr.cfg.egress = "node:P/hk"
    assert asr.egress_paths() == ["node:P/hk"]      # pinned to one path


def test_models_filter_by_provider_and_config():
    provs = [ProviderSpec(name="openai", base_url="http://a/v1", keys=["k"],
                          models={"gpt": "gpt"}),
             ProviderSpec(name="anth", base_url="http://b/v1", keys=["k"],
                          models={"claude": "claude"})]
    asr = _bare_assessor()
    asr.s = Settings(providers=provs, assess=cfg())
    assert asr.models() == ["claude", "gpt"]
    asr.cfg.providers = ["openai"]
    assert asr.models() == ["gpt"]
    asr.cfg.models = ["claude"]
    assert asr.models() == ["claude"]               # explicit list wins


def test_report_windows_and_sources(tmp_path):
    u = Usage(str(tmp_path / "a.sqlite3"))
    u.assess_log(model="m", egress="direct", source="probe", ok=1, status=200,
                 latency_ms=100)
    u.con.execute("UPDATE assess SET ts=?", (time.time() - 7200,))
    u.con.commit()
    asr = _bare_assessor(window_s=3600)
    asr.usage = u
    assert asr.report()["rows"] == []               # outside the 1h window
    assert len(asr.report(window_s=4 * 3600)["rows"]) == 1
    assert asr.report(sources=("live",))["rows"] == []
    assert asr.report(window_s=4 * 3600, model="m")["rows"]
    assert asr.report(window_s=4 * 3600, model="other")["rows"] == []
    u.close()


def test_slow_and_unstable_verdicts(tmp_path):
    u = Usage(str(tmp_path / "a.sqlite3"))
    for ms in (9000, 9500):
        u.assess_log(model="slow-m", egress="direct", source="probe", ok=1,
                     status=200, latency_ms=ms)
    for i in range(4):
        u.assess_log(model="flip-m", egress="direct", source="probe",
                     ok=1 if i else 0, status=200 if i else 500, latency_ms=100)
    for _ in range(2):
        u.assess_log(model="flip-m", egress="direct", source="probe",
                     ok=0, status=500)                   # 3 ok / 6 = 0.5
    asr = _bare_assessor(slow_ms=8000)
    asr.usage = u
    rows = {r["model"]: r for r in asr.report()["rows"]}
    assert rows["slow-m"]["verdict"] == "slow"
    assert rows["flip-m"]["verdict"] == "unstable"
    asr.cfg.slow_ms = 20000
    assert {r["model"]: r["verdict"] for r in asr.report()["rows"]}["slow-m"] == "healthy"
    u.close()


def test_by_egress_rollup_groups_the_cells(tmp_path):
    u = Usage(str(tmp_path / "a.sqlite3"))
    u.assess_log(model="m1", egress="direct", source="probe", ok=1, status=200,
                 latency_ms=100)
    u.assess_log(model="m2", egress="direct", source="probe", ok=1, status=200,
                 latency_ms=300)
    u.assess_log(model="m1", egress="node:P/hk", source="probe", ok=0, status=0,
                 error="connection refused")
    asr = _bare_assessor()
    asr.usage = u
    rep = asr.report()
    cells = {d["egress"]: d for d in rep["by_egress"]}
    assert cells["direct"]["cells"] == 2 and cells["direct"]["healthy"] == 2
    assert cells["direct"]["p50_ms"] == 300.0
    assert cells["node:P/hk"]["blocked"] == 1
    assert rep["models"] == 2
    u.close()


# ------------------------------------------------------------- dashboard ---
def test_dashboard_hides_reachability_until_assessment_is_on(make_client):
    with make_client() as c:
        page = c.get("/").text
        assert "__ASSESS_ON__" not in page, "placeholder must be substituted"
        assert "const ASSESS_ON=0;" in page
        assert 'id="reachblk" class="hide"' in page      # block exists, stays hidden


def test_dashboard_shows_reachability_when_enabled(make_client):
    with make_client(cfg()) as c:
        page = c.get("/").text
        assert "const ASSESS_ON=1;" in page
        assert 'id="reach"' in page and "renderStack" in page
        # stack block is present but hidden until /concurrency reports it enabled
        assert 'id="stackblk" class="hide"' in page
