"""EGC v1 出站标准层的验收：出网真的走了它、往返无损、且转换在毫秒内。

三条承诺各有一道门（对应 `规范_出站精简_EGC_v1.md` §0/§3/§8）：
1. **它是标准不是选项**——每个出网请求的字节都由 `egc.egress()` 产出；关档则逐字节
   回到接它之前（`wire is None` → 交回 httpx 的 `json=`）。
2. **往返无损**——R6 短 id 必须在响应（含流式 delta）里换回原 id，否则工具循环断。
3. **毫秒门**——DSM 信封 → 出网字节的转换 p95 必须 ≤ `egc.budget_ms`（默认 5ms）。

⚠ 基线口径（实测钉住，别拿它去虚报收益）：路由器接 EGC 之前走 httpx `json=`，
而 httpx **本来就是** `separators=(",",":") + ensure_ascii=False`。所以规范 §0 的
「−38.8% 线上字节」是**相对 SMS 客户端 `json.dumps` 默认档**那一跳（§8 改动点 0），
在 SMSocket 这一侧 R1 的增量是 0——本层的真收益来自 R6/R1⁺/R7/R9（字节＋token）
与 R2/R8（缓存命中，token）。这条断言就是防止以后有人拿错基线吹收益。
"""
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket import dsm, egc
from SMSocket.config import (DSMConfig, EgcConfig, ProviderSpec, Settings,
                         egc_from_raw)
from SMSocket.gateway import create_app
from SMSocket.router import _normalize_event
from SMSocket.router import Router

H = {"Authorization": "Bearer sk-test"}
CT = {"Content-Type": "application/json"}
DCT = {"Content-Type": "application/dsm+json"}
ORIG_ID = "call_" + "a" * 24


def _reply_tool(request: httpx.Request) -> dict:
    """回声式上游：把请求里看到的 tool_call id 原样放进响应——客户端拿回的必须是
    **原 id**，短 id 一旦漏出去这条测试就红。"""
    body = json.loads(request.content)
    seen = [tc["id"] for m in body.get("messages", []) for tc in (m.get("tool_calls") or [])]
    return {
        "id": "x", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": seen[0] if seen else ORIG_ID,
                                                 "type": "function",
                                                 "function": {"name": "ztool_01",
                                                              "arguments":
                                                                  "{\"path\":\"配置.json\"}"}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


def _settings(tmp_path, **kw) -> Settings:
    return Settings(listen="127.0.0.1:0", master_keys=["sk-test"],
                    db_path=str(tmp_path / "usage.sqlite3"),
                    pricing={"demo": {"prompt": 2.0, "completion": 8.0, "currency": "USD"}},
                    providers=[ProviderSpec(name="m", base_url="http://mock/v1", keys=["k1"],
                                            models={"demo": "demo-x"})],
                    **kw)


@pytest.fixture()
def live(tmp_path):
    """App whose upstream is a MockTransport (offline, byte-exact capture)."""
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply_tool(request))

    app = create_app(_settings(tmp_path))
    st = app.state.llm
    with TestClient(app) as c:
        st.http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
        c.seen = seen
        yield c


@pytest.fixture(autouse=True)
def _clean_profile():
    """EGC 的档位是模块级真值（热更用），测试之间必须互相看不见——
    否则上一个用例的 lane 白名单会漏进下一个用例，绿成假账。"""
    egc.bind_settings(None)
    egc.reset_stats()
    yield
    egc.bind_settings(None)
    egc.reset_stats()


def _body(tools_n=17, turns=4):
    tools = [{"type": "function", "function": {
        "name": "ztool_%02d" % i, "description": "工具 %d：带中文说明的动作。" % i,
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "路径"}}, "required": ["path"]}}}
        for i in range(tools_n)]
    msgs = [{"role": "system", "content": "内核规则。\n当前时间：2026-10-06 19:20\n" * 8}]
    for t in range(turns):
        msgs.append({"role": "user", "content": "第 %d 轮：读配置并给结论，中文。" % t})
        msgs.append({"role": "assistant", "content": None,
                     "reasoning_content": "内部思考，绝不出网",
                     "tool_calls": [{"id": "%s_%d" % (ORIG_ID, t), "type": "function",
                                     "function": {"name": "ztool_01",
                                                  "arguments": json.dumps(
                                                      {"path": "配置/中文.json"},
                                                      ensure_ascii=True)}}]})
        msgs.append({"role": "tool", "tool_call_id": "%s_%d" % (ORIG_ID, t),
                     "content": json.dumps({"ok": True, "note": "中文结果"}, ensure_ascii=True)})
    msgs.append({"role": "user", "content": "结论？"})
    return {"model": "demo", "messages": msgs, "tools": tools, "temperature": 1,
            "top_p": 1, "stream": False, "presence_penalty": 0, "max_tokens": 4096}


# ---------------------------------------------------------------- 1 出网标准
def test_egc_owns_the_outbound_bytes(live):
    r = live.post("/v1/chat/completions", headers={**H, **CT}, json=_body())
    assert r.status_code == 200, r.text
    wire = live.seen[-1].content
    body = json.loads(wire)
    assert wire == egc.serialize(body)                     # 出网字节＝EGC 的产物
    assert b"reasoning_content" not in wire                # 内部思考零泄漏
    assert b"temperature" not in wire and b"presence_penalty" not in wire   # R9
    assert all(tc.get("type") == "function" for m in body["messages"]  # type 必填·不可删（实测上游 400）
               for tc in (m.get("tool_calls") or []))
    assert all(t.get("type") == "function" for t in body["tools"])   # 协议面一个都不动
    assert "\\u" not in wire.decode()                      # 内外层都无字面转义
    src = [tc["function"]["arguments"] for m in _body()["messages"]
           for tc in (m.get("tool_calls") or [])]
    args = [tc["function"]["arguments"] for m in body["messages"]
            for tc in (m.get("tool_calls") or [])]
    assert args and all("\\u" not in a for a in args)       # R1⁺ 转义税消除
    assert all(json.loads(a) == json.loads(b) for a, b in zip(args, src))   # 且解析同值
    names = [(t.get("function") or t)["name"] for t in body["tools"]]
    assert names == sorted(names)                           # R2 顺序＝缓存键
    assert list(body)[:3] == ["model", "messages", "tools"]  # R2 稳定键序
    assert len(wire) <= len(egc.legacy_wire(body))   # 只可能更短，不可能更长
    st = egc.state()
    assert st["enabled"] and st["calls"] >= 1 and st["ms_p95"] <= st["budget_ms"]


def test_egc_short_ids_never_leak_to_the_client(live):
    r = live.post("/v1/chat/completions", headers={**H, **CT}, json=_body())
    got = r.json()["choices"][0]["message"]["tool_calls"][0]["id"]
    assert got == ORIG_ID + "_0"                            # R6 反查：原 id 回家
    wire = json.loads(live.seen[-1].content)
    on_wire = [tc["id"] for m in wire["messages"] for tc in (m.get("tool_calls") or [])]
    assert on_wire and all(len(i) <= 3 for i in on_wire)     # 出网确实是短 id


def test_egc_disabled_is_byte_for_byte_legacy(tmp_path):
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json=_reply_tool(request))
    app = create_app(_settings(tmp_path, egc=EgcConfig(enabled=False)))
    st = app.state.llm
    with TestClient(app) as c:
        st.http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
        r = c.post("/v1/chat/completions", headers={**H, **CT}, json=_body())
    assert r.status_code == 200, r.text
    assert b"reasoning_content" in seen[-1]                  # 关档＝真回滚，不偷偷精简
    assert r.json()["choices"][0]["message"]["tool_calls"][0]["id"] == ORIG_ID + "_0"
    assert egc.state()["passthrough"] >= 1


def test_lane_tools_subset_reaches_the_wire(tmp_path):
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_reply_tool(request))
    app = create_app(_settings(tmp_path, egc=EgcConfig(lane_tools={"*": ["ztool_01",
                                                                         "ztool_02"]})))
    st = app.state.llm
    with TestClient(app) as c:
        st.http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
        assert c.post("/v1/chat/completions", headers={**H, **CT},
                      json=_body()).status_code == 200
    assert sorted((t.get("function") or t)["name"] for t in seen[-1]["tools"]) \
        == ["ztool_01", "ztool_02"]                          # R4 白名单生效


def test_stream_tool_call_ids_are_restored():
    """流式转发：`_normalize_event` 是 wrap_stream 里唯一的还原点，短 id 必须换回原 id。"""
    ids = egc.IdMap()
    short = ids.put(ORIG_ID + "_0")
    assert short == "c1" and short != ORIG_ID + "_0"
    evt = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": short, "type": "function",
         "function": {"name": "ztool_01", "arguments": '{"path":'}}]}}]})
    ).encode()
    out, _ = _normalize_event(evt, "demo", ids)
    assert ORIG_ID + "_0" in out.decode()                    # 换回原 id
    assert '"c1"' not in out.decode().replace(" ", "")        # 短 id 不外泄
    # 不传映射时不得无中生有（legacy 路径零改动）
    out2, _ = _normalize_event(evt, "demo")
    assert short in out2.decode()


def test_egc_passthrough_keeps_legacy_bytes(tmp_path):
    """关档回滚：wire is None → 交回 httpx 的 json=，出网字节与接 EGC 之前同形。"""
    body = _body(tools_n=3, turns=1)
    w = egc.egress(body, overrides=None)
    assert w.wire is not None
    egc.bind_settings(EgcConfig(enabled=False))
    off = egc.egress(body)
    assert off.wire is None and off.compact is False
    assert off.bytes_out == len(egc.legacy_wire(body))       # 记账按真基线
    assert egc.state()["passthrough"] >= 1
    egc.bind_settings(None)


# ---------------------------------------------------------------- 2 DSM ↔ EGC 往返
def _env(system, tools, msgs, sch, lane="l1", sid="s1"):
    return {"v": 1, "sid": sid, "lane": lane, "sch": sch,
            "x": {"sms.model": "demo"},
            "d": [list(dsm.encode_turn(m)) for m in msgs]}


def test_dsm_envelope_to_egress_wire_roundtrip_is_lossless(tmp_path):
    """DSM 信封 →（物化）→ EGC 出网字节 →（提供商回声）→ 反查 → DSM 响应信封。

    这是「EGC 作为出站标准并入 SMSocket」的核心承诺：信封进、信封出，中间的
    精简层对客户端完全不可见——tool_call id 回家、内容一字不差、策略键不出网。
    """
    system = "你是推理内核。\n当前时间：2026-10-06 19:20\n约束：内部处理用英语。" * 6
    tools = _body()["tools"]
    reg = dsm.register_schema("", system, tools, dsm.SchemaStore(tmp_path / "s.json"))
    sch = reg["ref"]
    msgs = [{"role": "user", "content": "读配置"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": ORIG_ID, "type": "function",
                             "function": {"name": "ztool_01",
                                          "arguments": json.dumps({"path": "配置/中文.json"},
                                                                  ensure_ascii=True)}}]},
            {"role": "tool", "tool_call_id": ORIG_ID,
             "content": json.dumps({"ok": True, "note": "中文结果"}, ensure_ascii=True)},
            {"role": "user", "content": "结论？"}]
    env = _env(system, tools, msgs, sch)
    body = dsm.materialize(env, "demo", "openai-chat",
                           store=dsm.SchemaStore(tmp_path / "s.json"), use_session=False)
    w = egc.egress(body, lane="l1")
    assert w.compact and w.bytes_out < len(egc.legacy_wire(body))
    assert not dsm.leak_check(env, w.body)                    # 策略键永不出网
    assert b"reasoning_content" not in w.wire
    # 提供商按出网形状回一个工具调用（用的是短 id）
    assistant = next(m for m in w.body["messages"] if m.get("tool_calls"))
    short = assistant["tool_calls"][0]["id"]
    assert short != ORIG_ID                                  # 出网确实是短 id
    resp = {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": short, "type": "function",
                                                     "function": {"name": "ztool_01",
                                                                  "arguments":
                                                                      '{"path":"x"}'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}}
    egc.restore_ids(resp, w.ids)
    out = dsm.encode_response(resp, env, alias="demo", egress="direct")
    assert out["stop"] == "tool_call"
    assert out["answer"][0]["id"] == ORIG_ID                  # 原 id 回到信封
    assert out["usage"]["in"] == 100


def test_dsm_http_path_serves_egc_standard(tmp_path):
    """HTTP 层：/v1/dsm/chat 的出网字节同样由 EGC 产出，响应信封里是原 id。"""
    system = "内核规则。\n当前时间：2026-10-06 19:20" * 10
    tools = _body(tools_n=6, turns=0)["tools"]
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json=_reply_tool(request))
    app = create_app(_settings(
        tmp_path, dsm=DSMConfig(enabled=True,
                                schema_store=str(tmp_path / "dsm_schemas.json"))))
    st = app.state.llm
    with TestClient(app) as c:
        st.http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        st.router = Router(st.settings, st.pool, st.usage, st.http, st.gate)
        reg = c.post("/v1/dsm/schema", headers={**H, **DCT},
                     content=json.dumps({"system": system, "tools": tools}).encode())
        assert reg.status_code == 200, reg.text
        env = _env(system, tools, [{"role": "user", "content": "结论？"}], reg.json()["ref"])
        r = c.post("/v1/dsm/chat", headers={**H, **DCT}, content=json.dumps(env).encode())
    assert r.status_code == 200, r.text
    assert seen, "出网请求没被捕获"
    assert b'"sch"' not in seen[-1] and b'"sid"' not in seen[-1]   # 信封/策略键不出网
    wire = json.loads(seen[-1])
    assert all(t.get("type") == "function" for t in wire["tools"])  # 协议面完整（红线）
    assert r.json()["answer"][0]["id"] == ORIG_ID     # 回声即信封里的原 id
    assert egc.state()["calls"] >= 1


# ---------------------------------------------------------------- 3 毫秒门
def test_egc_egress_within_budget_in_clean_process():
    """egress() 单段的毫秒门也在干净子进程里量（同进程只留粗门）。

    满载套件里 p95 会被别的用例的 CPU/GC 噪声抬高——写死 5ms 就是随机红灯，
    写死 25ms 又太松；所以细门交给子进程，这里只保证「没跑飞」。
    """
    import subprocess
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = ("import sys,time;sys.path.insert(0,r'%s');sys.path.insert(0,r'%s');"
              "from SMSocket import egc;import test_egc as T;"
              "b=T._body(tools_n=40,turns=20);"
              "c=[egc.egress(b,memo=False).ms for _ in range(150)];"
              "w=[egc.egress(b).ms for _ in range(150)];"
              "q=lambda x,p:sorted(x)[int(p*(len(x)-1))];"
              "print('EGCMS',round(q(c,0.95),3),round(q(w,0.95),3),round(q(w,0.5),3))"
              % (root, os.path.join(root, "tests")))
    out = subprocess.run([_sys.executable, "-B", "-c", script],
                         capture_output=True, text=True, timeout=180)
    line = [l for l in out.stdout.splitlines() if l.startswith("EGCMS ")]
    assert line, ("egress 基准没跑出数", out.stdout[-400:], out.stderr[-600:])
    cold95, warm95, warm50 = (float(x) for x in line[0].split()[1:])
    bm = egc.state()["budget_ms"]
    assert warm95 <= bm, ("热路径超预算", warm50, warm95, bm)
    assert cold95 <= bm, ("冷路径（无缓存）超预算", cold95, bm)
    body = _body(tools_n=40, turns=20)
    inproc = [egc.egress(body).ms for _ in range(40)]
    assert sorted(inproc)[int(0.95 * 39)] < 25.0, "同进程粗门：没跑飞即可"


def test_egc_selftest_gate_passes():
    """离线自检＝这一层的验收门（等价性/守恒/合法性/毫秒），红了就不配当标准。"""
    rep = egc.selftest(iters=60)
    assert rep["ok"], rep
    assert rep["default_profile_strict_equivalent"] is True
    assert rep["default_profile_conserves_content"] is True
    assert rep["ids_roundtrip"] and rep["inner_escapes_gone"]
    assert rep["reasoning_leak"] is False
    assert rep["ms_warm_p95"] <= rep["budget_ms"]


def test_egc_budget_violation_is_counted(tmp_path):
    """毫秒门不是装饰：budget_ms 压到极低时，账上必须看见超预算次数。"""
    egc.reset_stats()
    egc.bind_settings(EgcConfig(budget_ms=0.0001))
    try:
        for _ in range(5):
            egc.egress(_body(tools_n=30, turns=10))
        assert egc.state()["over_budget"] >= 1
    finally:
        egc.bind_settings(EgcConfig())
        egc.reset_stats()


# ---------------------------------------------------------------- 4 配置面
def test_egc_config_rejects_bad_switches():
    with pytest.raises(ValueError):
        egc_from_raw({"ff": {"R99_nope": True}})
    with pytest.raises(ValueError):
        egc_from_raw({"budget_ms": 0})
    with pytest.raises(ValueError):
        egc_from_raw({"budget_ms": 9999})
    with pytest.raises(ValueError):
        egc_from_raw({"lane_tools": {"l1": "not-a-list"}})
    ok = egc_from_raw({"ff": {"R3a_drop_fn_desc": True}, "budget_ms": 2.5,
                       "lane_tools": {"l1": ["read"]}})
    assert ok.ff["R3a_drop_fn_desc"] is True and ok.budget_ms == 2.5
    assert egc_from_raw(None).enabled is True                 # 缺块＝默认档（出站标准默认开）


def test_egc_semantic_cost_switches_are_off_by_default():
    """有语义代价的开关一律默认关——省 token 不能靠悄悄改变模型看到的东西。"""
    for k in egc.SEMANTIC_COST:
        # 「有代价的那个方向」必须默认关：删类开关默认 False；R3c 是保留开关
        # （默认 True＝不删 required）；R4 默认开但**只在调用方给了白名单时**才动内容。
        assert egc.FF[k][0] is (k in ("R3c_keep_required", "R4_lane_subset")), k
    assert egc.egress(_body(tools_n=3, turns=0)).acct["tools_out"] == 3      # 无白名单＝不裁
    assert egc.egress(_body(tools_n=3, turns=0), lane="x").acct["tools_out"] == 3
    assert egc.ff("R3a_drop_fn_desc") is False
    assert egc.ff("R3a_drop_fn_desc", {"R3a_drop_fn_desc": True}) is True
    # 打开 R3a 后语义指纹必须**真的变了**（自检如实报告不等价，而不是假装等价）
    body = _body(tools_n=3, turns=0)
    w = egc.egress(body, overrides={"R3a_drop_fn_desc": True})
    assert egc.semantic_signature(w.body) != egc.semantic_signature(body)


def test_egc_ambiguous_pairs_gates_r3a():
    """R3a 的机检门：task/task_plan、ask/ask_user 这类同词根簇＝删描述会掉精准度。"""
    amb = egc.ambiguous_pairs([{"function": {"name": n}} for n in
                               ("task", "task_plan", "ask", "ask_user", "grep")])
    assert ("task", "task_plan") in amb and ("ask", "ask_user") in amb
    assert egc.ambiguous_pairs([{"function": {"name": n}} for n in
                                ("read", "write", "glob")]) == []


# ---------------------------------------------------------------- 3.5 端到端毫秒门
def test_dsm_egc_full_conversion_within_milliseconds():
    """**用户的硬要求**：DSM 信封 ↔ EGC 的整条转换必须在几毫秒内。

    量的是「信封 → 物化 → 精简 → 出网字节 →（提供商回声短 id）→ 反查 → 响应信封」
    整条链，40 工具＋20 轮工具往返的真实规模，且**在干净子进程里量**：同进程测量会
    被套件的 CPU/GC 噪声污染（实测同一段转换隔离 p95 0.77ms、满载 6.15ms），
    那种红灯是假的、绿灯也可能是假的。基准脚本真源＝`tests/bench_dsm_egc.py`。
    """
    import subprocess
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([_sys.executable, "-B",
                          os.path.join(root, "tests", "bench_dsm_egc.py")],
                         capture_output=True, text=True, timeout=180)
    line = [l for l in out.stdout.splitlines() if l.startswith("MS ")]
    assert line, ("基准没跑出数", out.stdout[-500:], out.stderr[-800:])
    p50, p95, mx = (float(x) for x in line[0].split()[1:])
    assert p95 < 5.0, ("整条转换超几毫秒", p50, p95, mx)
    assert mx < 10.0, ("最坏情况失控", p50, p95, mx)


def test_dsm_egc_conversion_is_lossless_in_process(tmp_path):
    """同进程只断**无损**（不测时延）：原 id 回家、策略键不出网、内部思考不出网。"""
    system = "内核规则。\n当前时间：2026-10-06 19:20\n" * 20
    tools = _body(tools_n=12, turns=0)["tools"]
    store = dsm.SchemaStore(str(tmp_path / "s.json"))
    reg = dsm.register_schema("", system, tools, store)
    msgs = [{"role": "user", "content": "读配置"},
            {"role": "assistant", "content": "",
             "reasoning_content": "内部思考",
             "tool_calls": [{"id": "tc1", "type": "function",
                             "function": {"name": "ztool_01",
                                          "arguments": json.dumps({"path": "配置.json"},
                                                                  ensure_ascii=True)}}]},
            {"role": "tool", "tool_call_id": "tc1",
             "content": json.dumps({"ok": True, "note": "结果"}, ensure_ascii=True)},
            {"role": "user", "content": "结论？"}]
    env = {"v": 1, "sid": "s1", "lane": "l1", "sch": reg["ref"], "x": {"sms.model": "demo"},
           "d": [list(dsm.encode_turn(m)) for m in msgs]}
    body = dsm.materialize(env, "demo", "openai-chat", store=store, use_session=False)
    w = egc.egress(body, lane="l1")
    assert w.compact and not dsm.leak_check(env, w.body)
    assert b"reasoning_content" not in w.wire and "\\u" not in w.wire.decode()
    wid = next(t["id"] for m in w.body["messages"] for t in (m.get("tool_calls") or []))
    assert wid != "tc1" and len(wid) <= 4                      # 线上确实是短 id
    resp = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": wid, "type": "function",
                        "function": {"name": "ztool_01", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
    egc.restore_ids(resp, w.ids)
    out = dsm.encode_response(resp, env, alias="demo")
    assert out["answer"][0]["id"] == "tc1"                     # 原 id 回家
    assert out["usage"]["in"] == 7
