"""Auto model-hit verification: a probed upstream id is published as an alias
and a /v1/chat/completions call using that alias routes to the right provider.

Simulates the full pipeline without real keys:
  1. Mock provider lists model "agnes-3.0-flash" via GET /models
  2. Discoverer.probe_provider probes it (200 OK)
  3. Catalog stores the row
  4. publish_aliases writes the alias into the config file (adds only)
  5. load_config picks up the alias
  6. POST /v1/chat/completions {"model": "agnes-3.0-flash"} routes to that provider
"""
import asyncio
import json

import httpx
import pytest

from SMSocket.config import ProviderSpec, Settings, load_config
from SMSocket.discover import Catalog, Discoverer, TEST_PROMPT
from SMSocket.gateway import create_app
from SMSocket.providers import Pool
from SMSocket.router import Router

KEY = "sk-test"


def mock_upstream(model_ids=("agnes-3.0-flash", "agnes-2.5-flash")):
    """Provider that lists models and answers chat for any listed model."""
    async def h(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models") and request.method == "GET":
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": m, "object": "model", "owned_by": "agnes"}
                         for m in model_ids]})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            m = str(body.get("model") or "")
            if m not in model_ids:
                return httpx.Response(404, json={"error": {"message": f"unknown model {m}"}})
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "model": m,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_probe_publish_then_hit(tmp_path, monkeypatch):
    """Full pipeline: probe → publish alias → reload → hit via alias."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'listen: 127.0.0.1:8000\nmaster_keys: ["sk-test"]\nstrategy: priority\n'
        'retry: 0\ncd_path: ""\ndb_path: db.sqlite3\n'
        'providers:\n'
        '  - name: agnes\n    base_url: http://mock/v1\n    keys: [sk-test]\n'
        '    models: {}\n    priority: 10\n', encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg_path))

    # 1+2: probe
    st_settings = load_config(cfg_path)
    prov = st_settings.providers[0]
    transport = mock_upstream()
    async def run_probe():
        async with httpx.AsyncClient(transport=transport) as c:
            d = Discoverer(st_settings, c)
            return await d.probe_provider(prov, models=None, test_prompt=TEST_PROMPT,
                                          probe_params=False, probe_stream=False,
                                          concurrency=4, timeout=5.0, max_models=20,
                                          max_params=0)
    rep = asyncio.run(run_probe())
    ok_models = [m["model"] for m in rep["models"] if m["ok"]]
    assert "agnes-3.0-flash" in ok_models, rep

    # 3: catalog
    cat = Catalog(str(tmp_path / "usage.sqlite3"))
    cat.put(rep["models"])
    rows = [r for r in cat.all() if r.get("ok")]
    assert any(r["model"] == "agnes-3.0-flash" for r in rows)

    # 4: publish alias into the config file
    from SMSocket.discover_routes import publish_aliases
    # publish_aliases calls admin.cfg_path() which reads SMSSOCKET_CONFIG env
    st_holder = type("H", (), {})()
    st_holder.settings = st_settings
    st_holder.catalog = cat
    st_holder.pool = Pool(st_settings.providers)

    added, skipped = publish_aliases(st_holder, want={"agnes"})
    assert "agnes/agnes-3.0-flash" in added, (added, skipped)

    # 5: reload config and verify alias is present
    st_settings2 = load_config(cfg_path)
    idx = st_settings2.model_index()
    assert "agnes-3.0-flash" in idx, idx

    # 6: wire router and hit the alias
    app = create_app(st_settings2)
    st2 = app.state.llm
    st2.http = httpx.AsyncClient(transport=mock_upstream())
    st2.router = Router(st2.settings, st2.pool, st2.usage, st2.http, st2.gate)

    async def hit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gw") as c:
            r = await c.post("/v1/chat/completions",
                             headers={"Authorization": "Bearer " + KEY},
                             json={"model": "agnes-3.0-flash",
                                   "messages": [{"role": "user", "content": "hi"}]})
            return r

    r = asyncio.run(hit())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "agnes-3.0-flash"
    assert data["choices"][0]["message"]["content"] == "ok"

    cat.close()


def test_unknown_alias_still_404(tmp_path, monkeypatch):
    """A model that was never probed/published remains a 404 — no silent hit."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'listen: 127.0.0.1:8000\nmaster_keys: ["sk-test"]\nstrategy: priority\n'
        'retry: 0\ncd_path: ""\ndb_path: db.sqlite3\n'
        'providers:\n'
        '  - name: agnes\n    base_url: http://mock/v1\n    keys: [sk-test]\n'
        '    models: {}\n    priority: 10\n', encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg_path))
    st_settings = load_config(cfg_path)
    app = create_app(st_settings)
    st2 = app.state.llm
    st2.http = httpx.AsyncClient(transport=mock_upstream())
    st2.router = Router(st2.settings, st2.pool, st2.usage, st2.http, st2.gate)

    async def hit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gw") as c:
            return await c.post("/v1/chat/completions",
                                headers={"Authorization": "Bearer " + KEY},
                                json={"model": "never-probed-model",
                                      "messages": [{"role": "user", "content": "hi"}]})

    r = asyncio.run(hit())
    assert r.status_code == 404, r.text
