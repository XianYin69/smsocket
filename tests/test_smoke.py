"""Smoke tests: gateway behaviour against a stubbed upstream (no network)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket.config import ProviderSpec, Settings, _env, load_config
from SMSocket.gateway import create_app

CALLS: list[str] = []


def _key(request):
    return (request.headers.get("authorization", "").replace("Bearer ", "")
            or request.headers.get("x-api-key", ""))


class SSEStream(httpx.AsyncByteStream):
    """Chunked SSE body (MockTransport cannot re-iterate plain bytes)."""

    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p.encode()


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    CALLS.append(url)
    if _key(request).startswith("bad"):
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})
    body = json.loads(request.content)
    if url.endswith("/v1/embeddings"):
        inp = body.get("input")
        k = len(inp) if isinstance(inp, list) else 1
        return httpx.Response(200, json={
            "object": "list", "model": body["model"],
            "data": [{"object": "embedding", "index": i, "embedding": [0.5, 0.5]}
                     for i in range(k)],
            "usage": {"prompt_tokens": 3, "total_tokens": 3}})
    return _anthropic(body) if url.endswith("/v1/messages") else _openai(body)


def _anthropic(body):
    if body.get("stream"):
        ev = lambda o: "event: x\ndata: " + json.dumps(o) + "\n\n"
        sse = (ev({"type": "message_start", "message": {"usage": {"input_tokens": 7}}})
               + ev({"type": "content_block_delta", "delta": {"text": "hi from claude"}})
               + ev({"type": "message_stop"}))
        return httpx.Response(200, stream=SSEStream([sse]))
    return httpx.Response(200, json={"id": "msg_1", "content": [
        {"type": "text", "text": "hi from claude"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3}})


def _openai(body):
    if body.get("stream"):
        c = lambda o: "data: " + json.dumps(o) + "\n\n"
        sse = (c({"choices": [{"index": 0, "delta": {"content": "yo "}}]})
               + c({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                             "total_tokens": 7}}) + "data: [DONE]\n\n")
        return httpx.Response(200, stream=SSEStream([sse]))
    return httpx.Response(200, json={
        "id": "cmpl_1", "object": "chat.completion", "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})


def make_settings(tmp_path) -> Settings:
    return Settings(listen="127.0.0.1:8000", master_keys=["sk-test"], strategy="priority", cooldown=30, db_path=str(tmp_path / "usage.sqlite3"),
                    currency="USD", pricing={"demo": {"prompt": 2.0, "completion": 8.0}},
                    providers=[
                        ProviderSpec(name="primary", base_url="http://mock/v1",
                                     keys=["bad-key", "good-key-AAA"],
                                     models={"demo": "demo-upstream"}, priority=10,
                                     embeddings={"demo-embed": "embed-upstream"}),
                        ProviderSpec(name="backup", base_url="http://mock/v1",
                                     keys=["good-key-BBB"], models={"demo": "demo-bak"}),
                        ProviderSpec(name="claude", base_url="http://mock", style="anthropic",
                                     keys=["ak-test"], models={"claude-sonnet": "claude-x"}),
                        ProviderSpec(name="off", base_url="http://mock/v1", keys=["k"],
                                     models={"ghost": "ghost"}, enabled=False),
                        ProviderSpec(name="keyless", base_url="http://mock/v1", keys=[],
                                     models={"empty-model": "empty"}),
                    ])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    CALLS.clear()
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


H = {"Authorization": "Bearer sk-test"}


def test_auth_required(client):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers=H).status_code == 200
    assert client.get("/healthz").json()["ok"] is True


def test_model_list_exposes_aliases_only(client):
    ids = [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]
    assert ids == ["claude-sonnet", "demo", "empty-model", "demo-embed"]
    assert "ghost" not in ids and "demo-upstream" not in ids


def test_chat_maps_alias_and_keeps_public_model(client):
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["model"] == "demo"
    assert any(u.endswith("/v1/chat/completions") for u in CALLS)


def test_failover_and_key_cooldown(client, monkeypatch):
    # Random per-round start can otherwise pick good-key-AAA first, so the
    # request succeeds without ever touching bad-key. Pin the start so the
    # deterministic failover (bad->good) the test asserts is exercised.
    from SMSocket import router as _router
    monkeypatch.setattr(_router.random, "randrange", lambda n: 0)
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    slots = client.get("/pool").json()["slots"]
    bad = next(s for s in slots if s["provider"] == "primary" and s["fails"] >= 1)
    assert bad["fails"] >= 1 and bad["cooldown_left"] > 0
    assert client.get("/pool").json()["strategy"] == "priority"


def _chat_json(model, content):
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}


def _key(request):
    return (request.headers.get("authorization", "").replace("Bearer ", "")
            or request.headers.get("x-api-key", ""))


def test_429_cools_only_the_failed_model(tmp_path, monkeypatch):
    """Per-model cooldown: a 429 on model `alpha` must cool only `alpha`.
    The sibling model `beta` on the *same* key/provider must still be served.

    Both models live on `multi`'s single `shared-key`; `fb` only offers `alpha`.
    With old (whole-key) cooldown, `beta` would find `shared-key` already cold
    and have no fallback -> 503. Now `beta` goes straight through -> 200."""
    settings = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                        strategy="priority", cooldown=30,
                        db_path=str(tmp_path / "usage.sqlite3"), pricing={},
                        providers=[
                            ProviderSpec(name="multi", base_url="http://mock/v1",
                                         keys=["shared-key"],
                                         models={"alpha": "alpha-up",
                                                 "beta": "beta-up"}, priority=10),
                            ProviderSpec(name="fb", base_url="http://mock/v1",
                                         keys=["fb-key"], models={"alpha": "alpha-up"},
                                         priority=0),
                        ])

    def handler(request):
        body = json.loads(request.content)
        key = _key(request)
        if body.get("model") == "alpha-up" and key == "shared-key":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if body.get("model") == "beta-up" and key == "shared-key":
            return httpx.Response(200, json=_chat_json("beta-up", "beta-ok"))
        # anything else (fb) -> 200
        return httpx.Response(200, json=_chat_json("alpha-up", "alpha-via-fb"))

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(
                            *a, **{**kw, "transport": httpx.MockTransport(handler)}))
    with TestClient(create_app(settings)) as c:
        H = {"Authorization": "Bearer sk-test"}
        # alpha: 429 on shared-key -> failover to fb (per-model cool, not whole-key)
        r = c.post("/v1/chat/completions", headers=H,
                   json={"model": "alpha", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "alpha-via-fb"
        # beta: same shared-key must STILL serve it (alpha's 429 did not cold it)
        r2 = c.post("/v1/chat/completions", headers=H,
                    json={"model": "beta", "messages": [{"role": "user", "content": "hi"}]})
        assert r2.status_code == 200, r2.text
        assert r2.json()["choices"][0]["message"]["content"] == "beta-ok"


def test_anthropic_bridge(client):
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "claude-sonnet",
        "messages": [{"role": "system", "content": "be nice"},
                     {"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    b = r.json()
    assert b["object"] == "chat.completion"
    assert b["choices"][0]["message"]["content"] == "hi from claude"
    assert b["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert any(u.endswith("/v1/messages") for u in CALLS)


def test_anthropic_messages_surface(client):
    """/v1/messages answers with an Anthropic message object, x-api-key auth."""
    r = client.post("/v1/messages", headers={"x-api-key": "sk-test"}, json={
        "model": "claude-sonnet", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["type"] == "message" and b["role"] == "assistant"
    assert b["model"] == "claude-sonnet"
    assert b["content"] == [{"type": "text", "text": "hi from claude"}]
    assert b["stop_reason"] == "end_turn"
    assert b["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert any(u.endswith("/v1/messages") for u in CALLS)


def test_anthropic_messages_surface_translates_openai_upstream(client):
    """An anthropic client talking to an openai-style provider gets anthropic out."""
    r = client.post("/v1/messages", headers={"x-api-key": "sk-test"}, json={
        "model": "demo", "system": "be nice", "max_tokens": 64,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["type"] == "message" and b["model"] == "demo"
    assert b["content"][0]["type"] == "text" and b["content"][0]["text"] == "hello"
    assert any(u.endswith("/v1/chat/completions") for u in CALLS)


def test_anthropic_messages_stream(client):
    r = client.post("/v1/messages", headers={"x-api-key": "sk-test"}, json={
        "model": "demo", "max_tokens": 64, "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.text.startswith("event: message_start")
    types = [l.split(":", 1)[1].strip() for l in r.text.splitlines()
             if l.startswith("event:")]
    assert types == ["message_start", "content_block_start", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]


def test_anthropic_messages_unknown_model(client):
    r = client.post("/v1/messages", headers={"x-api-key": "sk-test"}, json={
        "model": "nope", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404


def test_anthropic_messages_requires_model(client):
    r = client.post("/v1/messages", headers={"x-api-key": "sk-test"}, json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400


def test_streaming(client):
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "demo", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and "data:" in r.text and "[DONE]" in r.text


def test_unknown_model_and_no_upstream(client):
    post = lambda m: client.post("/v1/chat/completions", headers=H,
                                 json={"model": m, "messages": []})
    assert post("nope").status_code == 404          # unknown alias
    assert post("ghost").status_code == 404         # hidden: provider disabled
    assert post("empty-model").status_code == 200   # known alias, keyless slot serves it


def test_stats_and_dashboard(client):
    client.post("/v1/chat/completions", headers=H, json={
        "model": "demo", "messages": [{"role": "user", "content": "hi"}]})
    s = client.get("/stats").json()
    assert s["calls"] >= 1 and s["tokens"] >= 7
    assert any(p["provider"] for p in s["by_provider"])
    assert "SMSocket" in client.get("/").text


def test_reload_and_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-live-1")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "master_keys:\n  - ${MY_KEY}\nproviders:\n"
        "  - name: p\n    base_url: http://mock/v1\n    keys:\n"
        "      - ${MY_KEY}\n      - ${MISSING}\n"
        "    models:\n      m: m\n", encoding="utf-8")
    st = load_config(cfg)
    assert st.master_keys == ["sk-live-1"]
    assert [p.keys for p in st.providers][0] == ["sk-live-1"]
    assert _env("$MY_KEY") == "sk-live-1"


def test_embeddings_route_and_account(client):
    r = client.post("/v1/embeddings", headers=H,
                    json={"model": "demo-embed", "input": ["x", "y"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["model"] == "demo-embed"
    assert [d["object"] for d in j["data"]] == ["embedding", "embedding"]
    assert j["usage"]["total_tokens"] == 3
    assert any(u.endswith("/v1/embeddings") for u in CALLS), CALLS


def test_embeddings_rejects_chat_alias(client):
    r = client.post("/v1/embeddings", headers=H, json={"model": "demo", "input": "x"})
    assert r.status_code in (404, 503), r.text


def test_stream_events_are_normalized(client):
    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "demo", "stream": True,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    events = [e.split("data: ", 1)[1] for e in r.text.split("\n\n") if "data: " in e]
    payloads = [json.loads(e) for e in events if e.strip() != "[DONE]"]
    assert payloads, r.text
    for p in payloads:
        assert p["object"] == "chat.completion.chunk", p
        assert p["model"] == "demo", p


def test_usage_endpoint_reports_cost(client):
    body = {"model": "demo", "messages": [{"role": "user", "content": "x"}]}
    for _ in range(2):
        client.post("/v1/chat/completions", headers=H, json=body)
    u = client.get("/v1/usage", headers=H).json()
    assert u["currency"] == "USD"
    assert u["totals"]["calls"] >= 1
    assert u["totals"]["cost"] > 0, u
    assert u["daily"] and u["daily"][0]["tokens"] > 0
    demo = [m for m in u["by_model"] if m["alias"] == "demo"]
    assert demo and demo[0]["cost"] > 0, u["by_model"]


def test_reload_guard_and_key_health(client, tmp_path, monkeypatch):
    # Pin the per-round start so bad-key is tried first, cooling it - the
    # later assertions check that key health survives a config reload.
    from SMSocket import router as _router
    monkeypatch.setattr(_router.random, "randrange", lambda n: 0)
    body = {"model": "demo", "messages": [{"role": "user", "content": "x"}]}
    client.post("/v1/chat/completions", headers=H, json=body)
    all_before = client.get("/pool").json()["slots"]
    before = [x for x in all_before if x["provider"] == "primary"]
    assert any(x["fails"] for x in before), before

    # 1. no config file on disk -> reload refused, live state untouched
    #    pin the path explicitly: a real config.yaml in the repo cwd must not
    #    make this assertion depend on where pytest was launched from
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(tmp_path / "absent.yaml"))
    assert client.post("/admin/reload", headers=H).status_code == 409
    mid = [x for x in client.get("/pool").json()["slots"] if x["provider"] == "primary"]
    assert [x["fails"] for x in mid] == [x["fails"] for x in before], mid

    # 2. valid config -> applied, but observed key health survives the swap
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "master_keys:\n  - sk-test\nproviders:\n"
        "  - name: primary\n    base_url: http://mock/v1\n"
        "    keys:\n      - bad-key\n      - good-key-AAA\n"
        "    models:\n      demo: demo-upstream\n", encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg))
    r = client.post("/admin/reload", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["providers"] == 1
    after = client.get("/pool").json()["slots"]
    assert [x["fails"] for x in after] == [x["fails"] for x in before], (before, after)
    assert any(x["cooldown_left"] > 0 for x in after), after


# ---- Anthropic <-> chat translation units (tool shapes) -------------------
def test_anthropic_tools_translate_to_openai_functions():
    from SMSocket.upstream import anthropic_request_to_chat
    p = anthropic_request_to_chat({
        "model": "demo", "max_tokens": 8,
        "tools": [{"name": "get_weather", "description": "d",
                   "input_schema": {"type": "object", "properties": {"city": {}}}}],
        "tool_choice": {"type": "tool", "name": "get_weather"},
        "messages": [{"role": "user", "content": "weather?"}]})
    assert p["tools"] == [{"type": "function", "function": {
        "name": "get_weather", "description": "d",
        "parameters": {"type": "object", "properties": {"city": {}}}}}]
    assert p["tool_choice"] == {"type": "function",
                                "function": {"name": "get_weather"}}


def test_anthropic_tool_use_and_result_round_trip():
    from SMSocket.upstream import anthropic_request_to_chat, chat_to_anthropic
    p = anthropic_request_to_chat({
        "model": "demo", "max_tokens": 8,
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        ]})
    assert p["messages"][0]["tool_calls"] == [
        {"id": "toolu_1", "type": "function",
         "function": {"name": "f", "arguments": '{"a": 1}'}}]
    assert p["messages"][1] == {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"}
    out = chat_to_anthropic({
        "id": "chatcmpl-1", "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "f",
                                                          "arguments": '{"a": 2}'}}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2}}, "demo")
    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [{"type": "tool_use", "id": "call_1", "name": "f",
                               "input": {"a": 2}}]
    assert out["usage"] == {"input_tokens": 1, "output_tokens": 2}


def test_anthropic_system_block_list_becomes_a_system_message():
    from SMSocket.upstream import anthropic_request_to_chat
    p = anthropic_request_to_chat({
        "model": "demo", "max_tokens": 8, "system": [
            {"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        "messages": [{"role": "user", "content": "hi"}]})
    assert p["messages"][0] == {"role": "system", "content": "one\ntwo"}


def test_anthropic_thinking_blocks_are_dropped():
    from SMSocket.upstream import anthropic_request_to_chat
    p = anthropic_request_to_chat({
        "model": "demo", "max_tokens": 8,
        "messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "***"},
            {"type": "redacted_thinking", "data": "x"},
            {"type": "text", "text": "hi"}]}]})
    assert p["messages"][0]["content"] == "hi"
