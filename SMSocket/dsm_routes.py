"""DSM v1 routes: serve the envelope next to legacy OpenAI (contract §1/§2).

  POST /v1/dsm/chat     DSM envelope in, DSM response envelope out
  POST /v1/dsm/schema   first-frame registration {ref, system, tools} (idempotent)
  GET  /v1/dsm/schema   ?ref=sha1:...  inspect a stored schema (debug)
  POST /v1/dsm/fan      split a fan envelope into lane requests (dry-run unless run=1)

Mounted only while `dsm.enabled` — an absent route answers 404, which is exactly
the signal the client uses to downgrade to legacy (§1). Content-Type must be
application/dsm+json or the call is refused with 415 (the other downgrade code).

Materialisation always lands on the openai-chat shape, because that is the
gateway's internal lingua franca: router.complete -> upstream.build_payload then
translates to whatever the picked provider actually speaks (anthropic /
responses). Reusing that path means DSM adds no second provider-translation
implementation to keep in sync.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import dsm
from .providers import is_auto_alias, resolve_auto_alias
from .router import NoUpstream, UpstreamError

log = logging.getLogger("smssocket")


def _empty_env(env: dict) -> bool:
    """DSM 响应信封是否「200 但什么都没有」——正文/工具调用/思考回放三者全空。

    这正是 SMS 侧「换 DSM 返回空响应」的形态（上游只吐 reasoning 就被截断，或
    _dsm_stream 收口帧 answer=""）。路由不改状态码（契约 §4 仍是 200·客户端已改为
    loud 降级 legacy），但必须在服务端日志里留一行：否则下次报障只剩一片 200。
    """
    e = env or {}
    a = e.get("answer")
    if isinstance(a, str):
        if a.strip():
            return False
    elif a:
        return False
    r = e.get("reason") or {}
    if isinstance(r, str):
        if r.strip():
            return False
    elif isinstance(r, dict) and str(r.get("summary") or "").strip():
        return False
    return True


def build(st, dep, egress_hdr=None) -> APIRouter:
    """Mounted by gateway; `dep` = master-key dependency list,
    `egress_hdr` = gateway's egress_headers() closure (keeps the net-plane rule
    of "no new headers while clash is disabled" in one place)."""
    r = APIRouter(dependencies=dep)
    hdr = egress_hdr or (lambda e: {})

    def need() -> None:
        if not st.settings.dsm.enabled:
            raise HTTPException(404, detail={"error": {
                "message": "dsm is disabled (set dsm.enabled: true)",
                "type": "dsm_disabled"}})

    def bad(code: int, msg: str, typ: str) -> HTTPException:
        return HTTPException(code, detail={"error": {"message": msg, "type": typ}})

    async def envelope(req: Request) -> dict:
        need()
        if dsm.CTYPE not in (req.headers.get("content-type") or ""):
            raise bad(415, f"Content-Type 必须是 {dsm.CTYPE}", "unsupported_media_type")
        try:
            env = await req.json()
        except Exception as e:
            raise bad(400, f"body 不是 JSON：{e}", "invalid_request_error")
        errs = dsm.validate(env)
        if errs:
            raise bad(422, "DSM 信封校验失败：" + "；".join(errs[:6]), "invalid_dsm_envelope")
        return env

    def alias_of(env: dict) -> str:
        return str((env.get("x") or {}).get("sms.model") or "")

    def attribution(env: dict) -> dict:
        return {"sid": env.get("sid") or "", "cid": env.get("cid") or "",
                "lane": env.get("lane") or "",
                "skill": str((env.get("bill") or {}).get("to") or "")}

    def cost_of(alias: str, usage: dict) -> dict:
        u = usage or {}
        return st.settings.cost_detail(
            alias, int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0))

    def materialise(env: dict, alias: str) -> dict:
        try:
            body = dsm.materialize(env, alias=alias, style="openai-chat",
                                   require_registered=st.settings.dsm.require_registered_schema)
        except dsm.StateMismatch as e:
            # 水位不符：绝不发半截历史 —— 让客户端按 fallback 走 legacy 全量重发
            raise bad(409, str(e), "dsm_state_mismatch")
        except dsm.Unsupported as e:
            raise bad(422, str(e), "dsm_materialize_failed")
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        cap = st.settings.dsm.max_materialize_bytes
        if len(raw) > cap:
            raise bad(413, f"materialised body {len(raw)} B > dsm.max_materialize_bytes {cap}",
                      "dsm_too_large")
        return body

    @r.post("/v1/dsm/chat")
    async def chat(request: Request):
        env = await envelope(request)
        alias = alias_of(env)
        if not alias:
            raise bad(400, "x['sms.model'] is required (model alias)", "invalid_dsm_envelope")
        idx = st.settings.model_index()
        if alias not in idx and not is_auto_alias(alias):
            raise bad(404, f"unknown model '{alias}'", "invalid_request_error")
        # Resolve auto-aliases before materialisation: the pool ranks providers by
        # effective priority/weight and cooldown, so `auto` is not the first alias
        # of the first provider in config.yaml.
        if is_auto_alias(alias):
            alias = resolve_auto_alias(st.pool, st.settings.strategy, alias)
            if not alias:
                raise bad(503, "no models available", "server_error")
        body = materialise(env, alias)
        dmt = st.settings.dsm.default_max_tokens
        if dmt and not body.get("max_tokens") and not body.get("max_completion_tokens"):
            body["max_tokens"] = dmt          # config 兜底：客户端未限时不被提供商低默认截断
        out: dict = {}
        if (env.get("out") or "json") == "delta":
            body["stream"] = True
            slot, alias2, t0, resp = await _open(body, env)
            h = {"x-socket-upstream": slot.provider.name, **hdr(getattr(resp, "_sms_egress", ""))}
            return StreamingResponse(_dsm_stream(env, alias2, t0, slot, resp),
                                     media_type=dsm.CTYPE_STREAM, headers=h)
        try:
            chat_body = await st.router.complete(body, egress_out=out, dsm=attribution(env))
        except NoUpstream as e:
            raise bad(503, str(e), "server_error")
        except UpstreamError as e:
            raise bad(e.status, str(e.detail), "upstream_error")
        alias = str(chat_body.get("model") or alias)   # smsocket 自动路由：计费/归因用解析后的真实模型
        resp_env = dsm.encode_response(chat_body, env, alias=alias,
                                       egress=out.get("egress", ""),
                                       cost=cost_of(alias, chat_body.get("usage")))
        if _empty_env(resp_env):
            log.warning("DSM 200 empty envelope (json) sid=%s cid=%s alias=%s egress=%s"
                        " —— SMS 侧会 loud 降级 legacy（见 gateway._dsm_chat 空信封守卫）",
                        env.get("sid"), env.get("cid"), alias, out.get("egress", ""))
        return JSONResponse(resp_env, media_type=dsm.CTYPE,
                            headers={"x-dsm-sch": str(env.get("sch") or ""),
                                     "x-dsm-mem-unsupported": str(dsm.mem_unsupported())})

    async def _open(body: dict, env: dict):
        try:
            return await st.router.open_stream(body, dsm=attribution(env))
        except NoUpstream as e:
            raise bad(503, str(e), "server_error")
        except UpstreamError as e:
            raise bad(e.status, str(e.detail), "upstream_error")

    async def _dsm_stream(env, alias, t0, slot, resp):
        """out=delta：按 seq 吐 ndjson 信封行（消解「每输出 token 背 63-83 B SSE 信封」）。

        三类增量都必须带上，否则 delta 模式比 legacy 少东西（2026-10-06 实测缺陷）：
          · content          → 逐帧 `answer`（客户端按 seq 拼接上屏）
          · reasoning_content→ 帧内 `reason.summary`（思考回放；只发 content 会让 SMS
            的 ◌ 推理信封整条消失，观感与归因一起退化）
          · tool_calls       → 按 index 累积，**收口帧**整体带出（增量分片对模型无意义，
            对工具循环是必需品；拆成多帧只会让客户端 merge 时丢形状）
        finish_reason 也收口帧如实带（length/tool_calls/stop 不能一律写成 stop）。
        """
        seq, usage, finish = 0, {}, ""
        tcs: dict[int, dict] = {}
        async for chunk in st.router.wrap_stream(slot, alias, t0, resp):
            line = chunk.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            ch = (obj.get("choices") or [{}])[0]
            finish = ch.get("finish_reason") or finish
            de = ch.get("delta") or {}
            piece = de.get("content") or ""
            rz = de.get("reasoning_content") or ""
            for t in de.get("tool_calls") or []:
                e = tcs.setdefault(int(t.get("index") or 0),
                                   {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                f = t.get("function") or {}
                e["id"] = e["id"] or (t.get("id") or "")
                e["function"]["name"] += f.get("name") or ""
                e["function"]["arguments"] += f.get("arguments") or ""
            if not piece and not rz:
                continue
            seq += 1
            frame = {"v": 1, "seq": seq, "answer": piece, "stop": None}
            if rz:
                frame["reason"] = {"tokens": 0, "summary": rz}
            for k in ("sid", "lane"):
                if env.get(k):
                    frame[k] = env[k]
            yield json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"
        # 收口帧只带 usage/stop/工具调用：正文已按 seq 逐帧吐过，末帧再带一遍全文会被
        # 客户端 merge_stream 拼成「4242」——增量协议的最后一帧必须是元数据，不是正文。
        answer = [tcs[i] for i in sorted(tcs)] if tcs else ""
        final = dsm.encode_response(
            {"choices": [{"message": {"role": "assistant", "content": "",
                                      "tool_calls": answer or None},
                          "finish_reason": finish or "stop"}], "usage": usage},
            env, alias=alias, egress=getattr(resp, "_sms_egress", ""),
            cost=cost_of(alias, usage), seq=seq + 1)
        final["answer"] = answer
        if _empty_env(final) and not seq:
            log.warning("DSM 200 empty envelope (delta) sid=%s cid=%s alias=%s frames=0"
                        " —— 上游一个 token 都没吐，SMS 侧会 loud 降级 legacy",
                        env.get("sid"), env.get("cid"), alias)
        yield json.dumps(final, ensure_ascii=False).encode("utf-8") + b"\n"

    @r.post("/v1/dsm/schema")
    async def schema_put(request: Request):
        need()
        if dsm.CTYPE not in (request.headers.get("content-type") or ""):
            raise bad(415, f"Content-Type 必须是 {dsm.CTYPE}", "unsupported_media_type")
        raw = await request.json()
        if not isinstance(raw, dict):
            raise bad(400, "body must be an object", "invalid_request_error")
        try:
            info = dsm.register_schema(str(raw.get("ref") or ""),
                                        str(raw.get("system") or ""),
                                        list(raw.get("tools") or []))
            dsm.schemas().flush()   # 首帧登记后批量落盘：重启后引用真源不丢
        except dsm.Unsupported as e:
            raise bad(422, str(e), "dsm_schema_mismatch")
        return JSONResponse({"accepted": True, **info}, media_type=dsm.CTYPE)

    @r.get("/v1/dsm/schema")
    async def schema_get(ref: str = Query(...)):
        need()
        rec = dsm.schema_of(ref)
        if rec is None:
            raise bad(404, f"unknown ref {ref}", "invalid_request_error")
        return JSONResponse({"ref": ref, "system": rec.get("system", ""),
                             "tools": rec.get("tools", []), "bytes": rec.get("bytes", 0)},
                            media_type=dsm.CTYPE)

    @r.post("/v1/dsm/fan")
    async def fan(request: Request, run: int = Query(0)):
        """拆分是纯函数（默认 dry-run 只回 requests[]）；run=1 才真投 /v1/batch 执行体。"""
        env = await envelope(request)
        if not env.get("fan"):
            raise bad(422, "信封没有 fan 声明", "invalid_dsm_envelope")
        done = {str(x) for x in (env.get("x") or {}).get("sms.done_lanes") or []}
        plan = dsm.split_fan(env, done_lanes=done)
        alias = alias_of(env)
        if is_auto_alias(alias):
            alias = resolve_auto_alias(st.pool, st.settings.strategy, alias) or alias
        bodies, errs = [], []
        for sub in plan["requests"]:
            try:
                bodies.append(dsm.materialize(sub, alias=alias, style="openai-chat",
                                            use_session=bool(run)))
            except Exception as e:
                errs.append(f"{sub.get('lane')}: {e}")
        plan["requests_materialised"] = bodies
        plan["errors"] = errs
        if not run:
            return JSONResponse(plan, media_type=dsm.CTYPE)
        if not plan["dispatchable"]:
            raise bad(409, "fan 无可派发道（dep 未满足）：" + ",".join(plan["blocked"]),
                      "dsm_fan_blocked")
        if not bodies:
            raise bad(422, "所有道物化失败：" + "; ".join(errs)[:200], "dsm_materialize_failed")
        job = st.batch.start(st.router, bodies, plan["concurrency"], plan["fail_fast"])
        return JSONResponse({"job": job.id, "total": job.total, "lanes": plan["ready"],
                             "merge": plan["merge"], "poll": f"/v1/batches/{job.id}"},
                            status_code=202, media_type=dsm.CTYPE)

    return r
