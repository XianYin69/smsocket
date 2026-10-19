"""v0.5 tests: console session, server-side theme, currency list, automatic
discovery, clash settings in the admin surface, the Responses API surface and
weight/priority derived from measurement."""
import json
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from SMSocket.config import ProviderSpec, Settings, load_config
from SMSocket.gateway import create_app

KEY = "sk-test"
H = {"Authorization": "Bearer " + KEY}
CFG = """listen: 127.0.0.1:8099
master_keys: [sk-test]
db_path: "{db}"
providers:
  - name: VendorX
    base_url: http://mock/v1
    keys: [kx]
    models:
      old-alias: x-old
"""


class SSEStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p.encode()


def _chat(body):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1,
            "model": body.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def _resp(body):
    return {"id": "resp_1", "object": "response", "created_at": 1,
            "status": "completed", "model": body.get("model"),
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": "hello responses", "annotations": []}]}],
            "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}}


def handler(request: httpx.Request) -> httpx.Response:
    """One fake provider that answers both OpenAI surfaces, chat and Responses."""
    url = str(request.url)
    if url.endswith("/v1/models"):
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "x-old"}, {"id": "x-new"}]})
    body = json.loads(request.content)
    if url.endswith("/v1/responses"):
        if body.get("stream"):
            return httpx.Response(200, stream=SSEStream([
                'data: {"type":"response.output_text.delta","delta":"he"}\n\n',
                'data: {"type":"response.output_text.delta","delta":"llo"}\n\n',
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"usage":{"input_tokens":4,"output_tokens":2}}}\n\n']))
        return httpx.Response(200, json=_resp(body))
    if url.endswith("/v1/chat/completions") and body.get("stream"):
        return httpx.Response(200, stream=SSEStream([
            'data: {"choices":[{"index":0,"delta":{"content":"he"}}]}\n\n',
            'data: {"choices":[{"index":0,"delta":{"content":"llo"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
            "data: [DONE]\n\n"]))
    return httpx.Response(200, json=_chat(body))


def _settings(tmp_path, providers=None, **kw):
    return Settings(listen="127.0.0.1:8099", master_keys=[KEY],
                    db_path=str(tmp_path / "usage.sqlite3"),
                    providers=providers or [
                        ProviderSpec(name="VendorX", base_url="http://mock/v1",
                                     keys=["kx"], models={"demo": "x-old"})],
                    **kw)


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CFG.replace("{db}", str((tmp_path / "usage.sqlite3")).replace("\\", "/")),
                   encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg))
    monkeypatch.delenv("SMSSOCKET_MASTER_KEY", raising=False)
    return cfg


@pytest.fixture()
def app_client(tmp_path, monkeypatch, cfg_file):
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = _settings(tmp_path)
    app = create_app(st)
    with TestClient(app) as c:
        yield c, app.state.llm        # the live State, not the Settings object


# ---- 1. the console must stop answering 401 -------------------------------
def test_console_gets_a_session_cookie_and_stops_401ing(app_client):
    c, _ = app_client
    r = c.get("/")
    assert r.status_code == 200
    assert "sms_console" in r.cookies, dict(r.cookies)
    # no Authorization header anywhere from here on
    assert c.get("/admin/config").status_code == 200
    assert c.get("/v1/models").status_code == 200
    assert c.get("/stats").status_code == 200


def test_a_forged_cookie_is_still_refused(app_client):
    c, _ = app_client
    r = c.get("/admin/config", headers={"Cookie": "sms_console=not-minted"})
    assert r.status_code == 401
    assert c.get("/admin/config", headers=H).status_code == 200


# ---- 2. the theme is remembered by the gateway, not by one browser --------
def test_theme_is_saved_on_the_server(app_client, cfg_file):
    c, st = app_client
    assert 'class="skin-a"' in c.get("/").text
    r = c.put("/admin/ui", headers=H, json={"skin": "c"})
    assert r.status_code == 200 and r.json()["saved"] is True
    assert st.settings.ui.skin == "c"
    assert 'class="skin-c"' in c.get("/").text
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["ui"]["skin"] == "c", raw


def test_bad_skin_is_refused(app_client):
    c, _ = app_client
    assert c.put("/admin/ui", headers=H, json={"skin": "z"}).status_code == 400


# ---- 3. the currency list is rendered, not fetched-and-hoped -------------
def test_currency_list_is_in_the_page(app_client):
    c, _ = app_client
    html = c.get("/").text
    assert "__CUR__" not in html and "__SYM__" not in html
    start = html.index("const CUR=[")
    listed = json.loads(html[start + len("const CUR="):html.index("];", start) + 1])
    assert len(listed) >= 20 and "CNY" in listed and "USD" in listed
    b = c.get("/admin/billing").json()
    assert len(b["table"]["supported"]) >= 20, b["table"]


# ---- 4. adding a model probes, publishes and measures by itself -----------
@pytest.fixture()
def file_client(tmp_path, monkeypatch, cfg_file):
    """App driven by a real config file (admin writes land in tmp, never in the
    operator's config.yaml)."""
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    app = create_app(load_config(cfg_file))
    with TestClient(app) as c:
        yield c, app.state.llm


def _wait_for(pred, secs=15.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_new_provider_is_discovered_without_being_asked(file_client, cfg_file):
    c, st = file_client
    r = c.post("/admin/providers", headers=H,
               json={"name": "NewX", "base_url": "http://mock/v1",
                     "keys": ["kn"], "models": {}})
    assert r.status_code == 200, r.text
    assert r.json().get("auto_discover") == "queued", r.json()

    def published():
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        p = [x for x in raw["providers"] if x.get("name") == "NewX"]
        return bool(p) and "x-new" in (p[0].get("models") or {})

    assert _wait_for(published), yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert "x-new" in st.settings.model_index(), sorted(st.settings.model_index())


def test_auto_discover_can_be_switched_off(file_client, cfg_file):
    c, st = file_client
    # the switch lives in the config file: apply_state() re-reads it, so this is
    # the only honest way to turn the behaviour off
    assert c.put("/admin/config", headers=H,
                 json={"discover": {"on_add": False}}).status_code == 200
    assert st.settings.discover.on_add is False
    r = c.post("/admin/providers", headers=H,
               json={"name": "Quiet", "base_url": "http://mock/v1",
                     "keys": ["kq"], "models": {}})
    assert r.json().get("auto_discover") == "off", r.json()
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    quiet = [x for x in raw["providers"] if x.get("name") == "Quiet"][0]
    assert not (quiet.get("models") or {}), quiet


# ---- 5. clash lives in the settings surface ------------------------------
def test_clash_block_is_set_and_validated(file_client):
    c, st = file_client
    r = c.put("/admin/config", headers=H, json={"clash": {"enabled": True,
                                             "controller": "http://127.0.0.1:9090",
                                             "mixed_port": 7890, "mode": "auto",
                                             "groups": "PROXY, AI",
                                             "provider_proxy": "VendorX=proxy"}})
    assert r.status_code == 200, r.text
    snap = c.get("/admin/config", headers=H).json()
    k = snap["clash"]
    assert k["enabled"] is True and k["mode"] == "auto"
    assert k["groups"] == ["PROXY", "AI"], k
    assert k["provider_proxy"] == {"VendorX": "proxy"}, k
    assert "net_plane" in r.json()["applied"], r.json()["applied"]


def test_clash_bad_mode_is_rejected(file_client):
    c, _ = file_client
    r = c.put("/admin/config", headers=H, json={"clash": {"mode": "sideways"}})
    assert r.status_code == 400, r.text


def test_clash_secret_is_never_echoed_and_empty_keeps_it(file_client):
    c, st = file_client
    c.put("/admin/config", headers=H,
          json={"clash": {"enabled": True, "secret": "top-secret"}})
    assert c.get("/admin/config", headers=H).json()["clash"]["secret"] == "***"
    c.put("/admin/config", headers=H, json={"clash": {"mode": "proxy"}})
    assert st.settings.clash.secret == "top-secret"
    assert st.settings.clash.mode == "proxy"


def test_tune_block_persists_values(file_client):
    c, st = file_client
    assert c.put("/admin/config", headers=H,
           json={"tune": {"interval_s": 5}}).status_code == 400
    r = c.put("/admin/config", headers=H,
               json={"tune": {"min_samples": 7, "persist": True}})
    assert r.status_code == 200, r.text
    assert st.settings.tune.min_samples == 7 and st.settings.tune.persist is True


# ---- 6. the Responses API is a real second surface ------------------------
def test_responses_surface_against_a_chat_upstream(app_client):
    c, _ = app_client
    r = c.post("/v1/responses", headers=H, json={"model": "demo",
                                                "instructions": "be brief",
                                                "input": "hi",
                                                "max_output_tokens": 9})
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["object"] == "response" and o["status"] == "completed", o
    assert o["output"][0]["content"][0]["type"] == "output_text"
    assert o["output_text"] == "hello"
    assert o["usage"] == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, o
    g = c.get("/v1/responses/" + o["id"], headers=H)
    assert g.status_code == 200 and g.json()["id"] == o["id"]
    d = c.delete("/v1/responses/" + o["id"], headers=H)
    assert d.json()["deleted"] is True
    assert c.get("/v1/responses/" + o["id"], headers=H).status_code == 404


def test_responses_surface_streams_named_events(app_client):
    c, _ = app_client
    with c.stream("POST", "/v1/responses", headers=H,
                  json={"model": "demo", "input": [{"role": "user", "content": "hi"}],
                        "stream": True}) as r:
        assert r.status_code == 200, r.status_code
        text = r.read().decode()
    events = [ln.split("event: ", 1)[1] for ln in text.split("\n")
              if ln.startswith("event: ")]
    for want in ("response.created", "response.output_text.delta",
                 "response.output_text.done", "response.completed"):
        assert want in events, events
    assert "hello" in text
    assert "[DONE]" not in text          # Responses does not use the chat sentinel


def test_responses_unknown_model(app_client):
    c, _ = app_client
    r = c.post("/v1/responses", headers=H, json={"model": "nope", "input": "hi"})
    assert r.status_code == 404, r.text


# ---- 7. a provider that only speaks the Responses API still serves chat ---
def test_responses_style_provider_serves_both_surfaces(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = _settings(tmp_path, providers=[ProviderSpec(
        name="RespX", base_url="http://mock/v1", keys=["kr"],
        style="openai-responses", models={"demo": "x-resp"})])
    with TestClient(create_app(st)) as c:
        r = c.post("/v1/chat/completions", headers=H,
                   json={"model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        o = r.json()
        assert o["object"] == "chat.completion" and o["choices"][0]["message"]["content"] \
            == "hello responses", o
        assert o["usage"]["prompt_tokens"] == 4, o          # input_tokens mapped
        s = c.post("/v1/responses", headers=H, json={"model": "demo", "input": "hi"})
        assert s.status_code == 200 and s.json()["output_text"] == "hello responses"
        with c.stream("POST", "/v1/chat/completions", headers=H,
                      json={"model": "demo", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as sr:
            body = sr.read().decode()
        assert "chat.completion.chunk" in body, body
        text = "".join(json.loads(ln[6:])["choices"][0]["delta"].get("content", "")
                       for ln in body.split("\n")
                       if ln.startswith("data: ") and ln[6:] != "[DONE]"
                       and json.loads(ln[6:])["choices"])
        assert text == "hello", body          # Responses deltas -> chat deltas
        assert '"prompt_tokens": 4' in body, body


# ---- 8. weight and priority come from measurement -------------------------
def test_tuning_derives_ranking_from_measured_traffic(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = _settings(tmp_path, providers=[
        ProviderSpec(name="Fast", base_url="http://mock/v1", keys=["k1"],
                     models={"demo": "x"}),
        ProviderSpec(name="Slow", base_url="http://mock/v1", keys=["k2"],
                     models={"demo": "x"}),
        ProviderSpec(name="Hands", base_url="http://mock/v1", keys=["k3"],
                     models={"demo": "x"}, auto=False, priority=99, weight=50)])
    app = create_app(st)
    with TestClient(app) as c:
        state = app.state.llm
        for _ in range(6):
            state.usage.assess_log(model="demo", provider="Fast", egress="direct",
                                   source="live", ok=1, status=200,
                                   latency_ms=120.0, tok_s=90.0)
            state.usage.assess_log(model="demo", provider="Slow", egress="direct",
                                   source="live", ok=1, status=200,
                                   latency_ms=6000.0, tok_s=4.0)
        assert c.get("/tune", headers=H).json()["enabled"] is True
        r = c.post("/tune/apply", headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        by = {x["provider"]: x for x in d["scores"]}
        assert by["Fast"]["samples"] == 6 and by["Slow"]["samples"] == 6, d
        assert by["Fast"]["priority"] > by["Slow"]["priority"], d
        assert by["Fast"]["weight"] > by["Slow"]["weight"], d
        assert by["Hands"]["note"] == "manual", d
        assert "Hands" not in state.pool.derived, state.pool.derived
        order = [x.provider.name for x in state.pool.candidates("demo", "priority")]
        # the opt-out keeps its typed priority (99), the tuned pair is ranked
        # by measurement: Fast above Slow
        assert order[0] == "Hands", order
        assert order.index("Fast") < order.index("Slow"), order
        assert c.get("/tune", headers=H).json()["in_force"]["Fast"]["priority"] == \
            by["Fast"]["priority"]


def test_tuning_survives_a_reload(tmp_path, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = _settings(tmp_path)
    app = create_app(st)
    with TestClient(app) as c:
        state = app.state.llm
        state.pool.set_derived({"VendorX": {"priority": 7, "weight": 9}})
        reb = state.pool.rebase(state.settings.providers)
        assert reb.derived == {"VendorX": {"priority": 7, "weight": 9}}, reb.derived


def test_tune_block_is_validated(file_client):
    c, st = file_client
    assert c.put("/admin/config", headers=H,
           json={"tune": {"interval_s": 5}}).status_code == 400
    r = c.put("/admin/config", headers=H, json={"tune": {"enabled": False}})
    assert r.status_code == 200 and st.settings.tune.enabled is False
    assert c.post("/tune/apply", headers=H).status_code == 409


# ---- 9. the pieces the console depends on are wired -----------------------
def test_assessor_hands_its_results_to_the_tuner(tmp_path, monkeypatch):
    from SMSocket.config import AssessConfig
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    st = _settings(tmp_path, assess=AssessConfig(enabled=True, interval_s=3600))
    app = create_app(st)
    with TestClient(app):
        state = app.state.llm
        assert state.assessor is not None and state.tuner is not None
        assert state.assessor.tuner is state.tuner, "sweep must reach the tuner"
        assert state.router.tuner is state.tuner
