"""Stack scheduler: park a request when a limit bites, pop it when one frees.

Two pieces, one module - the same split as concurrency.py (Meter + Gate):

  Stack           the parking lot. LIFO by default (`append` / `pop()`),
                  switchable to fifo / priority. Owns the counters that
                  GET /concurrency and GET /stack read.
  StackScheduler  the waker. `drain()` is a background task that pops the top
                  parked caller as soon as global concurrency, provider
                  concurrency and provider rpm all have headroom, and grabs
                  the admission slot for it (`take_slot`) so the woken caller
                  never waits on a semaphore twice.

Single-loop asyncio, no locks: entries are only touched at await points, so
the counters stay consistent (the Meter invariant from concurrency.py). With
`stack.enabled: false` nothing is attached to the gate and admission control
is exactly the pre-v0.4b behaviour - a saturated caller still gets its 429.

"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import StackConfig

log = logging.getLogger("smssocket")


@dataclass
class Parking:
    """One parked caller: what it wanted, why it waits, and its wake-up event."""

    ts: float
    alias: str
    provider: str
    prio: int
    event: asyncio.Event
    granted: bool = False
    reason: str = ""
    grabbed: bool = False          # a gate slot is already held for it
    dropped: bool = False          # removed by DELETE /stack/{i}
    parked_ms: float = 0.0
    clock: float = 0.0            # perf_counter at push time
    grab: bool = True             # wake with a gate slot (False = notify only)

    def view(self, i: int = -1) -> dict:
        row = {"alias": self.alias, "provider": self.provider or None,
               "prio": self.prio, "reason": self.reason,
               "waited_ms": round((time.time() - self.ts) * 1000, 1),
               "granted": self.granted}
        if i >= 0:
            row["i"] = i
        return row
class Stack:
    """The parked callers, in pop order, plus the counters ops read.

    `policy` picks the pop end: lifo = last in first out (default), fifo =
    arrival order, priority = highest `prio` first, ties broken by arrival.
    One list backs all three so `DELETE /stack/{i}` indexes the same view the
    snapshot shows; priority pops the best candidate instead of the last one.
    """

    def __init__(self, policy: str = "lifo", max_depth: int = 1000) -> None:
        self.policy = policy if policy in ("lifo", "fifo", "priority") else "lifo"
        self.max_depth = max(1, int(max_depth or 1))
        self.items: list[Parking] = []
        self.pushed = self.popped = self.expired = self.dropped = 0
        self.peak = 0
        self.parked_ms_sum = 0.0
        self.parked_ms_peak = 0.0
        self.by_provider: dict[str, dict] = {}
    # -- shape --------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.items)

    def push(self, entry: Parking) -> bool:
        """Park `entry`; False when the stack is full (caller keeps its 429)."""
        if len(self.items) >= self.max_depth:
            return False
        self.items.append(entry)
        self.pushed += 1
        self.peak = max(self.peak, len(self.items))
        self._row(entry.provider)["pushed"] += 1
        self._row(entry.provider)["depth"] = self._row(entry.provider)["depth"] + 1
        return True

    def _row(self, provider: str) -> dict:
        return self.by_provider.setdefault(
            provider or "-", {"pushed": 0, "popped": 0, "expired": 0, "depth": 0})

    def _key(self, e: Parking) -> tuple:
        if self.policy == "priority":
            return (-int(e.prio or 0), e.ts)
        return (e.ts,)

    def candidates(self) -> list[Parking]:
        """Entries in pop order - the top of the stack first."""
        if self.policy == "fifo":
            return list(self.items)
        if self.policy == "priority":
            return sorted(self.items, key=self._key)
        return list(reversed(self.items))          # lifo: newest first

    def pop(self) -> Parking | None:
        if not self.items:
            return None
        if self.policy == "fifo":
            return self.items.pop(0)
        if self.policy == "priority":
            best = min(range(len(self.items)), key=lambda i: self._key(self.items[i]))
            return self.items.pop(best)
        return self.items.pop()

    def take(self, entry: Parking) -> bool:
        """Remove a woken entry and book the time it spent parked."""
        return self._remove(entry, "popped")

    def expire(self, entry: Parking) -> bool:
        return self._remove(entry, "expired")

    def drop(self, entry: Parking) -> bool:
        """Remove an entry ops deleted (not counted as parked time)."""
        entry.dropped = True
        return self._remove(entry, "dropped", count_ms=False)

    def _remove(self, entry: Parking, what: str, count_ms: bool = True) -> bool:
        if entry not in self.items:
            return False
        self.items.remove(entry)
        row = self._row(entry.provider)
        row["depth"] = max(0, row["depth"] - 1)
        if what == "popped":
            self.popped += 1
            row["popped"] += 1
        elif what == "expired":
            self.expired += 1
            row["expired"] += 1
        else:
            self.dropped += 1
        if count_ms:
            self.note_parked(entry)
        entry.event.set()
        return True

    def note_parked(self, entry: Parking) -> None:
        ms = entry.parked_ms if entry.parked_ms else \
            (time.perf_counter() - entry.clock) * 1000
        entry.parked_ms = round(ms, 1)
        self.parked_ms_sum += ms
        self.parked_ms_peak = max(self.parked_ms_peak, ms)
    # -- reporting ----------------------------------------------------------
    def snapshot(self, entries: int = 20) -> dict:
        """Counters for /concurrency; `entries` parked rows for /stack."""
        done = self.popped + self.expired
        out = {"depth": len(self.items), "peak": self.peak, "policy": self.policy,
               "max_depth": self.max_depth, "pushed": self.pushed,
               "popped": self.popped, "expired": self.expired, "dropped": self.dropped,
               "avg_parked_ms": round(self.parked_ms_sum / done, 1) if done else 0.0,
               "parked_ms_peak": round(self.parked_ms_peak, 1),
               "by_provider": {k: dict(v) for k, v in sorted(self.by_provider.items())}}
        if entries:
            out["entries"] = [e.view(i) for i, e in enumerate(self.items[:entries])]
        return out

class StackScheduler:
    """Stack + the `drain()` task that empties it when limits free up.

    Attached as `State.stack` and `Gate.stack`. When `stack.enabled` is false
    the gate is left untouched (`Gate.stack = None`), so nothing about the old
    429 path changes - the object only reports counters.
    """

    def __init__(self, cfg: StackConfig, gate=None, pool=None) -> None:
        self.cfg = cfg
        self.gate = gate
        self.pool = pool
        self.stack = Stack(cfg.policy, cfg.max_depth)
        self.interval = max(0.02, float(getattr(cfg, "interval", 0.25) or 0.25))
        self._task: asyncio.Task | None = None
        self._nudge = None                      # created on the live loop
    # -- config surface -----------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def wait(self) -> float:
        return float(self.cfg.wait)

    def parks_on(self, reason: str) -> bool:
        return self.enabled and reason in self.cfg.park_on

    # -- headroom -----------------------------------------------------------
    def rpm_headroom(self, provider: str, alias: str | None = None) -> int | None:
        """Requests the provider may still send this minute (None = uncapped).

        `alias` scopes the cooldown check so a key cooling for one model is still
        counted as having rpm headroom for a sibling model under the same provider.
        """
        if self.pool is None or not provider:
            return None
        now = time.time()
        best = None
        for slot in getattr(self.pool, "slots", []):
            p = slot.provider
            if p.name != provider or not p.enabled:
                continue
            # A keyless provider (OpenCode free models) has no credential: its
            # single slot carries an empty key and still counts as usable.
            if not slot.key and not getattr(p, "keyless", False):
                continue
            if slot.cooled(now, alias):                # cooling down: no use waking
                continue
            room = slot.rpm_headroom(now)
            if room is None:
                return None
            best = room if best is None else max(best, room)
        return best

    def rpm_blocked(self, provider: str, alias: str | None = None) -> bool:
        room = self.rpm_headroom(provider, alias)
        return room is not None and room <= 0

    def provider_cooling(self, provider: str, alias: str | None = None) -> bool:
        """True when every live key slot for this provider is in cooldown.

        Scoped to `alias` (or every alias when None): a provider is "cold" for
        model A only if all its keys are cooling for A, so model B can still be
        served by a key that is merely cooling for A. This is the
        `NoUpstream` cause the operator actually hits: a free-tier upstream
        returns 429/503, smsocket cools the slot down, and the next request for
        that alias finds zero candidates.
        """
        if self.pool is None or not provider:
            return False
        now = time.time()
        slots = [s for s in getattr(self.pool, "slots", [])
                 if s.provider.name == provider and s.provider.enabled and s.key]
        if not slots:
            return False
        return all(s.cooled(now, alias) for s in slots)

    async def take_slot(self, provider: str = "") -> bool:
        """Grab the admission slot for a woken caller (no double counting)."""
        if self.gate is None:
            return True
        return await self.gate.try_grab(provider)

    def release_slot(self, provider: str = "") -> None:
        if self.gate is not None:
            self.gate.release_grab(provider)
    # -- parking ------------------------------------------------------------
    async def park(self, alias: str = "", provider: str = "", reason: str = "saturated",
                   timeout: float | None = None, prio: int = 0, grab: bool = True):
        """Wait on the stack. Returns the Parking entry (`granted` says whether
        it was woken; with `grab` a gate slot is already held for the caller)."""
        limit = self.wait if timeout is None else float(timeout)
        entry = Parking(ts=time.time(), clock=time.perf_counter(), alias=alias or "",
                        provider=provider or "", prio=int(prio or 0),
                        event=asyncio.Event(), reason=reason, grab=grab)
        if not self.stack.push(entry):
            entry.reason = "stack_full"
            return entry
        self._ping()
        try:
            await asyncio.wait_for(entry.event.wait(), max(0.001, limit))
        except asyncio.TimeoutError:
            if not entry.granted:
                self.stack.expire(entry)
        except asyncio.CancelledError:
            if entry.granted and entry.grabbed:
                self.release_slot(entry.provider)
            self.stack.expire(entry)
            raise
        if not entry.granted and not entry.dropped:
            self.stack.expire(entry)          # woken by a drop we did not grant
        return entry
    # -- waking -------------------------------------------------------------
    async def wake_once(self) -> int:
        """Pop every parked caller whose limits are free, top of the stack first.

        Head-of-line blocking is not honoured: an entry stuck on a full rpm
        window must not stop a neighbour from going, so we scan in pop order
        and take the first (and further) eligible ones.
        """
        woke = 0
        for entry in list(self.stack.candidates()):
            if self.rpm_blocked(entry.provider, entry.alias):
                continue
            if self.provider_cooling(entry.provider, entry.alias):
                continue
            if entry.grab and not await self.take_slot(entry.provider):
                continue
            if not self.stack.take(entry):
                continue
            entry.granted = True
            entry.grabbed = bool(entry.grab)
            woke += 1
            entry.event.set()
        return woke

    async def drain(self) -> None:
        """Background loop: wake on demand, otherwise poll the headroom."""
        while True:
            try:
                woke = await self.wake_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                 # never kill the loop
                log.warning("stack drain error: %s", exc)
                woke = 0
            if woke:
                continue                             # capacity may still be free
            if self._nudge is not None:
                try:
                    await asyncio.wait_for(self._nudge.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
                self._nudge.clear()
            else:
                await asyncio.sleep(self.interval)
    def _ping(self) -> None:
        if self._nudge is not None:
            self._nudge.set()

    def start(self) -> None:
        """Spawn the drain task (lifespan / tests). Idempotent per loop."""
        if self._task is not None and not self._task.done():
            return
        self._nudge = asyncio.Event()
        self._task = asyncio.create_task(self.drain(), name="stack-drain")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # -- ops surface --------------------------------------------------------
    async def drain_now(self, n: int = 1) -> dict:
        """POST /stack/drain: wake up to `n` parked callers, limits respected."""
        n = max(0, int(n or 0))
        woke = 0
        for _ in range(n or 1):
            if n and woke >= n:
                break
            woke += await self.wake_once()
            if not self.stack.items:
                break
        return {"woken": woke, "depth": len(self.stack.items),
                "enabled": self.enabled}

    def drop_at(self, index: int) -> dict:
        """DELETE /stack/{i}: release a parked caller with no grant (-> 429)."""
        if not (0 <= int(index) < len(self.stack.items)):
            return {"dropped": False, "reason": "no such entry", "index": int(index)}
        entry = self.stack.items[int(index)]
        self.stack.drop(entry)
        return {"dropped": True, "index": int(index), "alias": entry.alias,
                "provider": entry.provider or None, "reason": entry.reason}

    def snapshot(self, entries: int = 20) -> dict:
        out = {"enabled": self.enabled, "policy": self.cfg.policy,
               "wait": self.cfg.wait, "park_on": list(self.cfg.park_on),
               "reparks": self.cfg.repark, "draining": self._task is not None
               and not self._task.done()}
        out.update(self.stack.snapshot(entries=entries))
        return out
