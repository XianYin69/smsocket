"""Unit tests for the router's poll-until-success behaviour.

The router never converges to an error any more: every request polls the
candidate pool round after round (requested alias first, then cross-model
fallbacks) until an upstream answers. Cooldowns are respected between rounds.
"""
import asyncio

import httpx
import pytest

from SMSocket.config import ProviderSpec, Settings
from SMSocket.router import Router
from SMSocket.usage import Usage
from SMSocket.concurrency import Gate

H = {"Authorization": "Bearer sk-test"}


def _settings(tmp_path, cooldown=0.5):
    return Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                    strategy="priority", cooldown=cooldown,
                    db_path=str(tmp_path / "usage.sqlite3"), pricing={},
                    providers=[
                        ProviderSpec(name="p0", base_url="http://mock/v1",
                                     keys=["k0"], models={"demo": "d0"}, priority=10),
                        ProviderSpec(name="p1", base_url="http://mock/v1",
                                     keys=["k1"], models={"demo": "d1"}, priority=9),
                        ProviderSpec(name="p2", base_url="http://mock/v1",
                                     keys=["k2"], models={"demo": "d2"}, priority=8),
                    ])


def _chat_json(model, content):
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}


# ---------------------------------------------------------------------------
# 1. random start of each round (unchanged)
# ---------------------------------------------------------------------------
def test_round_candidates_randomises_start(tmp_path, monkeypatch):
    """`random.randrange` controls the rotation; pinning it proves the start
    moves.  With three candidates and randrange pinned to 2, the first slot of
    the returned list must be the third candidate (index 2)."""
    settings = _settings(tmp_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_chat_json("d0", "ok"))))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    calls = []
    seen = []

    def fake_randrange(n):
        seen.append(n)
        return 2

    monkeypatch.setattr("SMSocket.router.random.randrange", fake_randrange)
    cands = router._round_candidates("demo")
    assert seen == [3]                      # one call, three candidates
    assert cands[0][0].key == "k2"             # the third candidate moved to front
    assert {c[0].key for c in cands} == {"k0", "k1", "k2"}
    assert len(cands) == 3                  # still visits every candidate


def test_round_candidates_no_randomisation_for_single_candidate(tmp_path, monkeypatch):
    """A single candidate must not call randrange (the >1 guard)."""
    settings = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                        strategy="priority", cooldown=30,
                        db_path=str(tmp_path / "u.sqlite3"), pricing={},
                        providers=[ProviderSpec(name="only", base_url="http://mock/v1",
                                                keys=["k"], models={"demo": "d"})])
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_chat_json("d", "ok"))))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    called = []
    monkeypatch.setattr("SMSocket.router.random.randrange",
                        lambda n: called.append(n) or 0)
    cands = router._round_candidates("demo")
    assert called == []                     # guard: no randomisation
    assert len(cands) == 1


# ---------------------------------------------------------------------------
# 2. poll until some upstream succeeds
# ---------------------------------------------------------------------------
class _CountingTransport(httpx.AsyncBaseTransport):
    """Fails the first ``failures`` requests with ConnectError, then answers 200."""

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("nope")
        return httpx.Response(200, json=_chat_json("d0", "ok"))


def test_complete_polls_until_success(tmp_path):
    """Three candidates fail round one; the first candidate of round two
    answers. complete must return the body instead of raising."""
    settings = _settings(tmp_path)
    transport = _CountingTransport(failures=3)
    http = httpx.AsyncClient(transport=transport)
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        return await router.complete({"model": "demo",
                                      "messages": [{"role": "user", "content": "x"}]})

    body = asyncio.run(go())
    assert body["model"] == "demo"
    assert transport.calls == 4             # 3 failed + 1 successful


def test_open_stream_polls_until_success(tmp_path):
    """Streaming open must equally poll round after round until one opens."""
    settings = _settings(tmp_path)
    transport = _CountingTransport(failures=3)
    http = httpx.AsyncClient(transport=transport)
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        return await router.open_stream({"model": "demo", "stream": True,
                                         "messages": [{"role": "user", "content": "x"}]})

    slot, alias, t0, r = asyncio.run(go())
    assert r.status_code == 200
    assert transport.calls == 4


def test_complete_cross_model_failover(tmp_path):
    """Requested alias exhausted, another alias on a different provider is
    healthy: the router must jump to the other model and serve it."""
    settings = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                        strategy="priority", cooldown=0.5,
                        db_path=str(tmp_path / "u.sqlite3"), pricing={},
                        providers=[
                            ProviderSpec(name="p0", base_url="http://mock/v1",
                                         keys=["k0"], models={"demo": "d0"}, priority=10),
                            ProviderSpec(name="p1", base_url="http://mock/v1",
                                         keys=["k1"], models={"demo2": "d1"}, priority=9),
                        ])

    def handler(r: httpx.Request) -> httpx.Response:
        key = (r.headers.get("authorization", "").replace("Bearer ", "")
               or r.headers.get("x-api-key", ""))
        if key == "k0":
            raise httpx.ConnectError("nope")
        return httpx.Response(200, json=_chat_json("d1", "ok"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        return await router.complete({"model": "demo",
                                      "messages": [{"role": "user", "content": "x"}]})

    body = asyncio.run(go())
    assert body["model"] == "demo2"         # served by the failover model


def test_complete_mixed_429_then_success(tmp_path):
    """A 429 cools one alias; the next round on the same alias must still be
    retried once cool, and a hard error on other candidates must not block."""
    settings = _settings(tmp_path)
    state = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        key = (r.headers.get("authorization", "").replace("Bearer ", "")
               or r.headers.get("x-api-key", ""))
        state["n"] += 1
        if key == "k0" and state["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if key in ("k1", "k2"):
            raise httpx.ConnectError("nope")
        return httpx.Response(200, json=_chat_json("d0", "ok"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        return await router.complete({"model": "demo",
                                      "messages": [{"role": "user", "content": "x"}]})

    body = asyncio.run(go())
    assert body["model"] == "demo"


# ---------------------------------------------------------------------------
# 3. persistent failure: never converges, never raises
# ---------------------------------------------------------------------------
def test_persistent_failure_never_converges(tmp_path):
    """Every candidate 429s every round: the router must keep polling instead
    of converging to an UpstreamError."""
    settings = _settings(tmp_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": {"message": "rate limited"}})))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        await asyncio.wait_for(
            router.complete({"model": "demo",
                             "messages": [{"role": "user", "content": "x"}]}),
            timeout=1.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())


def test_open_stream_persistent_failure_never_converges(tmp_path):
    """open_stream must equally never converge on a fully rate-limited pool."""
    settings = _settings(tmp_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": {"message": "rate limited"}})))
    from SMSocket.providers import Pool
    gate = Gate(settings.max_concurrency, settings.queue_wait,
                settings.per_provider_concurrency)
    router = Router(settings, Pool(settings.providers), Usage(settings.db_path),
                    http, gate)

    async def go():
        await asyncio.wait_for(
            router.open_stream({"model": "demo", "stream": True,
                                "messages": [{"role": "user", "content": "x"}]}),
            timeout=1.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())
