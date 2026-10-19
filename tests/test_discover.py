"""Discovery tests: model listing, test-message probing, param classification,
catalog caching, apply-to-config, async job + progress."""
import asyncio
import json
import time

import httpx
import pytest
import yaml

from SMSocket.config import ProviderSpec, Settings, load_config
from SMSocket.discover import (CHAT_PARAMS, Catalog, Discoverer, classify,
                                list_headers, models_url, parse_model_list)
from SMSocket.gateway import create_app
from SMSocket.providers import mask, learn_rate_limit
from SMSocket.router import Router

KEY = "sk-test"
H = {"Authorization": "Bearer " + KEY}


def make_settings(tmp_path, **kw):
    return Settings(listen="127.0.0.1:8000", master_keys=[KEY], strategy="priority", cooldown=0.0, db_path=str(tmp_path / "usage.sqlite3"),
                    providers=[ProviderSpec(name="VendorX", base_url="https://x.example/v1",
                                            keys=[KEY], models={"old-alias": "x-old"},
                                            priority=5)],
                    **kw)


def upstream(serve=("x-old", "x-big", "x-small"), reject=("reasoning_effort", "tools"),
            list_ok=True, live=None, slow=0.0, chat_deny=()):
    """Fake provider: /v1/models + chat with per-parameter rejection."""
    async def h(request: httpx.Request) -> httpx.Response:
        if live is not None:
            live["n"] = live.get("n", 0) + 1
            live["peak"] = max(live.get("peak", 0), live["n"])
        if slow:
            await asyncio.sleep(slow)
        if live is not None:
            live["n"] -= 1
        if request.url.path.endswith("/models") and request.method == "GET":
            if not list_ok:
                return httpx.Response(403, json={"error": {"message": "models endpoint off"}})
            return httpx.Response(200, headers={"content-type": "application/json"}, json={
                "object": "list",
                "data": [{"id": m, "object": "model", "owned_by": "vendorx",
                          "created": 1700000000,
                          "context_length": 128000 if m == "x-big" else 8000}
                         for m in serve]})
        body = json.loads(request.content)
        model = str(body.get("model") or "")
        if model in chat_deny:
            return httpx.Response(404, json={"error": {
                "message": f"The model '{model}' does not exist"}})
        if model not in serve:
            return httpx.Response(404, json={"error": {"message": f"The model '{model}' does not exist"}})
        for param in reject:
            if param in body:
                return httpx.Response(400, json={"error": {
                    "message": f"Unknown parameter: '{param}'"}})
        if body.get("stream"):
            sse = (b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                   b"data: [DONE]\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse)
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}})
    return httpx.MockTransport(h)


CFG = """listen: 127.0.0.1:8011
db_path: "{db}"
strategy: priority
cooldown: 0
providers:
  - name: VendorX
    base_url: https://x.example/v1
    keys: [sk-test]
    models:
      old-alias: x-old
    priority: 5
"""


def make_app(tmp_path, transport, monkeypatch, **kw):
    cfg = tmp_path / "config.yaml"
    db = str(tmp_path / "usage.sqlite3").replace("\\", "/")
    cfg.write_text(CFG.replace(chr(123) + "db" + chr(125), db), encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg))
    monkeypatch.setenv("SMSSOCKET_MASTER_KEY", KEY)
    monkeypatch.delenv("SMSSOCKET_NO_KEY", raising=False)
    settings = load_config(cfg)
    app = create_app(settings)
    st = app.state.llm
    st.http = httpx.AsyncClient(transport=transport)
    st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
    return app, st


def session(app, script):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as c:
            return await script(c)
    return asyncio.run(go())


# ---- pure functions -------------------------------------------------------
def test_parse_model_list_shapes():
    assert [m["id"] for m in parse_model_list(
        {"data": [{"id": "a"}, {"id": "b"}]})] == ["a", "b"]
    assert parse_model_list({"data": [{"name": "n1"}]})[0]["id"] == "n1"
    got = parse_model_list({"data": [{"id": "a", "model_metadata": {"context_window": 32000}}]})
    assert got[0]["context"] == 32000
    assert parse_model_list({"data": [{"id": ""}, {"nope": 1}]}) == []
    assert parse_model_list([{"id": "x"}])[0]["id"] == "x"


def test_models_url_and_masking():
    p = ProviderSpec(name="p", base_url="https://a.example/v1", keys=["sk-1234567890abcdef"])
    assert models_url(p) == "https://a.example/v1/models"
    p2 = ProviderSpec(name="p", base_url="https://a.example", keys=["k"])
    assert models_url(p2) == "https://a.example/v1/models"
    assert "1234567890abcdef" not in mask("sk-1234567890abcdef")


def test_classify_verdicts():
    assert classify(200, "", "temperature") == "supported"
    assert classify(400, "Unknown parameter: 'tools'", "tools") == "rejected"
    assert classify(404, "model not found", "seed") == "rejected"
    assert classify(500, "internal", "seed") == "unknown"
    assert classify(0, "ConnectError", "seed") == "unknown"


# ---- discoverer -----------------------------------------------------------
def test_probe_provider_lists_and_probes_every_model(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    d = Discoverer(st, httpx.AsyncClient(transport=upstream()))
    rep = asyncio.run(d.probe_provider(st.providers[0], concurrency=4, timeout=5.0))
    ids = sorted(m["model"] for m in rep["models"])
    assert ids == ["x-big", "x-old", "x-small"]
    assert rep["listed"] == 3 and rep["ok"] == 3 and rep["failed"] == 0
    big = next(m for m in rep["models"] if m["model"] == "x-big")
    assert big["context"] == 128000 and big["owned_by"] == "vendorx"
    assert big["usage"]["total_tokens"] == 10 and big["reply"] == "ok"
    assert big["params"]["temperature"] == "supported"
    assert big["params"]["reasoning_effort"] == "rejected"
    assert big["params"]["tools"] == "rejected"
    assert big["param_detail"]["tools"]["status"] == 400
    await_close = None
    asyncio.run(d.http.aclose())


def test_probe_marks_unusable_models_and_skips_unlisted_ones(tmp_path, monkeypatch):
    """A listed-but-unusable model is reported; models the provider never lists
    are not probed (the listing is authoritative)."""
    st = make_settings(tmp_path)
    d = Discoverer(st, httpx.AsyncClient(transport=upstream(chat_deny={"x-small"})))
    rep = asyncio.run(d.probe_provider(st.providers[0], concurrency=2, timeout=5.0))
    dead = next(m for m in rep["models"] if m["model"] == "x-small")
    assert dead["ok"] is False and dead["status"] == 404
    assert "does not exist" in dead["error"]
    assert dead["params"] == {}                 # never probed further
    assert rep["ok"] == 2 and rep["failed"] == 1
    asyncio.run(d.http.aclose())


def test_probe_ignores_models_the_provider_does_not_list(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    d = Discoverer(st, httpx.AsyncClient(transport=upstream(serve=("x-old",))))
    rep = asyncio.run(d.probe_provider(st.providers[0], concurrency=2, timeout=5.0))
    assert [m["model"] for m in rep["models"]] == ["x-old"]
    assert rep["ok"] == 1
    asyncio.run(d.http.aclose())


def test_probe_respects_model_filter_and_param_switch(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    d = Discoverer(st, httpx.AsyncClient(transport=upstream()))
    rep = asyncio.run(d.probe_provider(st.providers[0], models=["x-big"],
                                       probe_params=False, probe_stream=False,
                                       timeout=5.0))
    assert [m["model"] for m in rep["models"]] == ["x-big"]
    assert rep["models"][0]["params"] == {}
    asyncio.run(d.http.aclose())


def test_probe_without_key_reports_not_probes(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    st.providers[0].keys = []
    d = Discoverer(st, httpx.AsyncClient(transport=upstream()))
    rep = asyncio.run(d.probe_provider(st.providers[0]))
    assert rep["models"] == [] and "no api key" in rep["error"]
    asyncio.run(d.http.aclose())


def test_probe_runs_in_parallel(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    live = {"n": 0, "peak": 0}
    d = Discoverer(st, httpx.AsyncClient(transport=upstream(live=live, slow=0.05)))
    rep = asyncio.run(d.probe_provider(st.providers[0], concurrency=3, probe_params=False,
                                       probe_stream=False, timeout=5.0))
    assert rep["probed"] == 3 and live["peak"] > 1, live
    asyncio.run(d.http.aclose())


# ---- catalog --------------------------------------------------------------
def test_catalog_roundtrip_and_scoping(tmp_path, monkeypatch):
    c = Catalog(str(tmp_path / "cat.sqlite3"))
    rows = [{"provider": "A", "model": "a1", "ok": True},
            {"provider": "A", "model": "a2", "ok": False},
            {"provider": "B", "model": "b1", "ok": True}]
    assert c.put(rows) == 3
    assert len(c.all()) == 3 and [r["model"] for r in c.all("B")] == ["b1"]
    assert c.last_seen() > 0
    c.put([{"provider": "A", "model": "a1", "ok": True, "context": 99}])   # replace
    assert len(c.all()) == 3
    assert next(r for r in c.all("A") if r["model"] == "a1")["context"] == 99
    assert c.clear("B") == 1 and len(c.all()) == 2
    c.close()


# ---- routes ---------------------------------------------------------------
def test_discover_route_caches_catalog(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    r = session(app, lambda c: c.post("/admin/discover", headers=H,
                                      json={"providers": ["VendorX"], "concurrency": 4,
                                           "timeout": 5.0}))
    assert r.status_code == 200, r.text
    rep = r.json()["providers"][0]
    assert rep["ok"] == 3 and rep["probed"] == 3
    cached = session(app, lambda c: c.get("/admin/models", headers=H)).json()
    assert cached["count"] == 3
    assert any(m["model"] == "x-big" and m["context"] == 128000 for m in cached["data"])
    assert cached["published"] == ["old-alias"]


def test_discover_validates_target(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    bad = session(app, lambda c: c.post("/admin/discover", headers=H,
                                        json={"providers": ["Ghost"]}))
    assert bad.status_code == 404 and "Ghost" in bad.json()["detail"]["error"]["message"]
    junk = session(app, lambda c: c.post("/admin/discover", headers=H,
                                         json={"target": {"base_url": "https://z/v1"}}))
    assert junk.status_code == 400


def test_discover_adhoc_target_reuses_configured_key(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    r = session(app, lambda c: c.post("/admin/discover", headers=H, json={
        "target": {"name": "adhoc", "base_url": "https://x.example/v1"},
        "models": ["x-old"], "probe_params": False, "probe_stream": False, "timeout": 5.0}))
    assert r.status_code == 200, r.text
    rep = r.json()["providers"][0]
    assert rep["provider"] == "adhoc" and rep["ok"] == 1
    assert rep["key"] == mask(KEY)


def test_discover_async_job_and_progress(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(slow=0.05), monkeypatch)
    async def script(c):
        r = await c.post("/admin/discover", headers=H,
                         json={"providers": ["VendorX"], "async": 1, "concurrency": 1,
                               "probe_params": False, "probe_stream": False, "timeout": 5.0})
        assert r.status_code == 200, r.text
        assert r.json()["poll"] == "/admin/discover/status"
        seen = []
        for _ in range(80):
            s = (await c.get("/admin/discover/status", headers=H)).json()
            seen.append((s["status"], s["done"]))
            if s["status"] == "done":
                return s, seen
            await asyncio.sleep(0.02)
        return s, seen
    final, seen = session(app, script)
    assert final["status"] == "done" and final["done"] == 3
    assert any(st_ == "running" for st_, _ in seen), seen
    assert final["providers"] == ["VendorX"]


def test_apply_publishes_new_aliases_only(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    r = session(app, lambda c: c.post("/admin/models/apply", headers=H,
                                      json={"providers": ["VendorX"]}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["added"]) == ["VendorX/x-big", "VendorX/x-small"]
    assert "VendorX/old-alias" not in body["added"]
    assert "old-alias" in session(app, lambda c: c.get("/v1/models", headers=H)).text
    cfg_after = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    served = sorted(cfg_after["providers"][0]["models"])
    assert "x-big" in served and "old-alias" in served
    # second apply is a no-op
    again = session(app, lambda c: c.post("/admin/models/apply", headers=H,
                                          json={"providers": ["VendorX"]})).json()
    assert again["added"] == [] and len(again["skipped"]) == 3


def test_apply_requires_prior_discovery(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    r = session(app, lambda c: c.post("/admin/models/apply", headers=H,
                                      json={"providers": ["VendorX"]}))
    assert r.status_code == 404


def test_apply_alias_prefix(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_params": False,
                                        "probe_stream": False, "timeout": 5.0}))
    r = session(app, lambda c: c.post("/admin/models/apply", headers=H,
                                      json={"providers": ["VendorX"], "alias_prefix": "vx-"}))
    assert sorted(x.split("/")[1] for x in r.json()["added"]) == ["vx-x-big", "vx-x-old",
                                                                  "vx-x-small"]


def test_catalog_clear_route(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    assert session(app, lambda c: c.delete("/admin/models", headers=H,
                                           params={"provider": "VendorX"})).json()["cleared"] == 3
    assert session(app, lambda c: c.get("/admin/models", headers=H)).json()["count"] == 0


def test_catalog_delete_single_row(tmp_path, monkeypatch):
    """DELETE /admin/models/{provider}/{model} drops one cached probe row only."""
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    assert session(app, lambda c: c.get("/admin/models", headers=H)).json()["count"] == 3
    r = session(app, lambda c: c.delete("/admin/models/VendorX/x-big", headers=H))
    assert r.status_code == 200 and r.json()["deleted"] == 1
    rows = session(app, lambda c: c.get("/admin/models", headers=H)).json()
    assert rows["count"] == 2
    assert all(m["model"] != "x-big" for m in rows["data"])
    # the other two providers are untouched
    # deleting again is a 404 (the row is gone)
    assert session(app, lambda c: c.delete("/admin/models/VendorX/x-big",
                                            headers=H)).status_code == 404
    # an unknown provider/model is a 404 too
    assert session(app, lambda c: c.delete("/admin/models/Ghost/ghost",
                                            headers=H)).status_code == 404


def test_tree_unpublished_models_carry_delete_action(tmp_path, monkeypatch):
    """A probed-but-unpublished model shows alias: null and a cache-delete button."""
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    prov = session(app, lambda c: c.get("/admin/provider-models", headers=H)).json()["data"][0]
    byid = {x["upstream"]: x for x in prov["models"]}
    assert byid["x-big"]["published"] is False and byid["x-big"]["alias"] is None
    # published alias keeps published=True
    assert byid["x-old"]["published"] is True and byid["x-old"]["alias"] == "old-alias"
    # the console renders the tree; the unpublished row must offer a cache-delete
    from SMSocket.dashboard import PAGE as html
    assert "data-a=delmodel" in html
    assert "删除缓存" in html
    assert "取消拒绝" in html


def test_delete_records_a_denial_and_skips_the_model_on_reprobe(tmp_path, monkeypatch):
    """Deleting a cached probe row also denies that id: the next freshness pass
    must not re-list and re-probe it, and the tree must show it as denied."""
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    assert session(app, lambda c: c.get("/admin/models", headers=H)).json()["count"] == 3
    r = session(app, lambda c: c.delete("/admin/models/VendorX/x-big", headers=H))
    assert r.status_code == 200 and r.json()["deleted"] == 1 and r.json()["denied"] is True
    assert st.catalog.denied() == {("VendorX", "x-big")}
    assert session(app, lambda c: c.get("/admin/models", headers=H)).json()["count"] == 2

    # the tree still lists the rejected id, flagged denied, with no probe row
    prov = session(app, lambda c: c.get("/admin/provider-models", headers=H)).json()["data"][0]
    byid = {x["upstream"]: x for x in prov["models"]}
    assert byid["x-big"]["denied"] is True
    assert byid["x-big"]["probed"] is False and byid["x-big"]["ok"] is False

    # a second freshness pass would re-list x-big; the discoverer must skip it
    d = Discoverer(st.settings, st.http, st.catalog)
    rep = asyncio.run(d.probe_provider(st.settings.providers[0], probe_params=False,
                                        probe_stream=False, timeout=5.0))
    assert [m["model"] for m in rep["models"]] == ["x-old", "x-small"]
    asyncio.run(d.http.aclose())


def test_undeny_lets_the_discoverer_probe_again(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "timeout": 5.0}))
    session(app, lambda c: c.delete("/admin/models/VendorX/x-big", headers=H))
    assert st.catalog.denied() == {("VendorX", "x-big")}
    r = session(app, lambda c: c.post("/admin/models/VendorX/x-big/undeny", headers=H))
    assert r.status_code == 200 and r.json()["cleared"] == 1
    assert st.catalog.denied() == set()
    d = Discoverer(st.settings, st.http, st.catalog)
    rep = asyncio.run(d.probe_provider(st.settings.providers[0], probe_params=False,
                                        probe_stream=False, timeout=5.0))
    assert "x-big" in [m["model"] for m in rep["models"]]
    asyncio.run(d.http.aclose())


def test_discover_requires_auth(tmp_path, monkeypatch):
    app, st = make_app(tmp_path, upstream(), monkeypatch)
    assert session(app, lambda c: c.post("/admin/discover", json={})).status_code == 401
    assert session(app, lambda c: c.get("/admin/models")).status_code == 401


# ----------------------------------------------------------------- keyless ---
CFG_KEYLESS = """listen: 127.0.0.1:8011
db_path: "{db}"
strategy: priority
cooldown: 0
providers:
  - name: VendorX
    base_url: https://x.example/v1
    keyless: true
    keys: []
    models:
      free-model: free-model
    priority: 5
"""


def test_keyless_provider_probes_and_routes_without_credential(tmp_path, monkeypatch):
    """A provider flagged keyless: no key, no Authorization header, still routable.

    OpenCode serves its free models and its public /v1/models without any
    credential - the gateway must not send a bogus ``Bearer `` token (which
    makes the upstream reject the call) and must still route requests to it.
    """
    # upstream that answers 401 when an Authorization header is present
    seen = {"auth": []}
    async def h(request: httpx.Request) -> httpx.Response:
        seen["auth"].append(request.headers.get("authorization") or "")
        if request.url.path.endswith("/models") and request.method == "GET":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={
                "object": "list",
                "data": [{"id": "free-model", "object": "model", "owned_by": "opencode",
                          "created": 1700000000}]})
        body = json.loads(request.content)
        if str(body.get("model")) != "free-model":
            return httpx.Response(404, json={"error": {"message": "no such model"}})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    transport = httpx.MockTransport(h)

    cfg = tmp_path / "config.yaml"
    db = str(tmp_path / "usage.sqlite3").replace("\\", "/")
    cfg.write_text(CFG_KEYLESS.replace(chr(123) + "db" + chr(125), db), encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg))
    monkeypatch.setenv("SMSSOCKET_MASTER_KEY", KEY)
    settings = load_config(cfg)
    p = settings.providers[0]
    assert p.keyless is True and p.keys == []
    app = create_app(settings)
    st = app.state.llm
    st.http = httpx.AsyncClient(transport=transport)
    st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)

    # discovery: no Authorization header sent on the /models GET
    session(app, lambda c: c.post("/admin/discover", headers=H,
                                  json={"providers": ["VendorX"], "probe_stream": False,
                                        "probe_params": False, "timeout": 5.0}))
    assert seen["auth"][0] in (None, ""), seen["auth"]
    rows = session(app, lambda c: c.get("/admin/models", headers=H)).json()
    assert rows["count"] == 1 and rows["data"][0]["model"] == "free-model"
    assert rows["data"][0]["ok"] is True

    # the keyless slot is live: a client call routes through it without a key
    r = session(app, lambda c: c.post("/v1/chat/completions", headers=H, json={
        "model": "free-model",
        "messages": [{"role": "user", "content": "hi"}]}))
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    # every upstream call was keyless (no Bearer header anywhere)
    assert all(a in (None, "") for a in seen["auth"]), seen["auth"]


def test_keyless_provider_not_probeable_without_flag(tmp_path, monkeypatch):
    """A provider with no key and keyless:false is skipped, like before."""
    st = make_settings(tmp_path)
    st.providers[0].keys = []
    d = Discoverer(st, httpx.AsyncClient(transport=upstream()))
    rep = asyncio.run(d.probe_provider(st.providers[0], concurrency=4, timeout=5.0))
    assert rep["error"] == "no api key configured"
    asyncio.run(d.http.aclose())


# ----------------------------------------------------------------- rpm -----
def test_learn_rate_limit_from_standard_headers():
    p = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY])
    assert learn_rate_limit(p, {"x-ratelimit-limit-requests": "60"}, "") == 60
    assert p.max_rpm == 60
    # a lower learned value narrows an existing cap; a higher one does not loosen it
    assert learn_rate_limit(p, {"x-ratelimit-limit-requests": "120"}, "") == 60
    p2 = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY], max_rpm=30)
    assert learn_rate_limit(p2, {"x-ratelimit-limit-requests": "120"}, "") == 30
    assert learn_rate_limit(p2, {"x-ratelimit-limit-requests": "20"}, "") == 20


def test_learn_rate_limit_from_error_messages():
    p = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY])
    cases = [
        ("Rate limit exceeded: 20 rpm", 20),
        ("You have exceeded your free tier limit of 20 requests/minute", 20),
        ("rpm limit: 5", 5),
        ("请求频率过高，每分钟限制 100 次", 100),
        ("try again in 5 minutes", None),          # a wait window, not a rate
        ("generic rate limited", None),            # nothing named -> nothing learned
    ]
    for msg, want in cases:
        p.max_rpm = 0
        assert learn_rate_limit(p, None, msg) == want, msg
        assert p.max_rpm == (want or 0)


def test_learn_rate_limit_header_beats_message():
    p = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY])
    assert learn_rate_limit(p, {"x-ratelimit-limit": "10"}, "limit of 99 rpm") == 10


# --------------------------------------------------------------- opencode ---
def test_opencode_style_sends_cli_identity_headers():
    """OpenCode's free tier is gated behind the OpenCode CLI identity: a bare
    Authorization header is not enough, the request has to look like the agent
    talking to its own backend (User-Agent + x-opencode-* + a session id)."""
    from SMSocket.discover import list_headers
    from SMSocket.upstream import headers, _opencode_headers
    p = ProviderSpec(name="opencode", base_url="https://opencode.ai/zen/v1",
                     style="opencode", keys=[], keyless=True)
    h = headers(p, "")
    assert h["authorization"] == "Bearer public"
    assert h["user-agent"] == "opencode/1.18.35"
    assert h["x-opencode-client"] == "cli"
    assert h["x-opencode-project"] == "global"
    assert h["x-opencode-session"].startswith("ses_")
    assert h["x-opencode-session-id"].startswith("ses_")
    # a real key is forwarded instead of the public bearer
    h2 = headers(p, "oc_sk-1234")
    assert h2["authorization"] == "Bearer oc_sk-1234"
    # the /models listing goes through the same gate
    lh = list_headers(p, "")
    assert lh["authorization"] == "Bearer public"
    assert lh["user-agent"] == "opencode/1.18.35"
    # the session id is minted the way the real client mints one: ms*0x1000+counter
    ms = int(time.time() * 1000)
    sid = int(_opencode_headers("")["x-opencode-session"][4:], 16)
    want = ((ms << 12) | (ms & 0xF)) & 0xFFFFFFFFFFFF
    # time may tick between the two reads; accept the current or next ms
    want_next = (((ms + 1) << 12) | ((ms + 1) & 0xF)) & 0xFFFFFFFFFFFF
    assert sid in (want, want_next)


def test_probe_rate_limit_no_hits_leaves_cap_untouched():
    """A burst that stays under the cap must not invent a limit: nothing learned,
    provider keeps whatever it had (0 = unlimited), and live 429s remain the
    source of truth that the router will pick up later."""
    p = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY], models={"m": "m"})
    settings = Settings(listen="127.0.0.1:8000", master_keys=[KEY],
                        strategy="priority", cooldown=0.0,
                        db_path="x.sqlite3", providers=[p])

    async def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "c", "choices": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await Discoverer(settings, c).probe_rate_limit(p, burst=4, timeout=5.0)

    rep = asyncio.run(run())
    assert rep["limit_hits"] == 0 and rep["max_rpm"] is None, rep
    assert p.max_rpm == 0
    """The burst crosses the cap; the provider's 429 carries the RPM and the
    shared ProviderSpec learns it - the sliding-window limiter then enforces
    it with no config edit and no restart."""
    calls = [0]
    limit_body = {"error": {"message": "Rate limit exceeded: 3 rpm for free accounts"}}

    async def h(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] <= 3:
            return httpx.Response(200, json={"id": "c", "object": "chat.completion",
                                             "choices": []})
        return httpx.Response(429, json=limit_body)

    p = ProviderSpec(name="v", base_url="https://x/v1", keys=[KEY],
                     models={"m": "m"})
    settings = Settings(listen="127.0.0.1:8000", master_keys=[KEY],
                       strategy="priority", cooldown=0.0,
                       db_path="x.sqlite3", providers=[p])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            d = Discoverer(settings, c)
            return await d.probe_rate_limit(p, burst=6, timeout=5.0, model="m")

    rep = asyncio.run(run())
    assert rep["limit_hits"] == 3 and rep["max_rpm"] == 3, rep
    assert p.max_rpm == 3                                # written onto the spec
