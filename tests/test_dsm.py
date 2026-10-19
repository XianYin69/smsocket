"""DSM v1 server-side tests — contract §6 acceptance matrix A..H plus the
materialise/downgrade edge cases the matrix implies (415/422/409/413).

No network: the upstream is an httpx.MockTransport, exactly like test_smoke.py.
"""
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket import dsm
from SMSocket.config import dsm_from_raw, load_config, ProviderSpec, Settings
from SMSocket.gateway import create_app

CT = {"Content-Type": dsm.CTYPE}
SENT: list[dict] = []          # bodies the gateway actually pushed upstream
MOCK: dict[str, str] = {"mode": ""}   # 测试侧开关："" = 纯文本流，"tool_stream" = 带思考与工具调用
H = {"Authorization": "Bearer sk-test"}

TOOLS = [{"type": "function", "function": {"name": "exec",
           "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}}]


class SSEStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p.encode()


def _reply(body, mock=""):
    usage = {"prompt_tokens": 467, "completion_tokens": 9815, "total_tokens": 10282,
             "completion_tokens_details": {"reasoning_tokens": 9412},
             "prompt_tokens_details": {"cached_tokens": 120}}
    if body.get("stream"):
        c = lambda o: "data: " + json.dumps(o) + "\n\n"
        if mock == "tool_stream":
            # 真提供商形状：思考分片 → tool_calls 分片（id/name/arguments 拆开）→ usage
            return httpx.Response(200, stream=SSEStream([
                c({"choices": [{"index": 0, "delta": {"reasoning_content": "想想"}}]}),
                c({"choices": [{"index": 0, "delta": {"content": "先看"}}]}),
                c({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_A", "function": {"name": "exec", "arguments": ""}}]}}]}),
                c({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "{\"cmd\":\"ls\"}"}}]}}]}),
                c({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": usage}),
                "data: [DONE]\n\n"]))
        return httpx.Response(200, stream=SSEStream([
            c({"choices": [{"index": 0, "delta": {"content": "42"}}]}),
            c({"choices": [], "usage": usage}), "data: [DONE]\n\n"]))
    return httpx.Response(200, json={
        "id": "cmpl_dsm", "object": "chat.completion", "model": body.get("model", "demo"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "42"}}], "usage": usage})


def handler(request: httpx.Request) -> httpx.Response:
    b = json.loads(request.content)
    SENT.append(b)
    return _reply(b, MOCK["mode"])


def make_settings(tmp_path, **dsmkw) -> Settings:
    s = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                 db_path=str(tmp_path / "usage.sqlite3"), currency="USD",
                 pricing={"demo": {"prompt": 2.0, "completion": 8.0, "cache_read": 0.2}},
                 providers=[ProviderSpec(name="primary", base_url="http://mock/v1",
                                         keys=["good-key-AAA"], models={"demo": "demo-up"})])
    s.dsm = dsm_from_raw(dsmkw or None)
    return s


@pytest.fixture()
def app(tmp_path, monkeypatch):
    SENT.clear()
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    with TestClient(create_app(make_settings(tmp_path, enabled=True))) as c:
        yield c


def envelope(system="SYS TEXT", tools=None, turns=None, **over):
    """Client-shaped envelope (build_env lives on the client — the server receives)."""
    store = dsm.schemas()
    ref = store.put(system, TOOLS if tools is None else tools)
    env = {"v": 1, "sch": ref, "sid": "sess-1", "cid": "conv-1", "lane": "t1",
           "d": [[1, "hello"]] if turns is None else turns, "out": "json",
           "x": {"sms.turn_json": 1, "sms.model": "demo", "sms.delta_from": 0}}
    env.update(over)
    return env


def post_chat(client, env):
    return client.post("/v1/dsm/chat", headers={**H, **CT}, content=json.dumps(env))


# ---- A: switches off == today's behaviour, byte-for-byte --------------------------
def test_A_disabled_leaves_legacy_path_untouched(tmp_path, monkeypatch):
    SENT.clear()
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=transport, **k))
    with TestClient(create_app(make_settings(tmp_path))) as c:          # dsm off
        r = c.post("/v1/chat/completions", headers=H,
                   json={"model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert SENT[0] == {"model": "demo-up",
                           "messages": [{"role": "user", "content": "hi"}]}
        assert c.post("/v1/dsm/chat", headers={**H, **CT},
                      content=json.dumps(envelope())).status_code == 404
        assert c.get("/healthz").json()["dsm"]["enabled"] is False


# ---- B: envelope accepted, decodes to OpenAI shape, tool loop unchanged ----------
def test_B_envelope_roundtrip_and_tool_calls(app):
    r = post_chat(app, envelope())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(dsm.CTYPE)
    body = r.json()
    assert body["answer"] == "42" and body["stop"] == "end_turn"
    assert body["sid"] == "sess-1" and body["lane"] == "t1"
    u = body["usage"]
    assert (u["in"], u["out_reason"], u["out_answer"], u["cache_read"]) == (467, 9412, 403, 120)
    assert u["out_reason"] + u["out_answer"] == 9815 and u["in"] + 9815 == u["total"]
    back = dsm.decode_to_openai_shape(body)
    assert back["choices"][0]["message"]["content"] == "42"
    assert back["usage"]["prompt_tokens"] == 467 and back["usage"]["completion_tokens"] == 9815
    assert back["usage"]["prompt_tokens_details"]["cached_tokens"] == 120
    assert back["usage"]["completion_tokens_details"]["reasoning_tokens"] == 9412
    sent = SENT[-1]
    assert sent["messages"][0]["content"].startswith("SYS TEXT")     # materialised, not sent
    assert sent["tools"][0]["function"]["name"] == "exec"
    assert sent["model"] == "demo-up"
    tc = json.dumps({"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "exec", "arguments": '{"cmd":"ls"}'}}]},
                    sort_keys=True)
    r2 = post_chat(app, envelope(turns=[[2, tc]]))
    assert r2.status_code == 200, r2.text
    sent2 = SENT[-1]["messages"][-1]
    assert sent2["tool_calls"][0]["id"] == "c1"
    assert json.loads(sent2["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls"}


# ---- C: openai_compat=false closes legacy loudly, DSM still fine ------------------
def test_C_legacy_410_when_compat_off(app):
    app.app.state.llm.settings.dsm.openai_compat = False
    r = app.post("/v1/chat/completions", headers=H, json={"model": "demo", "messages": []})
    assert r.status_code == 410
    assert "openai_compat" in r.json()["detail"]["error"]["message"]
    assert app.post("/v1/responses", headers=H,
                    json={"model": "demo", "input": []}).status_code == 410
    assert post_chat(app, envelope()).status_code == 200


# ---- D: FF refused at load (no self-brick) ----------------------------------------
def test_D_ff_refused_at_config_load(tmp_path):
    with pytest.raises(ValueError):
        dsm_from_raw({"enabled": False, "openai_compat": False})
    p = tmp_path / "c.yaml"
    p.write_text("dsm:\n  enabled: false\n  openai_compat: false\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))


# ---- E: fan split honours dep -----------------------------------------------------
def test_E_fan_dep_gates_dispatch(app):
    fan = {"n": 3, "lane_ids": ["t4a", "t4b", "t4c"], "merge": "vote", "concurrency": 3}
    env = envelope(fan=fan, dep=["t1"])
    assert app.post("/v1/dsm/fan?run=1", headers={**H, **CT},
                    content=json.dumps(env)).status_code == 409
    dry = app.post("/v1/dsm/fan", headers={**H, **CT}, content=json.dumps(env)).json()
    assert dry["dispatchable"] is False and dry["ready"] == [] and dry["merge"] == "vote"
    assert len(dry["requests_materialised"]) == 3
    assert dry["requests"][0]["lane"] == "t4a" and "fan" not in dry["requests"][0]
    done = envelope(fan=fan, dep=["t1"],
                    x={"sms.model": "demo", "sms.delta_from": 0, "sms.done_lanes": ["t1"]})
    ok = app.post("/v1/dsm/fan", headers={**H, **CT}, content=json.dumps(done)).json()
    assert ok["dispatchable"] is True and ok["ready"] == ["t4a", "t4b", "t4c"]
    assert ok["concurrency"] == 3


# ---- F: usage columns + cache evidence (defects #1/#2/#3 closed) ------------------
def test_F_usage_three_way_accounting(app):
    post_chat(app, envelope(bill={"to": "shuli", "row": "t4"}))
    row = app.app.state.llm.usage.recent(1)[0]
    assert (row["sid"], row["cid"], row["lane"], row["skill"]) == ("sess-1", "conv-1", "t1", "shuli")
    assert (row["in"], row["out_reason"], row["out_answer"], row["cache_read"]) == (467, 9412, 403, 120)
    full = app.app.state.llm.settings.cost_detail("demo", 467, 9815)["amount"]
    assert row["cost"] < full                       # cache discount actually priced
    s = app.app.state.llm.usage.dsm_summary()
    assert s["tok_cache"] == 120 and s["cache_hits"] == 1 and s["dsm_calls"] == 1
    lane = [x for x in app.app.state.llm.usage.by_lane() if x["lane"] == "t1"]
    assert lane and lane[0]["tok_reason"] == 9412 and lane[0]["skill"] == "shuli"


# ---- G: invariants ----------------------------------------------------------------
def test_G_invariants(app):
    env = envelope()
    body = dsm.materialize(env, alias="demo", style="openai-chat", use_session=False)
    assert dsm.leak_check(env, body) == []
    assert dsm.ROLE[dsm.RID["constraint"]] == "constraint"
    assert [dsm.RID[n] for n in dsm.ROLE] == list(range(len(dsm.ROLE)))
    ext = envelope(x={"sms.model": "demo", "sms.delta_from": 0, "mydomain.flag": True})
    assert dsm.validate(ext) == [] and ext["x"]["mydomain.flag"] is True
    bad = dict(env); bad["rogue"] = 1
    assert any("rogue" in e for e in dsm.validate(bad))
    before = dsm.mem_unsupported()
    dsm.materialize(envelope(mem=["abcdef0123", "1234567890"]), alias="demo", use_session=False)
    assert dsm.mem_unsupported() > before           # unresolvable, never silent


# ---- H: downgrade signals (404 / 415) ---------------------------------------------
def test_H_downgrade_signals(app):
    assert post_chat(app, envelope()).status_code == 200
    r = app.post("/v1/dsm/chat", headers={**H, "Content-Type": "application/json"},
                 content=json.dumps(envelope()))
    assert r.status_code == 415
    app.app.state.llm.settings.dsm.enabled = False
    r2 = post_chat(app, envelope())
    assert r2.status_code == 404
    assert r2.json()["detail"]["error"]["type"] == "dsm_disabled"
    app.app.state.llm.settings.dsm.enabled = True
    assert post_chat(app, envelope()).status_code == 200


# ---- schema registration ----------------------------------------------------------
def test_schema_register_idempotent_and_mismatch(app):
    ref = dsm.fingerprint("REG SYS", [])
    r = app.post("/v1/dsm/schema", headers={**H, **CT},
                 content=json.dumps({"ref": ref, "system": "REG SYS", "tools": []}))
    assert r.status_code == 200 and r.json()["ref"] == ref
    assert app.post("/v1/dsm/schema", headers={**H, **CT},
                    content=json.dumps({"ref": ref, "system": "OTHER", "tools": []})).status_code == 422
    g = app.get("/v1/dsm/schema", headers=H, params={"ref": ref})
    assert g.status_code == 200 and g.json()["system"] == "REG SYS"
    assert app.get("/v1/dsm/schema", headers=H,
                   params={"ref": "sha1:deadbeefdead"}).status_code == 404


def test_bad_envelope_422(app):
    assert post_chat(app, {"v": 2, "sch": "sha1:000000000000", "d": []}).status_code == 422
    assert post_chat(app, envelope(d=[[99, "x"]])).status_code == 422
    assert post_chat(app, envelope(sch="not-a-ref")).status_code == 422


def test_delta_accumulates_across_calls(app):
    post_chat(app, envelope(turns=[[1, "first"]]))
    post_chat(app, envelope(turns=[[1, "second"]],
                            x={"sms.model": "demo", "sms.delta_from": 1}))
    user = [m["content"] for m in SENT[-1]["messages"] if m["role"] == "user"]
    assert user == ["first", "second"]


def test_state_mismatch_409(app):
    assert post_chat(app, envelope(turns=[[1, "x"]],
                                  x={"sms.model": "demo", "sms.delta_from": 7})).status_code == 409


def test_out_delta_streams_ndjson(app):
    r = post_chat(app, envelope(out="delta"))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(dsm.CTYPE_STREAM)
    rows = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert [x["seq"] for x in rows] == sorted(x["seq"] for x in rows)
    back = dsm.decode_to_openai_shape(rows)
    assert back["choices"][0]["message"]["content"] == "42"
    assert back["usage"]["completion_tokens"] == 9815


def test_healthz_reports_dsm_state(app):
    d = app.get("/healthz").json()["dsm"]
    assert d["enabled"] is True and d["openai_compat"] is True
    assert "budget_map" in d
    fresh = d["schemas"]
    post_chat(app, envelope())                       # one materialised schema
    after = app.get("/healthz").json()["dsm"]
    assert after["schemas"] > fresh                  # state() really tracks the store
    assert after["sessions"] >= 1


def test_budget_tier_maps_to_provider_field(app):
    post_chat(app, envelope(budget={"reason": "low"}))
    assert SENT[-1]["reasoning_effort"] == "low"
    assert "max_tokens" not in SENT[-1]          # budget must not cut the answer


def test_materialise_size_cap(app, tmp_path):
    app.app.state.llm.settings.dsm.max_materialize_bytes = 1024
    big = envelope(system="HUGE " * 2000)
    assert post_chat(app, big).status_code == 413


# ---------- out=delta 必须把 reasoning 与 tool_calls 一起带出（尾巴3 前置） ----------
def test_stream_resumes_after_provider_drop(tmp_path, monkeypatch):
    """超长任务：提供商流式传输中途断开（httpx.ReadError）时，网关必须续接而非断流。

    旧行为：`wrap_stream` 里 aiter_raw 抛 ReadError 直接穿透 → StreamingResponse 中断，
    客户端拿到半截正文并把任务当作已收口（任务停止）。新行为：断流后把已吐正文作为
    局部 assistant 消息续接重发（同槽优先、全池 failover），客户端最终收到两段拼出的全文；
    续接请求必须带上完整历史，否则上下文只剩半截。"""
    c = lambda o: "data: " + json.dumps(o) + "\n\n"
    sent = []
    n = {"calls": 0}

    class DropStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield c({"choices": [{"index": 0, "delta": {"content": "先看"}}]}).encode()
            raise httpx.ReadError("provider closed connection mid-stream")

    def h(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(200, stream=DropStream())
        return httpx.Response(200, stream=SSEStream([
            c({"choices": [{"index": 0, "delta": {"content": "42"}}]}),
            "data: [DONE]\n\n"]))

    real, transport = httpx.AsyncClient, httpx.MockTransport(h)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=transport, **k))
    with TestClient(create_app(make_settings(tmp_path, enabled=True))) as app:
        r = app.post("/v1/dsm/chat", headers={**H, **CT},
                     content=json.dumps(envelope(out="delta")))
    assert r.status_code == 200, r.text
    frames = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    text = "".join(f.get("answer") or "" for f in frames if isinstance(f.get("answer"), str))
    assert text == "先看42", ("上游中断后续接失败，正文断裂", frames)
    assert n["calls"] == 2, ("没有发生续接重发", n)
    resume = sent[-1]
    assert any(m.get("role") == "user" for m in resume["messages"]), \
        ("续接请求丢失原始上下文", resume["messages"])
    partial = [m for m in resume["messages"] if m.get("role") == "assistant"
               and m.get("content") == "先看"]
    assert partial, ("续接请求丢失已吐正文", resume["messages"])


def test_out_delta_carries_reasoning_and_tool_calls(app):
    """旧实现只转 content 增量：思考整段消失、finish_reason 一律写 stop、tool_calls
    直接被吞——一旦把 dsm.out 切成 delta，SMS 的工具循环会当场断掉。
    本用例锁死三类增量都在 ndjson 里，且收口帧把 stop 如实写成 tool_call。"""
    MOCK["mode"] = "tool_stream"
    try:
        r = post_chat(app, envelope(out="delta"))
    finally:
        MOCK["mode"] = ""
    assert r.status_code == 200, r.text
    frames = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert all(f.get("v") == 1 for f in frames)
    text = "".join(f.get("answer") or "" for f in frames if isinstance(f.get("answer"), str))
    reason = "".join(((f.get("reason") or {}).get("summary") or "") for f in frames)
    assert "先看" in text, ("正文增量丢失", frames)
    assert reason == "想想", ("reasoning 增量丢失——◌ 信封会整条消失", frames)
    final = frames[-1]
    assert final["stop"] == "tool_call", ("收口帧未如实带出停止原因", final)
    tcs = final["answer"]
    assert isinstance(tcs, list) and tcs, ("tool_calls 未进收口帧", final)
    assert tcs[0]["function"]["name"] == "exec"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"cmd": "ls"}
    assert tcs[0]["id"] == "call_A"
    assert final["usage"]["out_reason"] == 9412      # 三分账在 delta 模式同样成立


# ---------- smsocket 别名：DSM 路由与 legacy 路由同等待遇（自动路由） ----------
def test_smsocket_alias_json_auto_routes(app):
    """legacy /v1/chat/completions 一直支持 model='smsocket' 自动路由，
    DSM 信封路由原先缺同款豁免：sms-core（dsm.enabled=true）发 'smsocket'
    直接 404 unknown model，且客户端把 404 误判为「服务端无 DSM」进入 120s 冷却。"""
    r = post_chat(app, envelope(x={"sms.model": "smsocket"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"        # 路由解析到真实模型
    assert r.json()["model"] == "demo"           # 归因/计费用解析后的别名，而非 smsocket


def test_smsocket_alias_delta_auto_routes(app):
    r = post_chat(app, envelope(out="delta", x={"sms.model": "smsocket"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"
    frames = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert frames[-1]["model"] == "demo"


def test_auto_alias_json_auto_routes(app):
    """auto 别名也应像 smsocket 一样触发自动路由（兼容 sms-core 默认 model=auto）。"""
    r = post_chat(app, envelope(x={"sms.model": "auto"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"
    assert r.json()["model"] == "demo"


def test_auto_alias_delta_auto_routes(app):
    r = post_chat(app, envelope(out="delta", x={"sms.model": "auto"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"
    frames = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert frames[-1]["model"] == "demo"


def test_smsocket_auto_alias_json_auto_routes(app):
    """smsocket/auto 别名也应触发自动路由（兼容 sms-core 显式指定）。"""
    r = post_chat(app, envelope(x={"sms.model": "smsocket/auto"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"
    assert r.json()["model"] == "demo"


def test_smsocket_auto_alias_delta_auto_routes(app):
    r = post_chat(app, envelope(out="delta", x={"sms.model": "smsocket/auto"}))
    assert r.status_code == 200, r.text
    assert SENT[-1]["model"] == "demo-up"
    frames = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert frames[-1]["model"] == "demo"
