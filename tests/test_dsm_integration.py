"""Cross-repo integration (task-20261005-230931-477 t4): the REAL sms-core codec talks
to the REAL SMSocket app. Only the upstream HTTP transport is mocked — exactly like
production, where the client never sees the upstream.

Why this file exists at all: test_dsm.py (server) hand-builds envelopes and
test_dsm_core.py (client) stubs the transport, so each side only ever argues with its
own idea of the other. A *contract* drift (key names, watermark semantics, response
shape) passes both suites and breaks nothing until real traffic hits. Here a
client-built envelope is registered, stored, materialised and answered by the server,
then decoded by the client's own decoder — no hand-written envelopes.

Skips cleanly when sms-core is not installed (SMS_CORE_SCRIPTS overrides the path).
"""
import importlib.util
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket import dsm as S
from SMSocket.config import ProviderSpec, Settings, dsm_from_raw, load_config
from SMSocket.gateway import create_app

SMS_SCRIPTS = os.environ.get("SMS_CORE_SCRIPTS") or os.path.join(
    os.path.expanduser("~"), ".kilocode", "skills", "skill_manage_system",
    "skill", "scripts")
CDM = (os.environ.get("SMS_CORE_DSM") or os.path.join(SMS_SCRIPTS, "dsm.py"))


def _load_client():
    """sms-core's codec as a second live module object (SMSocket.dsm stays the server's)."""
    if not os.path.exists(CDM):
        return None
    sys.path.insert(0, SMS_SCRIPTS)
    try:
        spec = importlib.util.spec_from_file_location("sms_core_dsm", CDM)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["sms_core_dsm"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
    finally:
        try:
            sys.path.remove(SMS_SCRIPTS)
        except ValueError:
            pass


C = _load_client()
needs_client = pytest.mark.skipif(C is None, reason="sms-core codec absent: " + CDM)


H = {"Authorization": "Bearer sk-test"}
CT = {"Content-Type": "application/dsm+json"}
TOOLS = [{"type": "function", "function": {"name": "exec",
           "parameters": {"type": "object",
                          "properties": {"cmd": {"type": "string"}}}}}]
SENT: list[dict] = []          # bodies the gateway actually pushed upstream
ATTEMPTS = [0]                 # DSM HTTP round trips the client actually paid for
USAGE = {"prompt_tokens": 467, "completion_tokens": 9815, "total_tokens": 10282,
         "completion_tokens_details": {"reasoning_tokens": 9412},
         "prompt_tokens_details": {"cached_tokens": 120}}
SYS_MSGS = [{"role": "system", "content": "SYS TEXT"},
            {"role": "user", "content": "hi"}]


class SSEStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p.encode()


def _reply(body: dict) -> httpx.Response:
    if body.get("stream"):
        c = lambda o: "data: " + json.dumps(o) + "\n\n"
        return httpx.Response(200, stream=SSEStream([
            c({"choices": [{"index": 0, "delta": {"content": "42"}}]}),
            c({"choices": [], "usage": USAGE}), "data: [DONE]\n\n"]))
    if "USE_TOOL" in json.dumps(body.get("messages", []), ensure_ascii=False):
        return httpx.Response(200, json={
            "id": "cmpl_tool", "object": "chat.completion", "model": "demo-up",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": "", "tool_calls": [
                             {"id": "srv_tc_1", "type": "function",
                              "function": {"name": "grep",
                                           "arguments": '{"pattern":"dsm"}'}}]}}],
            "usage": USAGE})
    return httpx.Response(200, json={
        "id": "cmpl_dsm", "object": "chat.completion", "model": "demo-up",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "42"}}],
        "usage": USAGE})


def handler(request: httpx.Request) -> httpx.Response:
    SENT.append(json.loads(request.content))
    return _reply(SENT[-1])


def _settings(tmp_path, **dsmkw) -> Settings:
    s = Settings(listen="127.0.0.1:0", master_keys=["sk-test"],
                 db_path=str(tmp_path / "usage.sqlite3"), currency="USD",
                 pricing={"demo": {"prompt": 2.0, "completion": 8.0, "cache_read": 0.2}},
                 providers=[ProviderSpec(name="primary", base_url="http://mock/v1",
                                         keys=["good-key-AAA"],
                                         models={"demo": "demo-up"})])
    s.dsm = dsm_from_raw(dsmkw or None)
    return s


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Hermetic SMS_HOME — the client codec must never touch the real runtime state."""
    h = tmp_path / "sms_home"
    (h / "runtime").mkdir(parents=True)
    monkeypatch.setenv("SMS_HOME", str(h))
    if C is not None:
        monkeypatch.setattr(C, "session_ids", lambda conv=None: ("sess-int", "conv-int"))
        monkeypatch.setattr(C, "current_lane", lambda conv=None: "t1")
        monkeypatch.setattr(C, "mem_ids", lambda *a, **k: [])
        C.clear_broken()
    SENT.clear()
    ATTEMPTS[0] = 0
    yield str(h)
    if C is not None:
        C.clear_broken()


def _live(tmp_path, monkeypatch, **dsmkw):
    real, transport = httpx.AsyncClient, httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=transport, **k))
    return TestClient(create_app(_settings(tmp_path, **dsmkw)))


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    with _live(tmp_path, monkeypatch, enabled=True) as c:
        yield c


def _cfg(**kw):
    c = dict(C.DEFAULTS)
    c.update({"enabled": True, "style": "openai-chat"})
    c.update(kw)
    return c


def _store(home):
    return C.SchemaStore(path=os.path.join(home, "runtime", "dsm_schemas.json"))


def _env(home, store, msgs, model="demo", lane="t1", resync=False, fan=None,
         dep=None, x=None, out=None, enable_fan=False, **dsmkw):
    """A genuine sms-core envelope — never hand-written in this file."""
    if enable_fan:
        dsmkw["fan"] = True
    if out:
        dsmkw["out"] = out
    env = C.build_env(msgs, tools=TOOLS, model=model, dsm_cfg=_cfg(**dsmkw), store=store,
                      lane=lane, fan=fan, x=x, sms=home, resync=resync)
    if dep:
        env["dep"] = list(dep)
    return env


def _register(c, home, store, env):
    return c.post("/v1/dsm/schema", headers={**H, **CT},
                  content=json.dumps(C.schema_frame(env, store=store, sms=home)))


def _chat(c, env, accept=None):
    ATTEMPTS[0] += 1
    hdr = dict(H)
    hdr.update(CT)
    if accept:
        hdr["Accept"] = accept
    return c.post("/v1/dsm/chat", headers=hdr, content=json.dumps(env))


# ---- I1: the whole path, both codecs real ----------------------------------------
@needs_client
def test_I1_client_envelope_survives_the_whole_server_roundtrip(home, srv):
    store = _store(home)
    env = _env(home, store, SYS_MSGS)
    assert S.validate(env) == [], "服务端拒收自家编解码器产出的信封"
    assert _register(srv, home, store, env).status_code == 200
    r = _chat(srv, env)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(S.CTYPE)
    resp = r.json()
    assert resp["answer"] == "42" and resp["stop"] == "end_turn"
    assert (resp["sid"], resp["lane"]) == ("sess-int", "t1")
    u = resp["usage"]
    assert (u["in"], u["out_reason"], u["out_answer"], u["cache_read"]) == (467, 9412, 403, 120)
    o = C.decode_to_openai_shape(resp)                 # decoded by the client's own code
    ch = o["choices"][0]
    assert ch["message"]["content"] == "42" and ch["finish_reason"] == "stop"
    uu = o["usage"]
    assert (uu["prompt_tokens"], uu["completion_tokens"], uu["total_tokens"]) == (467, 9815, 10282)
    assert uu["completion_tokens_details"]["reasoning_tokens"] == 9412
    assert uu["prompt_tokens_details"]["cached_tokens"] == 120
    sent = SENT[-1]
    assert sent["messages"][0]["content"].startswith("SYS TEXT")   # sch resolved server-side
    assert sent["tools"][0]["function"]["name"] == "exec"


# ---- I2: tool loop needs zero changes (contract §4) -------------------------------
@needs_client
def test_I2_tool_calls_survive_both_directions(home, srv):
    store = _store(home)
    msgs = [{"role": "system", "content": "SYS TEXT"},
            {"role": "user", "content": "USE_TOOL"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "exec",
                 "arguments": '{"cmd":"ls"}'}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file.txt"}]
    env = _env(home, store, msgs)
    _register(srv, home, store, env)
    r = _chat(srv, env)
    assert r.status_code == 200, r.text
    sent = SENT[-1]["messages"]
    a = [m for m in sent if m.get("role") == "assistant"][-1]
    # EGC 是出网标准（R6）：id 在**线上**被短化，但必须成对一致、且能反查回原 id。
    wid = a["tool_calls"][0]["id"]
    assert wid != "tc1" and len(wid) <= 4, ("出网 id 未被 R6 短化", wid)
    assert json.loads(a["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls"}
    t = [m for m in sent if m.get("role") == "tool"][-1]
    assert t["tool_call_id"] == wid, "assistant.tool_calls.id 与 tool.tool_call_id 不成对＝实测 400"
    assert t["content"] == "file.txt"
    o = C.decode_to_openai_shape(r.json())
    ch = o["choices"][0]
    assert ch["finish_reason"] == "tool_calls", "stop=tool_call 未映射回 tool_calls"
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "grep"


# ---- I3: red line one, measured on the real egress body ---------------------------
@needs_client
def test_I3_policy_keys_never_reach_the_upstream(home, srv):
    store = _store(home)
    env = _env(home, store, SYS_MSGS)
    env["bill"] = {"to": "sms-core", "row": "t1"}
    assert S.validate(env) == []
    _register(srv, home, store, env)
    assert _chat(srv, env).status_code == 200
    body = SENT[-1]
    assert S.leak_check(env, body) == []
    assert C.leak_check(env, body) == []
    assert not (set(body) & (S.POLICY_KEYS - {"out"}))
    for k in ("sch", "mem", "x", "dep"):
        assert k not in body


# ---- I4: the watermark bug class (delta frame must APPEND, not replace) -----------
@needs_client
def test_I4_second_frame_is_a_delta_and_the_server_appends_it(home, srv):
    store = _store(home)
    m1 = [{"role": "system", "content": "SYS TEXT"},
          {"role": "user", "content": "hi"},
          {"role": "assistant", "content": "42"}]
    env1 = _env(home, store, m1)
    assert env1["x"]["sms.delta_from"] == 0 and len(env1["d"]) == 2
    _register(srv, home, store, env1)
    assert _chat(srv, env1).status_code == 200
    C.commit_from_env(env1, m1, sms=home)              # what gateway does on success
    m2 = m1 + [{"role": "user", "content": "again"}]
    env2 = _env(home, store, m2)
    assert len(env2["d"]) == 1, "水位没生效：整段历史又被重发一遍"
    assert env2["x"]["sms.delta_from"] == 2
    assert _chat(srv, env2).status_code == 200
    got = [m["content"] for m in SENT[-1]["messages"]]
    assert got == ["SYS TEXT", "hi", "42", "again"], "增量帧被当全量替换 → 历史静默丢失"


# ---- I5: 409 is recoverable, not a permanent failure (livelock guard) -------------
@needs_client
def test_I5_watermark_mismatch_409_then_resync_recovers(home, srv):
    store = _store(home)
    m = [{"role": "system", "content": "SYS TEXT"},
         {"role": "user", "content": "hi"},
         {"role": "assistant", "content": "42"}]
    env = _env(home, store, m)
    C.commit_from_env(env, m, sms=home)                # client ahead, server empty
    m2 = m + [{"role": "user", "content": "again"}]
    env2 = _env(home, store, m2)
    assert env2["x"]["sms.delta_from"] == 2
    _register(srv, home, store, env2)
    r = _chat(srv, env2)
    assert r.status_code == 409, r.text
    assert "dsm_state_mismatch" in json.dumps(r.json())
    C.reset_delta(env2["x"]["sms.delta_key"], sms=home)
    env3 = _env(home, store, m2, resync=True)
    assert env3["x"]["sms.delta_from"] == 0
    assert _chat(srv, env3).status_code == 200, "重同步未恢复 → 每轮再撞 409 的活锁"
    assert len(SENT[-1]["messages"]) == 4


# ---- I6: TF (dsm-only server) — legacy closed loudly, envelope fine ---------------
@needs_client
def test_I6_dsm_only_server_closes_legacy_loudly(home, tmp_path, monkeypatch):
    store = _store(home)
    env = _env(home, store, SYS_MSGS)
    with _live(tmp_path, monkeypatch, enabled=True, openai_compat=False) as c:
        assert _register(c, home, store, env).status_code == 200
        assert _chat(c, env).status_code == 200
        r = c.post("/v1/chat/completions", headers=H,
                   json={"model": "demo", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 410, "openai_compat=false 时 legacy 仍开门"
        assert "openai_compat" in json.dumps(r.json(), ensure_ascii=False)
        assert c.post("/v1/responses", headers=H,
                      json={"model": "demo", "input": []}).status_code == 410
    assert C.legacy_refused(_cfg(openai_compat=False)), "客户端会静默构造 legacy body"
    assert C.legacy_refused(_cfg(openai_compat=True)) is None


# ---- I7: FT (legacy-only server) — downgrade once, then coast (cooldown latch) ----
@needs_client
def test_I7_legacy_only_server_downgrades_once_then_coasts(home, tmp_path, monkeypatch):
    store = _store(home)
    env = _env(home, store, SYS_MSGS)

    def call(c):
        """Mirror of gateway._dsm_chat's decision table (404/415 → legacy + latch)."""
        if C.broken():
            return "legacy-cached"
        r = _chat(c, env)
        if r.status_code in (404, 415):
            C.mark_broken("dsm chat %d" % r.status_code)
            return "legacy"
        return "dsm"

    with _live(tmp_path, monkeypatch, enabled=False) as c:
        turns = [call(c) for _ in range(6)]
    assert turns[0] == "legacy" and turns[1:] == ["legacy-cached"] * 5
    assert ATTEMPTS[0] == 1, "降级后仍每轮白打 DSM 往返（冷却闩失效）"
    assert "404" in C.broken_info()[1]


# ---- I8: FF refused at real config load (no self-brick) ---------------------------
def test_I8_self_brick_combo_refused_at_real_config_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("dsm:\n  enabled: false\n  openai_compat: false\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))
    with pytest.raises(ValueError):
        dsm_from_raw({"enabled": False, "openai_compat": False})
    ok = dsm_from_raw({"enabled": True, "openai_compat": False})
    assert ok.enabled is True and ok.openai_compat is False


# ---- I9: attribution + three-way usage come from the CLIENT envelope --------------
@needs_client
def test_I9_attribution_and_three_way_usage_land_in_sqlite(home, srv):
    store = _store(home)
    env = _env(home, store, SYS_MSGS)
    env["bill"] = {"to": "sms-core", "row": "t1"}
    _register(srv, home, store, env)
    assert _chat(srv, env).status_code == 200
    row = srv.app.state.llm.usage.recent(1)[0]
    assert (row["sid"], row["cid"], row["lane"], row["skill"]) == (
        "sess-int", "conv-int", "t1", "sms-core")
    assert (row["in"], row["out_reason"], row["out_answer"], row["cache_read"],
            row["cache_write"]) == (467, 9412, 403, 120, 0)
    assert row["out_reason"] + row["out_answer"] == 9815
    st = srv.app.state.llm
    s = st.usage.dsm_summary()
    assert s["dsm_calls"] == 1 and s["tok_cache"] == 120 and s["cache_hits"] == 1
    assert st.settings.cost_detail("demo", 467, 9815)["amount"] > row["cost"], \
        "缓存折扣没进价（cache_read 白记）"


# ---- I10: fan declared by the client, split by the server (contract §6 E) ---------
@needs_client
def test_I10_client_declared_fan_splits_on_the_server(home, srv):
    store = _store(home)
    fan = {"n": 3, "lane_ids": ["t9.1", "t9.2", "t9.3"], "merge": "vote", "concurrency": 3}
    env = _env(home, store, SYS_MSGS, fan=fan, dep=["t1"], enable_fan=True)
    assert env["fan"] == fan and env["dep"] == ["t1"]
    _register(srv, home, store, env)
    r = srv.post("/v1/dsm/fan", headers={**H, **CT}, content=json.dumps(env))
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["dispatchable"] is False and plan["ready"] == []
    assert plan["blocked"] == fan["lane_ids"] and plan["merge"] == "vote"
    assert len(plan["requests_materialised"]) == 3
    env2 = dict(env)
    env2["x"] = {**env["x"], "sms.done_lanes": ["t1"]}
    ready = srv.post("/v1/dsm/fan", headers={**H, **CT}, content=json.dumps(env2)).json()
    assert ready["dispatchable"] is True and ready["ready"] == fan["lane_ids"]
    assert ready["concurrency"] == 3
    assert srv.post("/v1/dsm/fan?run=1", headers={**H, **CT},
                    content=json.dumps(env)).status_code == 409


# ---- I11: out=delta — ndjson frames reassemble exactly once ----------------------
@needs_client
def test_I11_delta_stream_reassembles_exactly_once(home, srv):
    store = _store(home)
    env = _env(home, store, SYS_MSGS, out="delta")
    assert env["out"] == "delta"
    _register(srv, home, store, env)
    r = _chat(srv, env, accept=C.CTYPE_STREAM)
    assert r.status_code == 200, r.text
    assert C.CTYPE_STREAM in r.headers["content-type"]
    frames = [json.loads(l) for l in r.text.strip().splitlines() if l.strip()]
    assert len(frames) >= 2
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1)), "seq 不连续"
    o = C.decode_to_openai_shape(frames)
    assert o["choices"][0]["message"]["content"] == "42", "末帧重复正文（4242）"
    assert o["usage"]["completion_tokens"] == 9815


# ---- I12: the byte claim, measured through both codecs ----------------------------
@needs_client
def test_I12_envelope_is_smaller_than_the_legacy_body_it_replaces(home, srv):
    store = _store(home)
    long_sys = "SYS " * 400
    msgs = [{"role": "system", "content": long_sys}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "t%d " % i * 30}
        for i in range(8)]
    env = _env(home, store, msgs)
    legacy = json.dumps({"model": "demo", "messages": msgs, "tools": TOOLS},
                        ensure_ascii=False).encode("utf-8")
    _register(srv, home, store, env)
    # Remove sms.schema_reg from chat envelope: it's only needed for schema
    # registration, not for chat. The server looks up schema by sch from its store.
    if "x" in env and "sms.schema_reg" in env["x"]:
        del env["x"]["sms.schema_reg"]
    assert _chat(srv, env).status_code == 200
    wire = len(json.dumps(env, ensure_ascii=False).encode("utf-8"))
    assert wire < len(legacy), "信封没省字节 = 只是换了层皮"
    assert len(SENT[-1]["messages"]) == len(msgs), "省字节省掉了语义"
