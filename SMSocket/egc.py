"""EGC v1（Egress Compact）— SMSocket 的**出站标准层**。

定位（三层里的第三层）：

    DSM 内部信封 ──dsm.materialize──▶ 物化 body ──EGC──▶ 出网字节 ──▶ 提供商
      引用＋策略（省内部跳）              内容（省不掉）      结构税（本层）

一句话：**DSM 省的是内部那一跳，EGC 省的是出网那一跳。** 提供商无状态，历史必须
重放，所以这一跳省不掉「内容」，只能省掉结构税（线上字节＋序列化 CPU）、前缀缓存
的浪费、以及双重编码里的转义税。规则表与全部实测依据见产物目录 DSM_v1 的
`规范_出站精简_EGC_v1.md`；本模块是它在 SMSocket 里的真实现（默认档＝实测零语义
损失的那一组开关）。

两条硬约束（规范 §8 红线）：
1. EGC 必须排在 `dsm.materialize` **之后**、序列化**之前**——它是出网的最后一道。
2. R6 的 id 映射表随请求生命周期存活：响应里的 `tool_calls[].id` 必须能反查回原 id
   （`restore_ids`），否则工具循环直接断。

转换耗时是这一层的验收门槛（`budget_ms`，默认 5ms）：出网标准不能变成延迟税。
"""
from __future__ import annotations

import copy
import gc
import hashlib
import json
import logging
import time

_HAS_CPU = hasattr(time, "process_time")


def _gc_counts():
    return gc.get_count()

log = logging.getLogger("smsocket.egc")

# ---------------------------------------------------------------- FF 开关表
# 值 = (默认, 一句话理由)。默认只开「实测零语义损失」的；有语义代价的一律默认关，
# 由调用方按 lane 显式打开——省 token 不能靠悄悄改变模型看到的东西。
FF = {
    "R1_wire_compact": (True, "separators+ensure_ascii=False：只省线上字节，token 不变（实测 −38.8%）"),
    "R2_stable_key_order": (True, "固定键序＋tools 按 name 排序：乱序实测首帧 cached=0"),
    "R3a_drop_fn_desc": (False, "删 function.description：−978 tok，但易混簇精准度 17/24→14/24"),
    "R3b_drop_prop_desc": (False, "删参数 description：−455 tok，风险低于 R3a"),
    "R3c_keep_required": (True, "required 保留：删了只省 127 tok，却换必填项漏填"),
    "R3d_drop_parameters": (False, "参数表空时删整键：−956 tok，但参数形状全丢"),
    "R4_lane_subset": (True, "lane 作用域工具子集：17→6 工具 −1840 tok（需 lane_tools 白名单）"),
    "R5_content_string": (True, "单 part 的 content[] → 字符串：token 不变，只省线上字节"),
    "R6_short_call_id": (True, "tool_call id 短化（可逆映射）：−20 tok/次，id 是不透明串"),
    "R7_drop_null_fields": (True, "删 null/空串/默认值字段：token 不变"),
    "R8_volatile_at_tail": (True, "易变内容（时间戳/session）挪到末尾：实测 cached 2048→4096"),
    "R9_omit_default_params": (True, "省略与提供商默认值相同的参数：token 不变"),
    "R10_precomputed_prefix": (True, "稳定前缀（清洗后的 tools）按指纹复用：只影响 CPU"),
}
_DEFAULTS = {k: v[0] for k, v in FF.items()}
#: 默认档之外，这些开关一旦打开就**改变模型所见**——自检必须如实报告「语义不等价」。
SEMANTIC_COST = ("R3a_drop_fn_desc", "R3b_drop_prop_desc", "R3c_keep_required",
                 "R3d_drop_parameters", "R4_lane_subset")


def ff(name, overrides=None):
    """单个开关的真值（默认档 ⊕ 调用方覆盖）。"""
    v = dict(_DEFAULTS)
    v.update(overrides or {})
    return bool(v.get(name, False))


def profile(overrides=None):
    """当前生效的开关全表（给 /healthz、面板与自检用）。"""
    v = dict(_DEFAULTS)
    v.update(overrides or {})
    return v


_ACTIVE_DEFAULT: dict[str, frozenset] = {}


def active(overrides=None):
    """当前档位下为真的开关**集合**——热路径只查成员，不再每查一个 flag 就重建整张
    默认表（旧 `on = lambda k: ff(k, ov)` 每条消息重建一次，实测占 egress CPU 大头）。

    只缓存「无覆盖」这一档（＝进程级常量）：**绝不按 id(dict) 缓存**——临时字典的
    id 会被 CPython 回收复用，那会把上一档的 flag 集合悄悄发给下一档。带覆盖时
    现算一次（每次 egress 算一次，不是每条消息一次）。
    """
    if overrides:
        return frozenset(k for k, v in profile(overrides).items() if v)
    v = _ACTIVE_DEFAULT.get("v")
    if v is None:
        v = frozenset(k for k, on in _DEFAULTS.items() if on)
        _ACTIVE_DEFAULT["v"] = v
    return v


# ---------------------------------------------------------------- 序列化（R1）
def legacy_wire(body):
    """路由器**接 EGC 之前**的出网字节：httpx `json=` 的编码形状（实测它就是紧凑＋
    非转义）。所有「省了多少字节」的账必须以它为基线，拿 `json.dumps` 默认档比是虚报。"""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def serialize(body, wire_compact=True):
    """出网字节。`ensure_ascii=False` 让 CJK 6B→3B；token 一字不变（实测），
    省的是**上传字节与序列化 CPU**，不是计费。"""
    if wire_compact:
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return json.dumps(body).encode("utf-8")


def hashlib_key(o):
    return "sha1:" + hashlib.sha1(json.dumps(o, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 调用 id（R6）
class IdMap:
    """tool_call id 短化（可逆）。id 对提供商是不透明串，只要请求内自洽即可：
    assistant.tool_calls[i].id 与随后 tool.tool_call_id 必须同值——本类保证双向一致。"""

    __slots__ = ("_n", "_fwd", "_rev")

    def __init__(self):
        self._n = 0
        self._fwd, self._rev = {}, {}

    def put(self, oid):
        if oid not in self._fwd:
            self._n += 1
            self._fwd[oid] = "c%d" % self._n
            self._rev[self._fwd[oid]] = oid
        return self._fwd[oid]

    def get(self, nid):
        return self._rev.get(nid, nid)

    def __len__(self):
        return len(self._fwd)

    def __bool__(self):
        return bool(self._fwd)


# ---------------------------------------------------------------- 结构精简
def _compact_inner(s):
    """R1⁺：`arguments` / tool 结果都是「字符串里塞 JSON」。内层 \\uXXXX 是模型真看到
    的字面文本，**实测计费**（−18 tok/次）——所以 R1 必须递归进内层，不只 body 外层。
    解码同值才重排，解不开就原样返回。"""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if t[:1] not in ("{", "["):
        return s
    try:
        v = json.loads(t)
    except Exception:
        return s
    # 只接受「解析同值」的改写：v 由 json.loads 产出、out 由 json.dumps 产出，
    # CPython 的 float/int 最短表示往返精确，故无需再 parse 一遍验同值（旧写法每条
    # 内层 JSON 多付一次解析，实测占 egress CPU 的约 1/4）。含非有限数则原样返回。
    if any(x in t for x in ("NaN", "Infinity", "-Infinity")):
        return s
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _shareable(m, on):
    """这条消息**没有任何可精简点** → 与调用方共享引用（零拷贝、零分配）。

    判据取保守方向：不是纯 role+content 两条键、内容不是非空 str、带 tool_calls /
    tool_call_id / reasoning_content、或字符串里带反斜杠-u 转义（内层双重编码值得重排）——
    一律走慢路 deepcopy。省的是**分配**：在线 egress 的长尾不是纯 CPU，是 deepcopy
    撑起的对象图把 gen2 GC 拖进请求路径（实测 wall p95 4.3ms 而 CPU 时间打满同一秒）。
    共享对象绝不就地改：唯一的就地写发生在 `_demote_volatile`，那里已改成 copy-on-write。
    """
    if not on("R7_drop_null_fields"):
        return False
    if len(m) != 2 or m.get("role") not in ("system", "user"):
        return False
    c = m.get("content")
    if not isinstance(c, str) or not c.strip():
        return False
    if "\\u" in c[:400]:
        return False
    return True


def _clean_msg(m, ids, on):
    """单条消息精简：只删「提供商不消费」的东西。返回新对象，绝不就地改。"""
    if _shareable(m, on):
        return m
    # 逐条 deepcopy 是旧写法最贵的一刀：一条消息里 90% 的值是 str/数字，整棵子树
    # 重造纯属浪费。这里只给**容器值**（content parts、tool_calls）建新对象，
    # 标量值直接共享——语义与 deepcopy 全等（后续只改写容器与新 dict）。
    m = {k: (copy.deepcopy(v) if isinstance(v, (list, dict)) else v)
         for k, v in m.items()}
    if on("R5_content_string"):
        c = m.get("content")
        if isinstance(c, list) and len(c) == 1 and isinstance(c[0], dict) \
                and c[0].get("type") == "text" and len(c[0]) == 2:
            m["content"] = c[0]["text"]                    # parts[] -> str（实测 token 不变）
    if isinstance(m.get("content"), list):                 # 多 part：逐 part 收敛
        for p in m["content"]:
            if on("R7_drop_null_fields") and isinstance(p, dict):
                for k in [k for k, v in p.items() if v in (None, "", [], {})]:
                    p.pop(k)
    if on("R7_drop_null_fields"):
        for k in [k for k, v in list(m.items()) if v in (None, "", [], {})]:
            m.pop(k)
    # assistant 带 tool_calls 时 content 不可删：严格 OpenAI 兼容上游（sensenova/agnes-ai
    # 等）对缺失 content 的 assistant 消息回 400 BadRequestError，工具循环整条断。
    # R7 把 null/"" 的 content 删掉后，这里补回 ""，保证 content 字段始终在场。
    if m.get("tool_calls") and "content" not in m:
        m["content"] = ""
    for tc in (m.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        if on("R6_short_call_id") and tc.get("id"):
            tc["id"] = ids.put(tc["id"])
        # type:function 是协议面必填字段，不是可删的默认值——实测 sensenova/agnes-ai
        # 等上游对缺失 type 的 tool_call 回 400 "invalid tool_call type"，工具循环整条断。
        # R7 只删真正的空值字段；这里反过来确保 type 存在，而非删掉它。
        tc.setdefault("type", "function")
        f = tc.get("function") or {}
        if on("R1_wire_compact") and isinstance(f.get("arguments"), str):
            f["arguments"] = _compact_inner(f["arguments"])
    if m.get("role") == "tool":
        if m.get("tool_call_id") and on("R6_short_call_id"):
            m["tool_call_id"] = ids.put(m["tool_call_id"])
        if on("R1_wire_compact") and isinstance(m.get("content"), str):
            m["content"] = _compact_inner(m["content"])     # 内层双重编码（实测 −18 tok/次）
    if m.get("role") == "assistant" and on("R7_drop_null_fields"):
        m.pop("reasoning_content", None)                    # 思考回放：出网必删（既贵又泄内部态）
        m.pop("refusal", None)
    return m


def _clean_tool(t, on):
    t = copy.deepcopy(t)
    f = t.get("function") or {}
    if not f:
        return t
    if on("R3a_drop_fn_desc"):
        f.pop("description", None)
    p = f.get("parameters") or {}
    props = p.get("properties") or {}
    if on("R3b_drop_prop_desc"):
        for v in props.values():
            if isinstance(v, dict):
                v.pop("description", None)
    if not on("R3c_keep_required"):
        p.pop("required", None)
    if on("R3d_drop_parameters") and not props:
        f.pop("parameters", None)
    return t


# 稳定前缀复用（R10）：tools 块在一个 lane 的生命周期内是**常量**，每轮重排 9 KB
# JSON 是纯 CPU 浪费。按「原始 tools ⊕ 开关档 ⊕ lane 白名单」指纹缓存清洗结果。
_TOOLS_MEMO: dict[tuple, tuple] = {}
_MEMO_MAX = 64
_PROFILE_TOKEN = 0


def _profile_token(overrides=None):
    """当前档位的廉价标识（bind_settings 时算一次）。带 overrides 的调用走不了缓存。"""
    global _PROFILE_TOKEN
    if overrides:
        return None
    _PROFILE_TOKEN = hash(frozenset((k, v) for k, v in _CFG["ff"].items()))
    return _PROFILE_TOKEN


def _clean_tools(tools, overrides=None, lane_tools=None, memo=True):
    on = active(overrides).__contains__
    keep = set(lane_tools) if (on("R4_lane_subset") and lane_tools is not None) else None
    key = None
    if memo and not overrides:
        # 键一＝源列表**对象身份**（同进程反复交同一列表时零成本命中；缓存握着强引用，
        # id 不会被回收后复用成假命中）。
        # 键二＝源列表**内容指纹**：在线路径每轮新建 tools 列表（dsm.materialize 从
        # schema 仓库取），身份键**永不命中**——实测 40 工具每轮多付 0.6ms 重清洗，
        # 且 64 槽缓存被同一条 SYS 的不同副本撑满后整表清空。指纹＝canonical dumps 的
        # sha1，实测 0.16ms，换掉 0.6ms 的清洗循环；命中后再比一次内容防哈希碰撞。
        ptk = _profile_token(overrides)
        kt = None if keep is None else tuple(sorted(keep))
        key = (id(tools), ptk, kt)
        hit = _TOOLS_MEMO.get(key)
        if hit is not None and hit[0] is tools:
            return list(hit[1])                           # 浅拷贝：调用方只排序不改元素
        fp = "fp:" + hashlib_key(tools)                  # 每轮只算一次指纹
        hit = _TOOLS_MEMO.get((fp, ptk, kt))
        if hit is not None and hit[1] == tools:
            cleaned = hit[1]
            _TOOLS_MEMO[key] = (tools, cleaned)           # 身份键回填：同列表再来时零成本
            _TOOLS_MEMO[(fp, ptk, kt)] = (list(tools), cleaned)
            return list(cleaned)
    # 默认档（不删描述/不删 required/不删参数表）**不动工具内容**——此时整块共享引用，
    # 绝不 deepcopy：实测 40 工具×20 轮的冷路径 5.96ms 全花在这层拷贝上。
    dirty = (on("R3a_drop_fn_desc") or on("R3b_drop_prop_desc")
             or on("R3d_drop_parameters") or not on("R3c_keep_required"))
    out = []
    for t in tools:
        if keep is not None:
            name = ((t.get("function") or t)).get("name")
            if name not in keep:
                continue
        out.append(_clean_tool(t, on) if dirty else t)
    if key is not None:
        if len(_TOOLS_MEMO) >= _MEMO_MAX:
            _TOOLS_MEMO.clear()
        cleaned = list(out)
        _TOOLS_MEMO[key] = (tools, cleaned)
        _TOOLS_MEMO[("fp:" + hashlib_key(tools), key[1], key[2])] = (list(tools), cleaned)
    return out


VOLATILE_HINTS = ("当前时间", "现在时间", "今日", "session＝", "会话拓扑", "时间＝",
                  "current date", "today is", "timestamp")


def _demote_volatile(msgs):
    """R8：把「每轮都变」的行从稳定前缀里摘出来，追加到最末一条 user 之后。

    实测依据：易变文本放 system 头部 -> cached 4096->2048；追加在尾部 -> cached 仍 4096。
    只位移、不增删（content_conservation 按行多重集把关）。找不到可挂的尾巴时整条规则
    直接跳过：宁可不省，也绝不丢内容。
    """
    tail = None
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            tail = m
            break
    cut = []
    for m in msgs:
        if m.get("role") != "system" or not isinstance(m.get("content"), str):
            continue
        cut += [l for l in m["content"].split("\n") if any(h in l for h in VOLATILE_HINTS)]
    if not cut or tail is None:
        return msgs
    for i, m in enumerate(msgs):          # 挂载目标换成自己的副本（可能是共享引用）
        if m is tail:
            msgs[i] = tail = dict(m)
            break
    for i, m in enumerate(msgs):
        if m.get("role") != "system" or not isinstance(m.get("content"), str):
            continue
        lines = m["content"].split("\n")
        keep = [l for l in lines if l not in cut]
        if len(keep) != len(lines):
            nm = dict(m); nm["content"] = "\n".join(keep); msgs[i] = nm
    tail["content"] += "\n" + "\n".join(cut)
    return msgs


# ---------------------------------------------------------------- 主入口（openai 侧）
KEY_ORDER = ["model", "messages", "tools", "tool_choice", "max_tokens",
             "max_completion_tokens", "reasoning_effort", "response_format",
             "stop", "seed", "temperature", "top_p", "stream", "stream_options", "user"]


def compact_openai(body, overrides=None, lane_tools=None, ids=None, memo=True):
    """openai-chat body → 精简 body（不改原对象）。返回 (body, 记账)。

    `ids` 可由调用方传入（响应侧要反查）；不传则新建。`lane_tools` 是 R4 的白名单，
    None＝不按 lane 裁剪。
    """
    on = active(overrides).__contains__   # 成员判定：不再每查一个 flag 重建整张默认表
    ids = ids if ids is not None else IdMap()
    b = dict(body)
    acct = {"short_ids": 0, "tools_in": len(body.get("tools") or []), "tools_out": 0}

    if "tools" in b:
        b["tools"] = _clean_tools(b["tools"], overrides, lane_tools,
                                  memo=memo and on("R10_precomputed_prefix"))
        if on("R2_stable_key_order"):
            # 顺序本身就是缓存键的一部分：乱序实测首帧 cached=0
            b["tools"] = sorted(b["tools"], key=lambda t: ((t.get("function") or t).get("name") or ""))
        acct["tools_out"] = len(b["tools"])

    if "messages" in b:
        msgs = [_clean_msg(m, ids, on) for m in b["messages"]]
        if on("R8_volatile_at_tail"):
            msgs = _demote_volatile(msgs)
        b["messages"] = msgs

    if on("R9_omit_default_params"):
        for k, dflt in (("temperature", 1), ("top_p", 1), ("stream", False),
                        ("n", 1), ("parallel_tool_calls", True), ("store", False)):
            if k in b and b[k] == dflt:
                b.pop(k)
        for k in ("presence_penalty", "frequency_penalty", "logit_bias"):
            if k in b and b[k] in (0, None, {}):
                b.pop(k)

    if on("R2_stable_key_order"):
        b = {k: b[k] for k in KEY_ORDER if k in b} | {k: v for k, v in b.items() if k not in KEY_ORDER}
    acct["short_ids"] = len(ids)
    return b, acct


# ---------------------------------------------------------------- anthropic 侧
ANTHROPIC_DEFAULTS = {"top_k": None, "metadata": None}


def compact_anthropic(body, overrides=None, cache_blocks=False):
    """anthropic body → 精简 body。与 openai 侧的本质差别：anthropic 的前缀缓存是
    **显式**的（cache_control:ephemeral）且按块计费——所以这里默认**不打点**，
    只被动精简；打点方案由 `anthropic_cache_plan()` 建议、调用方决定（写缓存 1.25×）。"""
    on = active(overrides).__contains__
    b = dict(body)
    if "tools" in b:
        b["tools"] = _clean_tools([{"function": t} for t in b["tools"]], overrides, None,
                                  memo=on("R10_precomputed_prefix"))
        b["tools"] = [t["function"] for t in b["tools"]]
        if on("R2_stable_key_order"):
            b["tools"] = sorted(b["tools"], key=lambda t: t.get("name") or "")
    if on("R7_drop_null_fields"):
        out = []
        for m in b.get("messages", []):
            m = {k: v for k, v in m.items() if v not in (None, "", [], {})}
            out.append(m)
        if "messages" in b:
            b["messages"] = out
    if isinstance(b.get("system"), str) and cache_blocks:
        b["system"] = [{"type": "text", "text": b["system"]}]
    return b


def anthropic_cache_plan(body, breakpoints=("system", "tools", "last_user")):
    """给出打点建议（哪几块加 cache_control）——**不直接改 body**：打错位置等于白花
    1.25× 的写缓存价，只有「该前缀真会被复用」时才值得打。"""
    cand = {"system": {"where": "system[-1]", "why": "SYS 在一个 lane 内冻结，复用率最高"},
            "tools": {"where": "tools[-1]", "why": "工具块实测占输入 56%，是最大可缓存前缀"},
            "last_user": {"where": "messages[-1].content[-1]",
                          "why": "仅当历史尾部稳定时才有意义"}}
    return [cand[k] for k in (breakpoints or ()) if k in cand and body.get(
        {"system": "system", "tools": "tools", "last_user": "messages"}[k])]


# ---------------------------------------------------------------- 等价性与合法性
def _canon_text(s):
    """字符串里塞 JSON（arguments / tool 结果）：按解析后的值比，转义与键序差异不算语义变化。"""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if t[:1] in "{[":
        try:
            return "JSON:" + json.dumps(json.loads(t), ensure_ascii=False, sort_keys=True)
        except Exception:
            return s
    return s


def _tool_shape(tools):
    out = []
    for t in tools or []:
        f = t.get("function") or t
        p = f.get("parameters") or f.get("input_schema") or {}
        out.append((f.get("name"), tuple(sorted((p.get("properties") or {}).keys())),
                    tuple(sorted(p.get("required") or []))))
    return sorted(out)


def semantic_signature(body):
    """语义指纹：精简前后必须全等，否则就是「改变了模型看到的东西」。
    口径＝角色序＋每条文本内容＋工具名＋参数名/required＋tool_call 配对。"""
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if c is None:                          # null 与 "" 对模型同义（assistant+tool_calls 无文本）
            c = ""
        if isinstance(c, list):
            c = "\n".join(p.get("text", json.dumps(p, ensure_ascii=False, sort_keys=True))
                          if isinstance(p, dict) else str(p) for p in c)
        out.append((m.get("role"), _canon_text(c) if isinstance(c, str)
                    else json.dumps(c, ensure_ascii=False, sort_keys=True)))
        for tc in (m.get("tool_calls") or []):
            f = tc.get("function") or {}
            out.append(("__call__", f.get("name"), _canon_text(f.get("arguments") or "{}")))
        if m.get("role") == "tool":
            out.append(("__toolpair__", bool(m.get("tool_call_id"))))
    return {"messages": out, "tools": _tool_shape(body.get("tools"))}


def content_conservation(body):
    """内容守恒：角色序＋全文本行多重集。R8 允许「位移」不允许「增删」，
    严格语义指纹会因位移而变——这条才是不丢信息的判据。"""
    roles, lines = [], []
    for m in body.get("messages", []):
        roles.append(m.get("role"))
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
        if isinstance(c, str):
            lines += [_canon_text(l) for l in c.split("\n") if l.strip()]
        for tc in (m.get("tool_calls") or []):
            f = tc.get("function") or {}
            lines.append("CALL:" + str(f.get("name")) + ":" + json.dumps(
                json.loads(f.get("arguments") or "{}"), ensure_ascii=False, sort_keys=True))
        if m.get("role") == "tool" and isinstance(c, str):
            lines.append("TOOLRESULT:" + _canon_text(c))
        # 注：reasoning_content / refusal 不计入守恒——它们是内部态，出网必删（规范红线），
        # 是否泄漏由 selftest 的 reasoning_leak 单独把关。
    for t in body.get("tools") or []:
        f = t.get("function") or t
        for k, v in ((f.get("parameters") or {}).get("properties") or {}).items():
            if isinstance(v, dict) and v.get("description"):
                lines.append("PDESC:" + k + ":" + v["description"])
        if f.get("description"):
            lines.append("FDESC:" + f["description"])
    return {"roles": roles, "lines": sorted(lines), "tools": _tool_shape(body.get("tools"))}


def validate(body, style="openai"):
    """出网前机检：提供商会因这些原因 400（实测：删 tools 包装＝HTTP 400）。返回问题列表。"""
    bad = []
    if style == "openai":
        for i, t in enumerate(body.get("tools") or []):
            if t.get("type") != "function" or not isinstance(t.get("function"), dict):
                bad.append("tools[%d] 缺 {'type':'function','function':{…}} 包装（实测 400）" % i)
            elif not (t["function"].get("name")):
                bad.append("tools[%d] 缺 name" % i)
        seen = set()
        for m in body.get("messages") or []:
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id"):
                    seen.add(tc["id"])
            if m.get("role") == "tool" and m.get("tool_call_id") not in seen:
                bad.append("tool 消息的 tool_call_id=%r 没有对应 tool_calls（实测 400 类）"
                           % m.get("tool_call_id"))
    elif style == "anthropic":
        if not body.get("max_tokens"):
            bad.append("anthropic 必须带 max_tokens")
        prev = None
        for m in body.get("messages") or []:
            if m.get("role") == prev:
                bad.append("anthropic 要求 user/assistant 交替，连续两个 %s" % m.get("role"))
            prev = m.get("role")
    return bad


def ambiguous_pairs(tools):
    """R3a 的机检门：工具名互为前缀／共享词根 → 视为「语义不可分」，此时删 description
    实测掉精准度（易混簇 17/24 → 14/24），必须留着。空列表＝删描述安全。"""
    names = [((t.get("function") or t).get("name")) for t in tools or []]
    names = [n for n in names if n]
    bad = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if a == b or a.startswith(b) or b.startswith(a):
                bad.append((a, b))
                continue
            if set(a.split("_")) & set(b.split("_")):
                bad.append((a, b))
    return sorted(set(bad))


# ---------------------------------------------------------------- 配置绑定
# 由 config.EgcConfig 在 create_app / 每次 reload 时灌进来（见 gateway.bind_egc）。
_CFG = {"enabled": True, "ff": {}, "lane_tools": {}, "budget_ms": 5.0, "validate": True}


def bind_settings(cfg=None):
    """EGC 的真值来源。cfg=None＝回到默认档（灰度回滚只要一行 `egc.enabled: false`）。"""
    global _CFG
    if cfg is None:
        _CFG = {"enabled": True, "ff": {}, "lane_tools": {}, "budget_ms": 5.0, "validate": True}
        return dict(_CFG)
    _CFG = {
        "enabled": bool(getattr(cfg, "enabled", True)),
        "ff": {str(k): bool(v) for k, v in (getattr(cfg, "ff", {}) or {}).items()},
        "lane_tools": {str(k): [str(x) for x in (v or [])]
                       for k, v in (getattr(cfg, "lane_tools", {}) or {}).items()},
        "budget_ms": float(getattr(cfg, "budget_ms", 5.0) or 5.0),
        "validate": bool(getattr(cfg, "validate", True)),
    }
    _profile_token()
    _ACTIVE_DEFAULT.clear()      # 无覆盖档＝默认表，与配置档无关，清一下以防万一
    _TOOLS_MEMO.clear()          # 档位变了，旧清洗结果一律作废
    unknown = [k for k in _CFG["ff"] if k not in _DEFAULTS]
    if unknown:
        raise ValueError("egc.ff 未知开关：%s（可用：%s）" % (",".join(sorted(unknown)), ",".join(sorted(_DEFAULTS))))
    return dict(_CFG)


def settings():
    return dict(_CFG)


def enabled():
    return bool(_CFG["enabled"])


def lane_tools_for(lane, overrides=None):
    """R4 的白名单：按 lane 精确匹配，`"*"` 兜底，都没有＝不裁剪。"""
    lt = _CFG.get("lane_tools") or {}
    return lt.get(lane) or lt.get("*")


# ---------------------------------------------------------------- 账本
_STATS = {"calls": 0, "passthrough": 0, "bytes_in": 0, "bytes_out": 0,
          "over_budget": 0, "invalid": 0, "ids_mapped": 0, "samples": [],
          "cpu_samples": [], "gc_samples": []}
_SAMPLES = 512


def reset_stats():
    for k in _STATS:
        _STATS[k] = [] if k.endswith("samples") else 0


def _pct(xs, q):
    if not xs:
        return 0.0
    v = sorted(xs)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))], 3)


class Wire:
    """一次出网转换的产物：字节＋反查表＋记账。"""

    __slots__ = ("body", "ids", "wire", "ms", "acct", "style", "errors", "compact")

    def __init__(self, body, ids, wire, ms, acct, style, errors=(), compact=True):
        self.body, self.ids, self.wire, self.ms = body, ids, wire, ms
        self.acct, self.style, self.errors, self.compact = acct, style, list(errors), compact

    @property
    def bytes_out(self):
        return len(self.wire) if self.wire is not None else len(legacy_wire(self.body))

    def __repr__(self):
        return "<egc.Wire %s %s %.3fms ids=%d%s>" % (
            self.style, "passthrough" if self.wire is None else "%dB" % len(self.wire),
            self.ms, len(self.ids),
            " errors=%s" % self.errors if self.errors else "")


# ---------------------------------------------------------------- 出网标准入口
def egress(body, style="openai", lane="", overrides=None, ids=None, memo=True,
           account_bytes=False):
    """**SMSocket 的出站标准动作**：物化后的 body → 出网字节。

    位置是硬约束：`dsm.materialize` / `up.build_payload` 之后、发请求之前。
    返回 `Wire`（含 `ids` 反查表——响应侧必须 `restore_ids`，否则工具循环断）。
    转换失败（校验不过）时**退回未精简的 body**并记账，绝不为省字节发出一个 400。
    """
    ff_over = dict(_CFG["ff"]); ff_over.update(overrides or {})
    on = active(ff_over).__contains__
    t0 = time.perf_counter()
    cpu0 = time.process_time() if _HAS_CPU else 0.0
    g0 = _gc_counts()[2]
    ids = ids if ids is not None else IdMap()
    acct = {"short_ids": 0, "tools_in": len(body.get("tools") or []), "tools_out": 0}
    errors: list[str] = []
    compact = bool(_CFG["enabled"])
    out = body
    if compact:
        lt = lane_tools_for(lane, ff_over)
        try:
            if style == "anthropic":
                out = compact_anthropic(body, ff_over)
            else:
                out, acct = compact_openai(body, ff_over, lane_tools=lt, ids=ids, memo=memo)
            if _CFG["validate"]:
                errors = validate(out, "anthropic" if style == "anthropic" else "openai")
                if errors:
                    out, ids, compact = body, IdMap(), False   # 退回：宁可不省，也不发坏报文
                    _STATS["invalid"] += 1
        except Exception as e:                        # 精简层绝不把请求打死，但必须留痕
            import traceback
            log.warning("egc 直通（精简失败）：%s", traceback.format_exc(limit=3))
            out, ids, compact, errors = body, IdMap(), False, ["egc 异常：%r" % e]
            _STATS["invalid"] += 1
    wire = None if not compact else serialize(out, wire_compact=on("R1_wire_compact"))
    ms = (time.perf_counter() - t0) * 1000.0
    # 归因三连：wall＝用户真正付的延迟税（门就卡它）；cpu＝本层自己的活；
    # gc＝这一趟里跑了几次回收。在线 p95 10.5ms 而干净进程同负载 p95 2.4ms，
    # 差值只可能是 GC 停顿或事件循环抢占——没有这三个数就永远只能猜。
    cpu = (time.process_time() - cpu0) * 1000.0 if _HAS_CPU else 0.0
    gcs = _gc_counts()[2] - g0
    _STATS["calls"] += 1
    if account_bytes:            # 对照字节要再序列化一次——只在自检/压测里算
        _STATS["bytes_in"] += len(legacy_wire(body))
    _STATS["bytes_out"] += len(wire) if wire is not None else len(legacy_wire(body))
    _STATS["ids_mapped"] += len(ids)
    if not compact:
        _STATS["passthrough"] += 1
    if ms > _CFG["budget_ms"]:
        _STATS["over_budget"] += 1
    s = _STATS["samples"]
    s.append(round(ms, 4))
    if len(s) > _SAMPLES:
        del s[:len(s) - _SAMPLES]
    c = _STATS["cpu_samples"]
    c.append(round(cpu, 4))
    if len(c) > _SAMPLES:
        del c[:len(c) - _SAMPLES]
    g = _STATS["gc_samples"]
    g.append(gcs)
    if len(g) > _SAMPLES:
        del g[:len(g) - _SAMPLES]
    return Wire(out, ids, wire, round(ms, 4), acct, style, errors, compact)


# ---------------------------------------------------------------- 响应侧反查（R6）
def restore_ids(obj, ids):
    """把出网短 id 换回原 id（就地）。没有映射时原样返回——幂等，可安全重复调用。

    覆盖 openai（choices/messages/delta 的 tool_calls、tool_call_id）与
    anthropic（content 里的 tool_use / tool_result id）。
    """
    if not ids or obj is None:
        return obj
    if isinstance(obj, dict):
        if isinstance(obj.get("tool_call_id"), str):        # openai: tool 结果回指
            obj["tool_call_id"] = ids.get(obj["tool_call_id"])
        for tc in obj.get("tool_calls") or []:              # openai: 助手发起的调用
            if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                tc["id"] = ids.get(tc["id"])
        if obj.get("type") in ("tool_use", "tool_result"):  # anthropic: 内容块
            if isinstance(obj.get("id"), str):
                obj["id"] = ids.get(obj["id"])
            if isinstance(obj.get("tool_use_id"), str):
                obj["tool_use_id"] = ids.get(obj["tool_use_id"])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                restore_ids(v, ids)
    elif isinstance(obj, list):
        for v in obj:
            restore_ids(v, ids)
    return obj


def state():
    """给 /healthz、面板与回归看的真值（含毫秒账）。"""
    s = _STATS["samples"]
    bin_, bout = _STATS["bytes_in"], _STATS["bytes_out"]
    return {"enabled": bool(_CFG["enabled"]), "budget_ms": _CFG["budget_ms"],
            "validate": _CFG["validate"], "profile": profile(_CFG["ff"]),
            "lanes": len(_CFG.get("lane_tools") or {}),
            "calls": _STATS["calls"], "passthrough": _STATS["passthrough"],
            "invalid": _STATS["invalid"], "over_budget": _STATS["over_budget"],
            "ms_p50": _pct(s, 0.5), "ms_p95": _pct(s, 0.95), "ms_max": round(max(s), 3) if s else 0.0,
            "cpu_p50": _pct(_STATS["cpu_samples"], 0.5),
            "cpu_p95": _pct(_STATS["cpu_samples"], 0.95),
            "gc_in_call": sum(1 for x in _STATS["gc_samples"] if x),
            "gc_pause_pct": round(100.0 * sum(1 for x in _STATS["gc_samples"] if x)
                                  / max(1, len(_STATS["gc_samples"])), 1),
            "bytes_in": bin_, "bytes_out": bout,
            "wire_saved_pct": round(100.0 * (1 - bout / bin_), 1) if bin_ else 0.0,
            "ids_mapped": _STATS["ids_mapped"]}


# ---------------------------------------------------------------- 自检（离线·真常量）
def _sample_body(tools_n=17, turns=4):
    """造一份「像真会话」的 body：CJK 重的 system（含易变行）＋工具调用往返＋17 工具。"""
    tools = [{"type": "function", "function": {
        "name": "tool_%d" % i,
        "description": "第 %d 个工具：执行一段带中文描述的动作，返回结构化结果。" % i,
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目标路径（相对工作区）"},
            "mode": {"type": "string", "enum": ["a", "b"]}},
            "required": ["path"]}}} for i in range(tools_n)]
    sys_txt = "\n".join([
        "你是网关侧的推理内核。", "规则：先思考再动手，动手必须落地。",
        "当前时间：2026-10-06 19:20:00", "session＝sess-20261006-170930",
        "约束：内部处理用英语，输出用用户语言。"] * 12)
    msgs = [{"role": "system", "content": sys_txt}]
    for t in range(turns):
        msgs.append({"role": "user", "content": "第 %d 轮：请读取配置文件并给出结论，中文回答。" % t})
        msgs.append({"role": "assistant", "content": None, "reasoning_content": "内部思考，绝不出网",
                     "tool_calls": [{"id": "call_%s_%d" % ("x" * 24, t), "type": "function",
                                     "function": {"name": "tool_1",
                                                  "arguments": json.dumps(
                                                      {"path": "配置/中文路径.json", "q": "值"},
                                                      ensure_ascii=True)}}]})
        msgs.append({"role": "tool", "tool_call_id": "call_%s_%d" % ("x" * 24, t),
                     "content": json.dumps({"ok": True, "note": "中文结果"}, ensure_ascii=True)})
    msgs.append({"role": "user", "content": "结论？"})
    return {"model": "auto", "messages": msgs, "tools": tools,
            "temperature": 1, "top_p": 1, "stream": False,
            "presence_penalty": 0, "max_tokens": 4096}


def selftest(iters=200, budget_ms=None):
    """离线自检：默认档必须**语义等价**且**转换在毫秒内**，任一不满足即返回 ok=false。

    跑法：`python -B -m SMSocket.egc selftest`。这是「出站标准」的验收门，
    也是回归里那条毫秒承诺的出处。
    """
    body = _sample_body()
    legacy = legacy_wire(body)
    strict = {k: False for k in ("R8_volatile_at_tail",)}
    w_strict = egress(body, overrides=dict(strict), memo=False, account_bytes=True)
    w_default = egress(body, memo=False, account_bytes=True)
    sig_in = semantic_signature(body)
    ok_strict = semantic_signature(w_strict.body) == sig_in
    cons_default = content_conservation(w_default.body) == content_conservation(body)
    # R6 反查：出网短 id → 响应原 id
    resp = {"choices": [{"message": {"role": "assistant", "content": "好",
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "tool_1",
                                                                  "arguments": "{}"}}]}}]}
    restore_ids(resp, w_default.ids)
    rid_ok = resp["choices"][0]["message"]["tool_calls"][0]["id"] == "call_" + "x" * 24 + "_0"
    # 内层转义税：值不变、字面 \u 消失
    inner = [tc for m in w_default.body.get("messages", [])
             for tc in (m.get("tool_calls") or [])]
    src_inner = [tc for m in body.get("messages", []) for tc in (m.get("tool_calls") or [])]
    esc_ok = bool(inner) and all(
        "\\u" not in (tc.get("function") or {}).get("arguments", "")
        and json.loads(tc["function"]["arguments"])
        == json.loads(src["function"]["arguments"])
        for tc, src in zip(inner, src_inner))
    esc_ok = esc_ok and all("\\u" not in (m.get("content") or "")
                            for m in w_default.body.get("messages", [])
                            if m.get("role") == "tool" and isinstance(m.get("content"), str))
    leak_ok = not any("reasoning_content" in m for m in w_default.body.get("messages", []))
    # 毫秒门：冷算（关前缀复用）与热算（开 R10）各测一轮
    egc_off_memo = []
    for _ in range(iters):
        egc_off_memo.append(egress(body, memo=False).ms)
    warm = []
    for _ in range(iters):
        warm.append(egress(body).ms)
    bm = budget_ms if budget_ms is not None else _CFG["budget_ms"]
    p50, p95 = _pct(warm, 0.5), _pct(warm, 0.95)
    cold95 = _pct(egc_off_memo, 0.95)
    flat = {"model": "auto", "messages": [], "tools": [{"name": "x", "parameters": {}}]}
    caught = bool(validate(flat, "openai"))
    amb = ambiguous_pairs([{"function": {"name": n}} for n in
                           ("task", "task_detail", "task_plan", "ask", "ask_user", "read", "write")])
    anth = compact_anthropic({"model": "m", "system": "S", "max_tokens": 64,
                              "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
                             {})
    return {"ok": bool(ok_strict and cons_default and w_default.compact and rid_ok and esc_ok
                       and leak_ok and caught and p95 <= bm and cold95 <= bm),
            "default_profile_strict_equivalent": ok_strict,
            "default_profile_conserves_content": cons_default,
            "ids_roundtrip": rid_ok, "inner_escapes_gone": esc_ok,
            "reasoning_leak": not leak_ok, "validate_catches_flattened_tools": caught,
            "wire_bytes_legacy": len(legacy), "wire_bytes_egc": w_default.bytes_out,
            "wire_saved_pct": round(100.0 * (1 - w_default.bytes_out / len(legacy)), 1),
            "ms_cold_p95": cold95, "ms_warm_p50": p50, "ms_warm_p95": p95, "budget_ms": bm,
            "ambiguous_pairs": ["/".join(p) for p in amb][:8],
            "anthropic_validate": validate(anth, "anthropic"),
            "tools_in_out": [w_default.acct["tools_in"], w_default.acct["tools_out"]]}


if __name__ == "__main__":
    import sys

    print(json.dumps(selftest(), ensure_ascii=False, indent=1))
    sys.exit(0 if selftest()["ok"] else 1)
