# ===== server-only: 会话态（delta 累积）+ 响应信封（三分账） =====
from collections import OrderedDict

STATE_MAX_SESSIONS = 2000
STATE_TTL = 3600.0


class StateMismatch(Exception):
    """客户端水位与服务端会话态不符——绝不猜前缀（宁可让客户端降级 legacy 全量重发）。"""


def _now():
    import time
    return time.time()


class SessionStore:
    """按 sid|cid|lane|sch 累积 d（客户端只发新增轮次·契约 §3/§10）。

    判定唯一依据是客户端送来的 x.sms.delta_from（它自己 delta_window 的起始下标）：
      · == 已存条数 → 正常增量，追加；
      · == 0        → 客户端全量重同步，整表替换；
      · 其他        → 水位不符 → StateMismatch（路由回 409，客户端按 fallback 降级 legacy，
                      绝不静默少发历史——语义保真优先于省字节，与客户端 delta_window 同口径）。
    落盘＝<data>/runtime/dsm_sessions.json（重启不丢·原子写）。
    """

    DEBOUNCE_S = 10.0             # 落盘去抖：高频 apply() 合并成一次原子写

    def __init__(self, path=None):
        self.path = path or os.path.join(sms_home(), "runtime", "dsm_sessions.json")
        self._lock = threading.Lock()
        self._mem: "OrderedDict[str, dict]" = OrderedDict()
        self._loaded = False
        self._dirty = False
        self._last_flush = 0.0

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        doc = atomic_io.rjson(self.path, default={}) or {}
        cut = _now() - STATE_TTL
        for k, v in (doc.get("sessions") or {}).items():
            if isinstance(v, dict) and float(v.get("ts") or 0) > cut and isinstance(v.get("d"), list):
                self._mem[k] = v

    def key(self, env):
        x = env.get("x") or {}
        return "%s|%s|%s|%s" % (env.get("sid") or "-", env.get("cid") or "-",
                                env.get("lane") or "-", env.get("sch") or "-")

    def apply(self, env):
        """回该信封应物化的完整 d（累积后的历史）。"""
        with self._lock:
            self._load()
            k = self.key(env)
            d = list(env.get("d") or [])
            frm = (env.get("x") or {}).get("sms.delta_from")
            cur = self._mem.get(k)
            n = len(cur["d"]) if cur else 0
            if frm is None:
                # 老客户端不带水位：只能按「全量」处理（不做累积，语义无损）
                full = d
            elif int(frm) == 0:
                full = d
            elif int(frm) == n:
                full = (cur["d"] if cur else []) + d
            else:
                raise StateMismatch("delta_from=%s 但服务端已存 %d 轮（key=%s）" % (frm, n, k))
            self._mem[k] = {"d": full, "ts": _now()}
            self._mem.move_to_end(k)
            while len(self._mem) > STATE_MAX_SESSIONS:
                self._mem.popitem(last=False)
            self._mark_dirty()
            return full

    def reset(self, env):
        with self._lock:
            self._mem.pop(self.key(env), None)
            self._mark_dirty()

    def _mark_dirty(self):
        self._dirty = True
        now = _now()
        # 去抖：距上次落盘 ≥ DEBOUNCE_S 才真正写盘，其余只标脏
        if now - self._last_flush >= self.DEBOUNCE_S:
            self._flush()

    def _flush(self):
        """真正原子写盘（调用方持锁）。强制出口＝close()/退出钩子。"""
        try:
            atomic_io.wjson(self.path, {"ts": int(_now()),
                                        "sessions": {k: v for k, v in self._mem.items()}})
            self._dirty = False
            self._last_flush = _now()
        except Exception as e:
            log.warning("dsm session state persist failed: %s", e)

    def _save(self):
        """兼容旧调用点：按去抖节奏决定真正写盘还是只标脏。"""
        self._mark_dirty()

    def _close(self):
        self.close()

    def close(self):
        """退出/热重载收口：把脏水位一次性落盘。"""
        with self._lock:
            if self._dirty:
                self._flush()

    def count(self):
        with self._lock:
            self._load()
            return len(self._mem)


_sessions: SessionStore | None = None
_schemas: SchemaStore | None = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = SessionStore()
    return _sessions


def schemas():
    global _schemas
    if _schemas is None:
        _schemas = SchemaStore()
    return _schemas

def _split_usage(u: dict) -> dict:
    """OpenAI usage → DSM 三分账（缺字段一律 0，绝不猜）。"""
    u = u or {}
    pin = int(u.get("prompt_tokens", 0) or 0)
    pout = int(u.get("completion_tokens", 0) or 0)
    cd = u.get("completion_tokens_details") or {}
    pd = u.get("prompt_tokens_details") or {}
    rz = int(cd.get("reasoning_tokens", u.get("reasoning_tokens", 0)) or 0)
    ans = max(0, pout - rz)
    return {"in": pin, "out_reason": rz, "out_answer": ans,
            "cache_read": int(pd.get("cached_tokens", u.get("cache_read", 0)) or 0),
            "cache_write": int(pd.get("cache_creation_input_tokens",
                                      u.get("cache_write", 0)) or 0),
            "total": int(u.get("total_tokens", pin + pout) or 0)}


def encode_response(body: dict, env: dict, alias: str = "", egress: str = "",
                    cost: dict | None = None, seq: int = 1) -> dict:
    """提供商 OpenAI 形状 → DSM 响应信封（契约 §4·decode_to_openai_shape 的逆）。

    answer：纯文本＝字符串；带 tool_calls＝[{...原始 function 结构}] 数组（客户端
    _norm_tool_call 精确还原，工具循环零改动＝验收 B 的前提）。
    usage 三分账在此产出——这是「先有度量再谈优化」的落点（规范 §8）。
    """
    ch = ((body or {}).get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    tcs = msg.get("tool_calls") or []
    txt = msg.get("content") or ""
    reason = msg.get("reasoning_content") or msg.get("reasoning") or ""
    fin = str(ch.get("finish_reason") or "")
    if tcs:
        stop = "tool_call"
        answer = [tc for tc in tcs]
    elif fin == "length":
        stop, answer = "length", txt
    else:
        stop, answer = "end_turn", txt
    u = _split_usage(body.get("usage"))
    env = env or {}
    out = {"v": 1, "seq": int(seq), "stop": stop, "answer": answer, "usage": u,
           "reason": {"tokens": u["out_reason"], "summary": reason if isinstance(reason, str) else ""}}
    for k in ("sid", "lane"):
        if env.get(k):
            out[k] = env[k]
    if alias:
        out["model"] = alias
    if egress:
        out["egress"] = egress
    if cost:
        u["cost"] = cost.get("amount")
        u["currency"] = cost.get("currency")
        out["bill"] = {"display": cost.get("display"),
                       "display_currency": cost.get("display_currency")}
    return out


def materialize(env: dict, alias: str = "", style: str = "openai-chat",
                store: SchemaStore | None = None, sess: SessionStore | None = None,
                require_registered: bool = False, use_session: bool = True) -> dict:
    """信封 → 提供商指定 body（出口翻译·策略键永不入 body，leak_check 兜底）。

    require_registered=true 且引用未登记 → Unsupported（契约：宁可拒绝也不发半截 SYS）。
    use_session=False：调用方已自备完整 d —— fan 干跑/只读检视绝不推进水位，
    否则一次预览就把会话态污染成「已收到这些轮次」。
    """
    store = store or schemas()
    if require_registered and not store.has(env.get("sch")) and not (env.get("x") or {}).get("sms.inline_schema"):
        raise Unsupported("sch %s 未登记（dsm.require_registered_schema=true）" % env.get("sch"))
    full_env = dict(env)
    if use_session:
        full_env["d"] = (sess or sessions()).apply(env)
    if style in ("anthropic",):
        body = to_anthropic(full_env, store, alias)
    elif style in ("openai-responses", RESPONSES_STYLE):
        body = to_responses(full_env, store, alias)
    else:
        body = to_openai(full_env, store, alias)
    leak = leak_check(full_env, body)
    if leak:
        raise Unsupported("策略键泄漏：%s" % ",".join(leak))
    return body


def register_schema(ref: str, system: str, tools: list, store: SchemaStore | None = None) -> dict:
    """首帧登记（POST /v1/dsm/schema·幂等）。ref 必须与内容指纹一致，否则拒登。"""
    store = store or schemas()
    want = fingerprint(system, tools)
    if ref and ref != want:
        raise Unsupported("ref 与内容不符：送 %s 算得 %s" % (ref, want))
    h = store.put(system or "", tools or [])
    return {"ref": h, "bytes": (store.get(h) or {}).get("bytes", 0), "stored": store.refs()}


def schema_of(ref: str, store: SchemaStore | None = None) -> dict | None:
    return (store or schemas()).get(ref)


_settings_dsm = None


def bind_settings(dsm_cfg) -> None:
    """记住服务端开关（healthz 读它，避免每次探测都去碰 Settings）。"""
    global _settings_dsm
    _settings_dsm = dsm_cfg


def configure(schema_path=None, session_path=None, budget_map=None):
    """服务端启动/热重载接线：把 Settings.dsm 的路径与档位表落到两个 store 上。

    路径来自 dsm.schema_store（abs_path 解析），会话态与引用同目录，一行配置管
    两个文件；budget_map 覆盖 REASON_TIER（客户端默认 low=0 是「不花思考预算」，
    服务端可按提供商实际能力抬高）。
    """
    global _sessions, _schemas
    if schema_path:
        _schemas = SchemaStore(str(schema_path))
    if session_path:
        _sessions = SessionStore(str(session_path))
    if isinstance(budget_map, dict):
        for k, v in budget_map.items():
            kk = str(k).strip().lower()
            if kk in REASON_TIER:
                try:
                    REASON_TIER[kk] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
    return {"schemas": str((_schemas or SchemaStore()).path),
            "sessions": str((_sessions or SessionStore()).path),
            "budget_map": dict(REASON_TIER)}


def state() -> dict:
    """可观测性（契约 §5：不看配置文件就能回答「DSM 到底开没开」）。"""
    return {"enabled": bool(_settings_dsm and _settings_dsm.enabled),
            "openai_compat": bool(not _settings_dsm or _settings_dsm.openai_compat),
            "schemas": _schemas.refs() if _schemas is not None else 0,
            "sessions": _sessions.count() if _sessions is not None else 0,
            "mem_unsupported": _mem_unsupported,
            "budget_map": dict(REASON_TIER)}
