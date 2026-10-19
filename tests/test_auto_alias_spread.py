"""`auto`/`smsocket` must spread across providers, not pin to the first one.

The original resolver walked `Settings.model_index()` (insertion order =
config.yaml provider order) and returned the first alias with a provider, so
`auto` always resolved to the first alias of the first provider in the file -
agnes in every config anyone happened to write. These tests pin the new
behaviour: priority-aware, availability-aware, strategy-driven rotation.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from SMSocket.config import ProviderSpec, Settings
from SMSocket.gateway import create_app
from SMSocket.providers import Pool

H = {"Authorization": "Bearer sk-test"}


def _slots(provs):
    return Settings(listen="127.0.0.1:8000", master_keys=["sk-test"], cooldown=30, db_path=":memory:", pricing={},
                    providers=provs)


def _prov(name, prio, alias):
    return ProviderSpec(name=name, base_url="http://mock/v1", keys=["k-%s" % name],
                        models={alias: "%s-up" % alias}, priority=prio)


def test_auto_alias_uses_priority_not_file_order():
    """Two providers, second in file order but higher priority: `auto` goes to
    the higher-priority one, not the first one typed."""
    s = _slots([_prov("first", prio=1, alias="a1"),
                _prov("second", prio=9, alias="a2")])
    pool = Pool(s.providers)
    assert pool.auto_alias("priority") == "a2"


def test_auto_alias_round_robin_rotates_across_providers():
    """round_robin walks the ranked providers so consecutive calls land on
    different ones; nothing pins to a single provider."""
    s = _slots([_prov("p0", 1, "m0"), _prov("p1", 2, "m1"), _prov("p2", 3, "m2")])
    pool = Pool(s.providers)
    seen = [pool.auto_alias("round_robin") for _ in range(6)]
    # rotation visits every provider, never stuck on one, and keeps moving.
    assert set(seen) == {"m0", "m1", "m2"}, seen
    assert all(seen[i] != seen[(i + 1) % len(seen)] for i in range(len(seen))), seen


def test_auto_alias_skips_cooled_provider():
    """A provider whose only key is cooled for its alias is skipped while
    another provider still has a live slot."""
    s = _slots([_prov("hi", 9, "hi-m"), _prov("lo", 1, "lo-m")])
    pool = Pool(s.providers)
    hi = next(x for x in pool.slots if x.provider.name == "hi")
    hi.note_fail(60, "hi-m")           # cool the high-priority alias
    assert pool.auto_alias("priority") == "lo-m"


def test_auto_alias_falls_back_when_everything_is_cooled():
    """When the whole pool is cooled, a best-ranked alias is still returned so
    the router can keep polling instead of erroring out."""
    s = _slots([_prov("hi", 9, "hi-m"), _prov("lo", 1, "lo-m")])
    pool = Pool(s.providers)
    for x in pool.slots:
        x.note_fail(60, x.provider.aliases()[0])
    assert pool.auto_alias("priority") in {"hi-m", "lo-m"}


def test_auto_alias_none_when_no_keys():
    """A pool with enabled providers but no keys resolves to nothing."""
    s = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                 db_path=":memory:", pricing={},
                 providers=[ProviderSpec(name="x", base_url="http://mock/v1",
                                         keys=[], models={"m": "m"})])
    assert Pool(s.providers).auto_alias("priority") is None


# ---- end-to-end: the gateway must not pin every request to one provider -----


def _chat_json(model, content="ok"):
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _handler():
    def h(request: httpx.Request) -> httpx.Response:
        b = __import__("json").loads(request.content)
        return httpx.Response(200, json=_chat_json(b.get("model"), "ok"))
    return h


def test_chat_auto_does_not_pin_to_first_provider(tmp_path, monkeypatch):
    s = Settings(listen="127.0.0.1:8000", master_keys=["sk-test"],
                 strategy="round_robin", cooldown=30,
                 db_path=str(tmp_path / "u.sqlite3"), pricing={},
                 providers=[_prov("p0", 1, "m0"), _prov("p1", 2, "m1"),
                            _prov("p2", 3, "m2")])
    transport = httpx.MockTransport(_handler())
    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    seen = []
    with TestClient(create_app(s)) as c:
        for _ in range(6):
            r = c.post("/v1/chat/completions", headers=H,
                       json={"model": "auto", "messages": [
                           {"role": "user", "content": "hi"}]})
            assert r.status_code == 200, r.text
            seen.append(r.json()["model"])
    # round_robin rotated across all three providers, not stuck on the first.
    assert set(seen) == {"m0", "m1", "m2"}, seen
    assert seen[0] != seen[1] or seen[1] != seen[2]   # actually rotating
