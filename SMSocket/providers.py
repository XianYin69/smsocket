"""Upstream key pool: rotation, health tracking, cooldown, rpm limiting."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ProviderSpec

log = logging.getLogger("smssocket")

# 429 error messages that carry an RPM figure ("20 requests/minute",
# "rate limit of 20/min exceeded", "rpm limit: 20", "每分钟限制 100 次", ...).
_RPM_MSG = re.compile(
    r"""(?ix)
    (?:^|[^0-9])([1-9][0-9]{0,5})\s*(?:(?:-|_)?rpm|requests?[/\s_-]*per\s*minute|
    req(?:uests?)(?:[/\s_-]+)?(?:per|/)??\s*minute|/min|per\s*min|每分钟|一分钟)
    """)
# Chinese phrasing puts the figure *after* the cap word: 每分钟限制 100 次 /
# 一分钟限制 20 次 / RPM限制 100.
_RPM_CN = re.compile(r"(?ix)(?:每分钟|一分钟|每\s*分钟|RPM\s*限制|限制)\s*([1-9][0-9]{0,5})\s*次?")
# Chinese "每 N 分钟可请求 N 次 / 每 N 分钟 1 次": figure sits *between* 每 and 分钟.
# This is agnes free-tier's 429 phrasing ("每 1 分钟可请求 1 次") - a real RPM
# window the stack scheduler must park on, NOT a hard quota cap.
_RPM_CN_WINDOW = re.compile(r"(?ix)每\s*([1-9][0-9]{0,5})\s*分钟")
_RPM_EXACT = re.compile(r"(?ix)\brpm\b\D{0,8}([1-9][0-9]{0,5})")
# 硬「额度/配额耗尽」类 429：只有这种（不含任何每分钟滑动窗口数字）才不学 RPM、
# 不入栈等待而快速失败。agno 的「今日文本额度已用完，当前处于限速模式：每 1 分钟
# 可请求 1 次」是**滑动窗口 RPM 限流**（RPM=1），不是硬配额——必须学 RPM 让调用者
# 入栈等到窗口放行（这是 smsocket stack.park_on=[rpm] 的设计本意，切勿误判成
# quota-exhausted 而丢掉入栈等待）。
_HARD_QUOTA_RE = re.compile(r"(?ix)(monthly|daily|quota|exhaust|月度|配额已用尽|额度已用尽[。．]?)(?!.*(?:每分钟|一分钟|rpm|/min))")


def _is_quota_exhausted(detail: str) -> bool:
    """True when a 429 is a *hard* daily/monthly quota cap (no per-minute window), not a
    transient sliding-window RPM limit.

    A hard cap should NOT trigger key cooldown and the caller should fail fast
    instead of parking. A sliding-window limit (e.g. "每 1 分钟可请求 1 次") is
    NOT quota-exhausted: it carries a real per-minute rate that must be learned
    so the stack scheduler can park callers until the window frees up.
    """
    if not detail:
        return False
    # A sliding-window 429 naming a per-minute wait window is RPM-limiting, never a hard cap.
    if _RPM_MSG.search(detail) or _RPM_CN.search(detail) or _RPM_EXACT.search(detail):
        return False
    return bool(_HARD_QUOTA_RE.search(detail))


def _rpm_from_message(text: str) -> int | None:
    """First plausible requests-per-minute figure in an upstream error string."""
    if not text:
        return None
    m = _RPM_EXACT.search(text)
    if m:
        return int(m.group(1))
    m = _RPM_CN.search(text)
    if m:
        # Chinese cap phrasing: the number is the requests-per-minute limit
        # itself (每分钟限制 N 次), never a wait window - no 60-second guard.
        return int(m.group(1))
    m = _RPM_CN_WINDOW.search(text)
    if m:
        # "每 N 分钟可请求 X 次" (agnes free tier): the per-minute window IS the
        # rate.  N minutes-per-allowance means 60/N rpm; use the allowance
        # figure when present, else fall back to the window width itself.
        return int(m.group(1))
    m = _RPM_MSG.search(text)
    if m:
        n = int(m.group(1))
        # 20 "minutes" is a retry window, not a rate; a minute-scale number
        # immediately before "per minute" phrasing is almost always the wait.
        if n >= 60:
            return None
        return n
    return None


AUTO_ALIASES = ("smsocket", "auto", "smsocket/auto")


def is_auto_alias(alias: str) -> bool:
    """True for the pseudo-aliases meaning "the gateway picks the model".

    SMS-core ships with `llm_gateway.model = auto`, so every SMS turn arrives as
    `model: auto` — these names must resolve to a real alias before routing.
    """
    return str(alias or "") in AUTO_ALIASES


def resolve_auto_alias(pool: "Pool", strategy: str, alias: str) -> str:
    """The concrete alias one `auto` request goes to, else `alias` unchanged.

    An unresolvable alias comes back as "" — the caller turns that into a 503.
    """
    if not is_auto_alias(alias):
        return alias
    return pool.auto_alias(strategy) or ""


def _rpm_from_headers(headers: Any) -> int | None:
    """OpenAI-style rate-limit headers: x-ratelimit-limit-requests (or -total)."""
    if not headers:
        return None
    try:
        items = headers.items() if hasattr(headers, "items") else list(headers)
    except Exception:
        return None
    for k, v in items:
        key = str(k).lower()
        if key in ("x-ratelimit-limit-requests", "x-ratelimit-limit-total",
                   "x-ratelimit-limit"):
            try:
                return int(float(str(v)))
            except ValueError:
                continue
    return None


def learn_rate_limit(provider: ProviderSpec, headers: Any = None,
                     message: str = "") -> int | None:
    """Learn a provider's real RPM from one 429 answer; returns the new cap.

    Source order: standard `x-ratelimit-limit*` header, then a number the
    provider itself named in the error body ("free accounts: 20 rpm").  The
    value is written onto `provider.max_rpm` so the sliding-window limiter
    (`KeySlot.available` / `rpm_headroom`) starts enforcing it at runtime -
    no config edit, no restart.  A provider that already carries a cap in its
    config file is never auto-loosened: a learned value only ever *narrows*
    the window (`min(old, learned)`), because "I got a 429 at N" proves N is
    too high, never that the cap is infinite.  Returns the cap in force, or
    None when nothing could be learned.
    """
    cap = _rpm_from_headers(headers)
    if cap is None:
        cap = _rpm_from_message(message)
    if cap is None or cap < 1:
        return None
    current = int(getattr(provider, "max_rpm", 0) or 0)
    learned = current if current and current < cap else cap
    if current and current == learned:
        return learned
    provider.max_rpm = learned
    log.info("learned rpm cap for provider '%s': %d (now enforcing max_rpm=%d)",
             provider.name, cap, learned)
    return learned


def mask(key: str) -> str:
    if len(key) <= 10:
        return (key[:2] + "***") if key else "(empty)"
    return f"{key[:5]}…{key[-4:]}"


@dataclass
class KeySlot:
    provider: ProviderSpec
    key: str
    fails: int = 0
    dead_until: dict[str, float] = field(default_factory=dict)  # alias -> cooldown expiry; "*" cools every alias
    ok: int = 0
    _window: list[float] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.provider.name}/{mask(self.key)}"

    def cooled(self, now: float, alias: str | None = None) -> bool:
        """In cooldown for ``alias`` (or for *any* alias when alias is None).

        Cooldown is tracked per model alias: a 429 on model A only cools A, so a
        sibling model B on the same key keeps being served. A hard key/provider
        failure records the catch-all alias ``"*"`` which cools every model on
        that key, preserving the old "whole key is dead" behaviour.
        """
        if alias is None:
            return any(now < v for v in self.dead_until.values())
        return (now < self.dead_until.get("*", 0.0)
                or now < self.dead_until.get(alias, 0.0))

    def available(self, now: float, alias: str | None = None) -> bool:
        # A keyless provider (OpenCode free models) has no credential at all -
        # its single slot carries an empty key and is always available, unlike a
        # keyed provider whose empty key would mean "no key configured".
        if not self.key and not getattr(self.provider, "keyless", False):
            return False
        if self.key and self.cooled(now, alias):
            return False
        if self.provider.max_rpm:
            self._window = [t for t in self._window if now - t < 60]
            if len(self._window) >= self.provider.max_rpm:
                return False
        return True

    def rpm_headroom(self, now: float | None = None) -> int | None:
        """Requests still allowed in this 60s window (None = no rpm cap).

        Prunes the window like `available()` does, so the stack scheduler can
        ask "is this provider rate-limited right now?" without consuming a key.
        """
        if not self.provider.max_rpm:
            return None
        now = time.time() if now is None else now
        self._window = [t for t in self._window if now - t < 60]
        return max(0, self.provider.max_rpm - len(self._window))

    def touch(self) -> None:
        self._window.append(time.time())

    def note_ok(self, alias: str | None = None) -> None:
        self.ok += 1
        self.fails = 0
        # A success means the key answered: drop the catch-all "*" (the key is
        # alive again) and the specific alias that just succeeded, but leave any
        # unrelated alias cooldowns in place.
        if alias is None:
            self.dead_until.clear()
        else:
            self.dead_until.pop("*", None)
            self.dead_until.pop(alias, None)

    def note_fail(self, cooldown: float, alias: str | None = None) -> None:
        self.fails += 1
        if alias is None or alias == "*":
            self.dead_until.clear()
            self.dead_until["*"] = time.time() + cooldown
        else:
            self.dead_until[alias] = time.time() + cooldown


class Pool:
    """All key slots across providers, with selection strategies."""

    def __init__(self, providers: list[ProviderSpec]) -> None:
        self.providers = providers
        self.slots: list[KeySlot] = []
        for p in providers:
            keys = [k for k in p.keys if k]
            # A keyless provider (OpenCode serves its free models and its public
            # /v1/models without any credential) still needs one slot so the
            # router can route to it - the slot just carries an empty key and
            # `headers()` then omits the Authorization header entirely.
            if not keys and getattr(p, "keyless", False):
                keys = [""]
            self.slots.extend(KeySlot(p, k) for k in keys)
        self._cursor = 0
        self._alias_cursor = 0      # round_robin rotation across providers for `auto`
        # autotune overrides: provider name -> {"priority":int,"weight":int}
        # empty = trust the numbers in the config file
        self.derived: dict[str, dict] = {}

    def set_derived(self, mapping: dict) -> None:
        """Replace the whole derived table (autotune writes, nothing else does)."""
        self.derived = {str(k): dict(v) for k, v in (mapping or {}).items()}

    def eff(self, p) -> tuple:
        """(priority, weight) actually in force: measured value, else typed value."""
        d = self.derived.get(getattr(p, "name", "")) or {}
        return (int(d.get("priority", p.priority)), int(d.get("weight", p.weight)))

    def upstream_model(self, slot: KeySlot, alias: str, embed: bool = False) -> str:
        table = slot.provider.embeddings if embed else slot.provider.models
        return table.get(alias, alias)

    def serving(self, alias: str, embed: bool = False) -> list[KeySlot]:
        out = []
        for s in self.slots:
            if not s.provider.enabled:
                continue
            names = s.provider.embeddings if embed else s.provider.aliases()
            if alias in names:
                out.append(s)
        return out

    def candidates(self, alias: str, strategy: str = "priority",
                   embed: bool = False, net=None) -> list[KeySlot]:
        """Ranked key slots for one alias.

        `net` (a clash.NetPlane) is optional: when the net plane scores in smart
        mode it re-ranks the result by measured egress quality, so a provider
        only reachable through a proxy outranks a dead direct route. Strategy
        order stays the tie-break, so nothing moves unless the network speaks.
        """
        now = time.time()
        live = [s for s in self.serving(alias, embed) if s.available(now, alias)]
        if not live:
            return []
        if strategy == "round_robin":
            n = len(live)
            self._cursor = (self._cursor + 1) % max(n, 1)
            return self._rerank(live[self._cursor:] + live[:self._cursor], net)
        if strategy == "weighted":
            weights = [max(self.eff(s.provider)[1], 1) for s in live]
            out, bag = [], list(live)
            while bag:
                pick = random.choices(bag, weights=[max(self.eff(s.provider)[1], 1)
                                                    for s in bag], k=1)[0]
                out.append(pick)
                bag.remove(pick)
            return self._rerank(out, net)
        # priority: high priority first, then low failure count, then weight
        # (measured values from autotune win over the numbers in the file)
        return self._rerank(sorted(live, key=lambda s: (-self.eff(s.provider)[0],
                                                 s.fails, -self.eff(s.provider)[1])), net)

    @staticmethod
    def _rerank(slots: list[KeySlot], net) -> list[KeySlot]:
        """Stable sort by egress quality: fastest first, then least broken."""
        if net is None or not getattr(net, "smart", False):
            return slots
        return sorted(slots, key=lambda s: net.sort_key(s.provider.name))

    def auto_alias(self, strategy: str = "priority") -> str | None:
        """Resolve `auto` to one concrete alias, by provider — never by file order.

        Picking "the first alias of the first provider in config.yaml" pins every
        `auto` request onto whichever provider someone typed first, ignoring its
        priority, weight, measured quality and current cooldown state.  Here the
        provider is chosen with the same ranking the pool already uses for key
        slots inside one alias (effective priority, so autotune numbers beat the
        typed ones, then weight, then name) and the alias inside it is the one
        that still has the most live key slots.  Providers with no live slot are
        skipped while others are available, so a 429-cooled provider stops
        winning; when the whole pool is cooled the best-typed provider is still
        returned so the router can keep polling instead of failing the request.
        """
        now = time.time()
        rows: list[tuple] = []
        for p in self.providers:
            if not p.enabled:
                continue
            slots = [s for s in self.slots if s.provider is p]
            # A keyless provider (OpenCode free models) has no credential: its
            # single slot carries an empty key and still counts as usable.
            keyed = [s for s in slots if s.key or getattr(p, "keyless", False)]
            if not keyed:
                continue
            floored = min([s.fails for s in keyed] or [0])
            live, best = [], None
            for a in p.aliases():
                n = sum(1 for s in keyed if s.available(now, a))
                if n:
                    live.append(n)
                key = (n, -floored, a)
                if best is None or key > best[0]:
                    best = (key, a)
            if best is None:
                continue
            pr, wt = self.eff(p)
            rows.append((pr, wt, p, best[1], sum(live)))
        if not rows:
            return None
        live_rows = [r for r in rows if r[4] > 0]
        pick_from = live_rows or rows
        pick_from.sort(key=lambda r: (-r[0], -r[1], r[2].name, -r[4]))
        if strategy == "weighted":
            weights = [max(r[1], 1) for r in pick_from]
            i = random.choices(range(len(pick_from)), weights=weights, k=1)[0]
        elif strategy == "round_robin":
            n = len(pick_from)
            self._alias_cursor = (self._alias_cursor + 1) % n
            i = self._alias_cursor
        else:
            i = 0
        return pick_from[i][3]

    def rebase(self, providers) -> "Pool":
        # Rebuild slots for a new provider set but carry over observed health,
        # so /admin/reload cannot instantly un-cool a key that is failing.
        keep = {(x.provider.name, x.key): x for x in self.slots}
        fresh = Pool(providers)
        for x in fresh.slots:
            old = keep.get((x.provider.name, x.key))
            if old:
                x.fails, x.ok = old.fails, old.ok
                x.dead_until = dict(old.dead_until)
        fresh.derived = dict(self.derived)   # measured ranking survives a reload
        return fresh
    def stats(self) -> list[dict]:
        out = []
        for s in self.slots:
            pr, wt = self.eff(s.provider)
            out.append({"provider": s.provider.name, "key": mask(s.key), "ok": s.ok,
                        "fails": s.fails,
                         "cooldown_left": round(max([0.0]
                                                    + [v - time.time()
                                                       for v in s.dead_until.values()]), 1),
                        "priority": pr, "weight": wt,
                        "auto": bool(self.derived.get(s.provider.name))})
        return out
