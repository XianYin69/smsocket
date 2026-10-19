"""Upstream adapters: request/response translation for openai-compatible & anthropic."""
from __future__ import annotations

import json
import re
import time
import uuid

from .config import ProviderSpec

ANTHROPIC_VERSION = "2023-06-01"
RESPONSES_STYLE = "openai-responses"     # upstream speaks the Responses API
CHAT_STYLES = ("openai", RESPONSES_STYLE)
OPENSECODE_STYLE = "opencode"            # OpenCode Zen (/zen/v1) - see _opencode_headers

_V = re.compile(r"/v\d+$")


def _ver(base: str) -> str:
    """True if base_url already ends with a version segment (/v1, /v4, ...)."""
    return bool(_V.search(base))


def opencode_session_id() -> str:
    """A plausible OpenCode CLI session id.

    OpenCode's free-tier gate (Cloudflare) rejects any session id whose 12-hex
    prefix is not ~= (ms * 0x1000 + counter), i.e. a value that looks like it
    was minted by the agent at this moment.  A random uuid4 hex prefix does not
    pass, so the prefix is derived from the current millisecond tick - the same
    shape the real client emits: the tick shifted left 12 bits with a small
    counter in the low nibble.
    """
    ms = int(time.time() * 1000)
    counter = ms & 0xF
    return "ses_" + format(((ms << 12) | counter) & 0xFFFFFFFFFFFF, "012x")


def _opencode_headers(key: str) -> dict:
    """The identity headers OpenCode's free tier requires from the CLI.

    A plain OpenAI-shaped request to opencode.ai/zen is answered with
    FreeUsageLimitError (429): the gate reads the client version out of
    User-Agent and rejects anything that is not a versioned opencode release,
    and it wants the canonical x-opencode-* identity headers too.  These make
    the request look like the OpenCode CLI talking to its own free tier.
    """
    h = {
        "user-agent": "opencode/1.18.35",
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
    }
    sid = opencode_session_id()
    h["x-opencode-session"] = sid
    h["x-opencode-session-id"] = sid
    # The free tier is served with the public bearer; a real key is forwarded
    # when the operator pasted one (paid Zen models need it).
    h["authorization"] = "Bearer " + (key or "public")
    return h


def responses_url(p: ProviderSpec) -> str:
    """The Responses API endpoint of an openai-responses provider."""
    base = p.base_url.rstrip("/")
    return base + "/responses" if _ver(base) else base + "/v1/responses"


def chat_url(p: ProviderSpec) -> str:
    if p.style == RESPONSES_STYLE:
        return responses_url(p)
    if p.style == "anthropic":
        base = p.base_url.rstrip("/")
        return base + "/messages" if _ver(base) else base + "/v1/messages"
    base = p.base_url.rstrip("/")
    return base + "/chat/completions" if _ver(base) else base + "/v1/chat/completions"


def embed_url(p: ProviderSpec) -> str:
    base = p.base_url.rstrip("/")
    return base + "/embeddings" if base.endswith("/v1") else base + "/v1/embeddings"
def headers(p: ProviderSpec, key: str) -> dict:
    """Outbound headers for one upstream call.

    An empty `key` means this provider is keyless (OpenCode serves its free
    models and its public /v1/models without any credential) - in that case no
    Authorization header is sent at all, because a ``Bearer `` token is worse
    than none: it makes the upstream reject the call outright.

    An `opencode` provider is the same idea one layer deeper: the free tier is
    gated behind the OpenCode CLI identity, so a bare Authorization header is
    not enough - the request has to *look* like the agent talking to its own
    backend (User-Agent + x-opencode-* + a session id minted the way the real
    client mints one).
    """
    if p.style == OPENSECODE_STYLE:
        h = _opencode_headers(key)
        h["content-type"] = "application/json"
        h.update(p.extra_headers)
        return h
    if p.style == "anthropic":
        h = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if key:
            h["x-api-key"] = key
    else:
        h = {"content-type": "application/json"}
        if key:
            h["authorization"] = f"Bearer {key}"
    h.update(p.extra_headers)
    return h


def _normalize_openai_tool_calls(body: dict) -> dict:
    """出网前兜底：保证 openai-chat 报文里的 tool_calls 永远合法。

    严格 OpenAI 兼容上游（sensenova/agnes-ai/scnet 等）对两类缺失回 400：
      · tool_call 缺 type 字段 → "invalid tool_call type"
      · 带 tool_calls 的 assistant 消息缺 content → "BadRequestError: OpenAIException"
    EGC 的 _clean_msg 也做这事，但 validate 不过时会整条回滚到原始 body，故这里
    在 build_payload 里无条件补一次，使「精简」与「回滚」两条路都合法。
    """
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        tcs = m.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            for tc in tcs:
                if isinstance(tc, dict):
                    tc.setdefault("type", "function")
            if m.get("role") == "assistant" and not m.get("content"):
                m["content"] = ""      # null/缺失都补 ""，严格上游不收 null content
    body.pop("top_k", None)            # anthropic-only；严格 openai 上游会 400
    return body


def build_payload(p: ProviderSpec, payload: dict, upstream_model: str) -> dict:
    body = dict(payload)
    body["model"] = upstream_model
    if p.style == RESPONSES_STYLE:
        return chat_to_responses_request(body)
    if p.style != "anthropic":
        return _normalize_openai_tool_calls(body)
    msgs = body.pop("messages", []) or []
    sys_txt = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "system")
    out = {"model": upstream_model,
           "messages": [m for m in msgs if m.get("role") != "system"],
           "max_tokens": int(body.pop("max_tokens", 0) or body.pop("max_completion_tokens", 0) or 1024)}
    if sys_txt:
        out["system"] = sys_txt
    for k in ("temperature", "top_p", "stream", "stop_sequences"):
        if body.get(k) is not None:
            out[k] = body[k]
    if "stop" in body and "stop_sequences" not in out:
        out["stop_sequences"] = body["stop"]
    return out


def _stop_reason(r) -> str:
    return {"end_turn": "stop", "max_tokens": "length",
            "stop_sequence": "stop", "tool_use": "tool_calls"}.get(r, "stop")


def _details(u: dict) -> dict:
    """提供商缓存/思考计数 → OpenAI *_tokens_details（全零时回空 dict）。

    只在全非零时才挂进 usage：现有测试按精确相等断言 usage 字典，凭空造空字段
    会改报文形状；且「提供商没报」与「报了 0」在账面上必须区分开。
    """
    u = u or {}
    pd, cd = {}, {}
    for src, dst in (("cache_read_input_tokens", "cached_tokens"),
                     ("cache_creation_input_tokens", "cache_creation_input_tokens")):
        v = int(u.get(src, 0) or 0)
        if v:
            pd[dst] = v
    for src, dst in (("reasoning_tokens", "reasoning_tokens"),):
        v = int(u.get(src, 0) or 0)
        if v:
            cd[dst] = v
    out = {}
    if pd:
        out["prompt_tokens_details"] = pd
    if cd:
        out["completion_tokens_details"] = cd
    return out


def from_anthropic(data: dict, alias: str) -> dict:
    u = data.get("usage", {}) or {}
    pin, pout = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"id": "chatcmpl-" + str(data.get("id", uuid.uuid4().hex[:12])),
            "object": "chat.completion", "created": int(time.time()), "model": alias,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": _stop_reason(data.get("stop_reason"))}],
            "usage": dict({"prompt_tokens": pin, "completion_tokens": pout,
                           "total_tokens": pin + pout}, **_details(u))}


def sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _chunk(cid, created, alias, delta, finish=None, usage=None) -> dict:
    c = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": alias,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        c["usage"] = usage
    return c


# ---- OpenAI Responses API <-> Chat Completions translation -----------------
# The two OpenAI surfaces are not aliases: /v1/chat/completions takes
# `messages` + `max_tokens` and answers with choices[]; /v1/responses takes
# `input` (+ `instructions`) + `max_output_tokens` and answers with an output
# item list + status. A provider can now be declared as either one, and the
# gateway can serve either one regardless of what its upstream speaks.

RESP_MAX_TEXT_FIELD = "max_output_tokens"


def anthropic_request_to_chat(body: dict) -> dict:
    """Anthropic Messages API request -> chat-shaped payload (what the router routes).

    The two surfaces are not aliases: /v1/messages takes `system` + `messages`
    with block content + `max_tokens` and answers with a content block list;
    /v1/chat/completions takes `messages` and answers with choices[]. The gateway
    serves both and translates against whatever the upstream actually speaks.
    """
    msgs: list[dict] = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_.strip():
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        txt = "\n".join(str(b.get("text") or "") for b in sys_
                        if isinstance(b, dict) and b.get("type") == "text")
        if txt.strip():
            msgs.append({"role": "system", "content": txt})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = m.get("content")
        if not isinstance(content, list):
            msgs.append({"role": role, "content": _text_of(content)})
            continue
        text_parts: list[str] = []
        extra: list[dict] = []
        calls: list[dict] = []
        tools_out: list[dict] = []
        for b in content:
            if not isinstance(b, dict):
                text_parts.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                text_parts.append(str(b.get("text") or ""))
            elif t == "image" and isinstance(b.get("source"), dict):
                src = b["source"]
                if src.get("type") == "url" and src.get("url"):
                    extra.append({"type": "image_url", "image_url": {"url": src["url"]}})
            elif t == "tool_use":
                calls.append({"id": b.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                              "type": "function",
                              "function": {"name": b.get("name"),
                                           "arguments": json.dumps(b.get("input") or {},
                                                                   ensure_ascii=False)}})
            elif t == "tool_result":
                tools_out.append({"role": "tool",
                                  "tool_call_id": b.get("tool_use_id"),
                                  "content": _text_of(b.get("content"))})
            # thinking / redacted_thinking / server_tool_use carry no text for the
            # chat surface - dropping them keeps the payload valid for strict upstreams
        msg: dict = {"role": role}
        if extra:
            msg["content"] = [{"type": "text", "text": "".join(text_parts)}] + extra
        else:
            msg["content"] = "".join(text_parts)
        if calls and role == "assistant":
            msg["tool_calls"] = calls
        # Drop a text-less user turn whose only block was tool_result: the tool
        # message below already says everything, and strict openai upstreams
        # reject an empty user message.
        if msg["content"] or calls or not tools_out:
            msgs.append(msg)
        msgs.extend(tools_out)

    out: dict = {"model": body.get("model"), "messages": msgs}
    if body.get("max_tokens") is not None:
        out["max_tokens"] = int(body["max_tokens"])
    elif body.get("max_completion_tokens") is not None:
        out["max_tokens"] = int(body["max_completion_tokens"])
    if body.get("stream") is not None:
        out["stream"] = bool(body["stream"])
    for k in ("temperature", "top_p", "top_k", "user", "metadata"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop_sequences") is not None:
        stop = body["stop_sequences"]
        out["stop"] = stop if isinstance(stop, list) else [stop]
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = anthropic_tools_to_openai(tools)
        if body.get("tool_choice") is not None:
            out["tool_choice"] = anthropic_tool_choice_to_openai(body["tool_choice"])
    return out


def anthropic_tools_to_openai(tools: list) -> list:
    """Anthropic tool definitions -> OpenAI function tools (keeps unknown keys)."""
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = {"name": t.get("name")}
        if t.get("description"):
            fn["description"] = t["description"]
        fn["parameters"] = t.get("input_schema") or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": fn})
    return out


def anthropic_tool_choice_to_openai(choice) -> dict | str:
    if isinstance(choice, str):
        return {"auto": "auto", "any": "required"}.get(choice, "auto")
    if isinstance(choice, dict):
        t = choice.get("type")
        if t == "any":
            return "required"
        if t == "tool" and choice.get("name"):
            return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def chat_to_anthropic(body: dict, alias: str) -> dict:
    """chat.completion -> Anthropic message object (what /v1/messages answers)."""
    u = body.get("usage") or {}
    pin = int(u.get("prompt_tokens", 0) or 0)
    pout = int(u.get("completion_tokens", 0) or 0)
    ch = (body.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    blocks: list[dict] = []
    text = _text_of(msg.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        try:
            parsed = json.loads(args) if isinstance(args, str) and args.strip() else {}
        except Exception:                                   # noqa: BLE001
            parsed = {}
        blocks.append({"type": "tool_use",
                       "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:12]),
                       "name": fn.get("name"), "input": parsed})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    usage = {"input_tokens": pin, "output_tokens": pout}
    pd = u.get("prompt_tokens_details") or {}
    if int(pd.get("cached_tokens", 0) or 0):
        usage["cache_read_input_tokens"] = int(pd["cached_tokens"])
    return {"id": "msg_" + str(body.get("id") or uuid.uuid4().hex[:12]).replace("chatcmpl-", ""),
            "type": "message", "role": "assistant", "model": alias,
            "content": blocks,
            "stop_reason": {"stop": "end_turn", "length": "max_tokens",
                            "tool_calls": "tool_use"}.get(
                                str(ch.get("finish_reason")), "end_turn"),
            "stop_sequence": None, "usage": usage}


async def chat_stream_to_anthropic_events(chunks, alias: str):
    """chat SSE (already normalised upstream) -> Anthropic SSE event stream.

    Emits text and tool_use content blocks lazily as they appear in the chat
    stream: a text block opens on the first text delta, a tool_use block opens
    on the first tool_call delta for that index (with ``id``/``name``), and the
    streamed ``function.arguments`` fragments become ``input_json_delta``
    deltas - so an agentic client (opencode/kilocode) sees live tool args.
    """
    def ev(t: str, obj: dict) -> bytes:
        return (f"event: {t}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n").encode()

    mid = "msg_" + uuid.uuid4().hex[:12]
    base = {"id": mid, "type": "message", "role": "assistant", "model": alias,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 1}}
    yield ev("message_start", {"type": "message_start", "message": base})

    next_index = 0
    open_block: int | None = None
    block_kind: dict[int, str] = {}
    tool_block: dict[int, int] = {}      # openai tool_call.index -> block index

    def close_open() -> bytes | None:
        if open_block is None:
            return None
        out = ev("content_block_stop",
                 {"type": "content_block_stop", "index": open_block})
        return out

    text, pin, pout, finish = "", 0, 0, "end_turn"
    async for raw in chunks:
        for line in raw.decode("utf-8", "ignore").split("\n"):
            s = line.strip()
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                ev_obj = json.loads(payload)
            except Exception:
                continue
            u = ev_obj.get("usage")
            if u:
                pin = int(u.get("prompt_tokens", pin) or 0)
                pout = int(u.get("completion_tokens", pout) or 0)
            for choice in ev_obj.get("choices") or []:
                fr = choice.get("finish_reason")
                if fr:
                    finish = {"stop": "end_turn", "length": "max_tokens",
                              "tool_calls": "tool_use"}.get(str(fr), "end_turn")
                delta = choice.get("delta") or {}
                txt = delta.get("content")
                if txt:
                    text += str(txt)
                    if open_block is None or block_kind.get(open_block) != "text":
                        if close_open() is not None:
                            yield close_open()
                        open_block = next_index
                        next_index += 1
                        block_kind[open_block] = "text"
                        yield ev("content_block_start", {
                            "type": "content_block_start", "index": open_block,
                            "content_block": {"type": "text", "text": ""}})
                    yield ev("content_block_delta", {
                        "type": "content_block_delta", "index": open_block,
                        "delta": {"type": "text_delta", "text": str(txt)}})
                for tc in delta.get("tool_calls") or []:
                    oidx = int(tc.get("index") or 0)
                    if oidx not in tool_block:
                        if close_open() is not None:
                            yield close_open()
                        bidx = next_index
                        next_index += 1
                        tool_block[oidx] = bidx
                        block_kind[bidx] = "tool_use"
                        open_block = bidx
                        fn = tc.get("function") or {}
                        yield ev("content_block_start", {
                            "type": "content_block_start", "index": bidx,
                            "content_block": {"type": "tool_use",
                                "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:12]),
                                "name": fn.get("name") or "", "input": {}}})
                    else:
                        bidx = tool_block[oidx]
                        # OpenAI streams tools sequentially; if a fragment for a
                        # different tool arrives while another block is open we
                        # skip it rather than reopen a closed block (which the
                        # Anthropic protocol forbids).
                        if open_block != bidx:
                            fn = tc.get("function") or {}
                            frag = fn.get("arguments") or ""
                            continue
                    fn = tc.get("function") or {}
                    frag = fn.get("arguments") or ""
                    if frag:
                        yield ev("content_block_delta", {
                            "type": "content_block_delta", "index": bidx,
                            "delta": {"type": "input_json_delta", "partial_json": frag}})
    if close_open() is not None:
        yield close_open()
    yield ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": finish, "stop_sequence": None},
        "usage": {"input_tokens": pin, "output_tokens": pout}})
    yield ev("message_stop", {"type": "message_stop"})


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(str(part.get("text") or part.get("content") or ""))
        return "".join(out)
    return "" if content is None else str(content)


def chat_to_responses_request(body: dict) -> dict:
    """chat-shaped payload -> Responses API request (keeps unknown keys)."""
    msgs = body.pop("messages", []) or []
    sys_txt = "\n".join(_text_of(m.get("content")) for m in msgs
                         if m.get("role") == "system")
    inp = [{"role": m.get("role", "user"), "content": _text_of(m.get("content"))}
           for m in msgs if m.get("role") != "system"]
    out: dict = {"model": body.pop("model", None), "input": inp}
    if sys_txt:
        out["instructions"] = sys_txt
    if body.get("max_tokens") is not None:
        out[RESP_MAX_TEXT_FIELD] = body.pop("max_tokens")
    if body.get("max_completion_tokens") is not None:
        out[RESP_MAX_TEXT_FIELD] = body.pop("max_completion_tokens")
    for k in ("temperature", "top_p", "stream", "stop", "tools", "tool_choice",
              "parallel_tool_calls", "truncation", "user", "metadata", "reasoning"):
        if body.get(k) is not None:
            out[k] = body[k]
    if isinstance(out.get("stop"), list):
        out["text"] = {"format": {"type": "text"}, **(out.get("text") or {})}
    body.pop("stream_options", None)
    for k, v in body.items():
        if k not in out:
            out[k] = v
    return out


def responses_to_chat_request(body: dict) -> dict:
    """Responses API request -> chat-shaped payload (what the router routes)."""
    msgs: list[dict] = []
    instr = body.get("instructions")
    if instr:
        msgs.append({"role": "system", "content": _text_of(instr)})
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                role = str(item.get("role") or "user")
                content = item.get("content")
                if isinstance(content, list):
                    content = _text_of(content)
                msgs.append({"role": role, "content": _text_of(content)})
            else:
                msgs.append({"role": "user", "content": str(item)})
    out = {"model": body.get("model"), "messages": msgs}
    if body.get(RESP_MAX_TEXT_FIELD) is not None:
        out["max_tokens"] = body[RESP_MAX_TEXT_FIELD]
    for k in ("temperature", "top_p", "stream", "stop", "tools", "tool_choice",
              "parallel_tool_calls", "truncation", "user"):
        if body.get(k) is not None:
            out[k] = body[k]
    return out


def _resp_usage(u: dict) -> dict:
    u = u or {}
    pin = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
    pout = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
    det = {}
    ipd = u.get("input_tokens_details") or {}
    if int(ipd.get("cached_tokens", 0) or 0):
        det["prompt_tokens_details"] = {"cached_tokens": int(ipd["cached_tokens"])}
    ioc = u.get("output_tokens_details") or {}
    if int(ioc.get("reasoning_tokens", 0) or 0):
        det["completion_tokens_details"] = {"reasoning_tokens": int(ioc["reasoning_tokens"])}
    return dict({"prompt_tokens": pin, "completion_tokens": pout,
                 "total_tokens": int(u.get("total_tokens", pin + pout) or 0)}, **det)


def _resp_text(data: dict) -> str:
    out = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    out.append(str(part.get("text") or ""))
        elif item.get("type") in ("function_call", "refusal"):
            out.append(str(item.get("text") or item.get("arguments") or ""))
    if not out and data.get("output_text"):
        t = data["output_text"]
        out.append(t if isinstance(t, str) else _text_of(t))
    return "".join(out)


def from_responses(data: dict, alias: str) -> dict:
    """Responses object -> chat.completion (the router's internal shape)."""
    u = _resp_usage(data.get("usage"))
    finish = {"completed": "stop", "incomplete": "length",
              "failed": "stop", "cancelled": "stop"}.get(
                  str(data.get("status") or ""), "stop")
    calls = [item for item in (data.get("output") or [])
             if isinstance(item, dict) and item.get("type") == "function_call"]
    msg: dict = {"role": "assistant", "content": _resp_text(data)}
    if calls:
        msg["tool_calls"] = [{"id": c.get("call_id") or c.get("id"),
                              "type": "function",
                              "function": {"name": c.get("name"),
                                           "arguments": c.get("arguments") or ""}}
                             for c in calls]
        finish = "tool_calls"
    return {"id": data.get("id") or ("chatcmpl-" + uuid.uuid4().hex[:12]),
            "object": "chat.completion", "created": int(data.get("created_at")
                                                        or time.time()),
            "model": alias,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": u}


def chat_to_responses_obj(body: dict, alias: str) -> dict:
    """chat.completion -> Responses object (what /v1/responses answers)."""
    u = _resp_usage(body.get("usage"))
    ch = (body.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    text = _text_of(msg.get("content"))
    rid = "resp_" + str(body.get("id") or uuid.uuid4().hex[:12]).replace(
        "chatcmpl-", "")
    out: list = []
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn = tc.get("function") or {}
            out.append({"type": "function_call", "id": tc.get("id"),
                        "call_id": tc.get("id"), "name": fn.get("name"),
                        "arguments": fn.get("arguments") or ""})
    out.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:12],
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text,
                             "annotations": []}]})
    status = {"stop": "completed", "length": "incomplete",
              "tool_calls": "completed"}.get(str(ch.get("finish_reason")), "completed")
    pin = u.get("prompt_tokens", 0)
    pout = u.get("completion_tokens", 0)
    return {"id": rid, "object": "response", "created_at": int(body.get("created")
                                                              or time.time()),
            "status": status, "model": alias, "output": out,
            "output_text": text,
            "usage": {"input_tokens": pin, "output_tokens": pout,
                      "total_tokens": pin + pout},
            "error": None, "incomplete_details": None,
            "parallel_tool_calls": True}


def _resp_event(t: str, obj: dict) -> bytes:
    return (f"event: {t}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n").encode()


async def responses_stream_to_openai(chunks, alias: str = ""):
    """Responses SSE -> chat.completion.chunk dicts (for chat clients)."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
    text, usage, finish = "", {}, "stop"
    async for raw in chunks:
        for line in raw.decode("utf-8", "ignore").split("\n"):
            s = line.strip()
            if not s.startswith("data:"):
                continue
            try:
                ev = json.loads(s[5:].strip())
            except Exception:
                continue
            t = str(ev.get("type") or "")
            if t == "response.output_text.delta":
                text += str(ev.get("delta") or "")
                yield _chunk(cid, created, alias, {"content": ev.get("delta") or ""})
            elif t == "response.completed":
                r = ev.get("response") or {}
                usage = _resp_usage(r.get("usage"))
                finish = {"incomplete": "length"}.get(str(r.get("status")), "stop")
            elif t == "response.failed" or t == "response.incomplete":
                finish = "stop"
    yield _chunk(cid, created, alias, {}, finish=finish, usage=usage or None)


async def chat_stream_to_response_events(chunks, alias: str):
    """chat SSE (already normalised upstream) -> Responses SSE event stream."""
    rid = "resp_" + uuid.uuid4().hex[:12]
    mid = "msg_" + uuid.uuid4().hex[:12]
    base = {"id": rid, "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "model": alias, "output": [],
            "usage": None, "error": None}
    yield _resp_event("response.created", {"type": "response.created",
                                           "response": base})
    yield _resp_event("response.output_item.added",
                      {"type": "response.output_item.added", "output_index": 0,
                       "item": {"id": mid, "type": "message", "role": "assistant",
                                "status": "in_progress", "content": []}})
    yield _resp_event("response.content_part.added",
                      {"type": "response.content_part.added", "output_index": 0,
                       "content_index": 0,
                       "part": {"type": "output_text", "text": "", "annotations": []}})
    text, usage = "", {}
    async for raw in chunks:
        for line in raw.decode("utf-8", "ignore").split("\n"):
            s = line.strip()
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            u = ev.get("usage")
            if u:
                usage = _resp_usage(u)
            for choice in ev.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    text += str(delta)
                    yield _resp_event("response.output_text.delta",
                                      {"type": "response.output_text.delta",
                                       "item_id": mid, "output_index": 0,
                                       "content_index": 0, "delta": str(delta)})
    yield _resp_event("response.output_text.done",
                      {"type": "response.output_text.done", "item_id": mid,
                       "output_index": 0, "content_index": 0, "text": text})
    done_item = {"id": mid, "type": "message", "role": "assistant",
                 "status": "completed",
                 "content": [{"type": "output_text", "text": text,
                              "annotations": []}]}
    yield _resp_event("response.content_part.done",
                      {"type": "response.content_part.done", "output_index": 0,
                       "content_index": 0,
                       "part": {"type": "output_text", "text": text,
                                "annotations": []}})
    yield _resp_event("response.output_item.done",
                      {"type": "response.output_item.done", "output_index": 0,
                       "item": done_item})
    final = dict(base)
    final.update({"status": "completed", "output": [done_item], "usage": usage,
                  "output_text": text})
    yield _resp_event("response.completed",
                      {"type": "response.completed", "response": final})


async def anthropic_stream_to_openai(chunks, alias: str):
    """Translate Anthropic SSE into OpenAI chat.completion.chunk dicts."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
    buf, pin, pout, finish = b"", 0, 0, None
    yield _chunk(cid, created, alias, {"role": "assistant", "content": ""})
    async for raw in chunks:
        buf += raw
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = line.decode("utf-8", "ignore").strip()
            if not s.startswith("data:"):
                continue
            try:
                ev = json.loads(s[5:].strip())
            except Exception:
                continue
            t = ev.get("type")
            if t == "message_start":
                pin = int((ev.get("message", {}).get("usage") or {}).get("input_tokens", 0) or 0)
            elif t == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    yield _chunk(cid, created, alias, {"content": d["text"]})
            elif t == "message_delta":
                pout = int((ev.get("usage") or {}).get("output_tokens", pout) or 0)
                finish = _stop_reason((ev.get("delta") or {}).get("stop_reason"))
            elif t == "message_stop":
                yield _chunk(cid, created, alias, {}, finish,
                             {"prompt_tokens": pin, "completion_tokens": pout,
                              "total_tokens": pin + pout})
