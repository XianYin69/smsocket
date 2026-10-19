"""Concurrency: live in-flight meter + admission gate (asyncio, no threads).

Two separate concerns, one module:

  Meter  - counts what is happening *right now*: in-flight requests, per
           provider, queue depth, peak, wait time, rejections. Read by
           GET /concurrency and the dashboard.
  Gate   - admission control. `max_concurrency` caps simultaneous upstream
           calls; extra callers wait up to `queue_wait` seconds, then get a
           429 (SMSocketBusy) instead of piling onto the event loop.

Everything is single-loop and lock-free: asyncio tasks only switch at await
points, so the counters below are consistent without a threading.Lock. Leases
are released exactly once (idempotent) even when a stream is abandoned.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class SMSocketBusy(Exception):
    """Gate is saturated and the caller refused to wait any longer."""

    def __init__(self, message: str, retry_after: float = 1.0,
                 parked_ms: float = 0.0, reason: str = ""):
        super().__init__(message)
        self.retry_after = retry_after
        self.parked_ms = round(max(0.0, float(parked_ms or 0.0)), 1)
        self.reason = reason or "saturated"


@dataclass
class Meter:
    active: int = 0
    peak: int = 0
    total: int = 0
    queued: int = 0
    rejected: int = 0
    errors: int = 0
    wait_ms_sum: float = 0.0
    wait_ms_peak: float = 0.0
    dur_ms_sum: float = 0.0
    dur_ms_peak: float = 0.0
    t0: float = field(default_factory=time.time)
    by_provider: dict[str, dict] = field(default_factory=dict)

    def enter(self, provider: str) -> None:
        self.active += 1
        self.total += 1
        self.peak = max(self.peak, self.active)
        row = self.by_provider.setdefault(
            provider or "-", {"active": 0, "peak": 0, "total": 0, "errors": 0})
        row["active"] += 1
        row["total"] += 1
        row["peak"] = max(row["peak"], row["active"])

    def exit(self, provider: str, ms: float, error: bool = False) -> None:
        self.active = max(0, self.active - 1)
        row = self.by_provider.get(provider or "-")
        if row:
            row["active"] = max(0, row["active"] - 1)
            if error:
                row["errors"] += 1
        if error:
            self.errors += 1
        self.dur_ms_sum += ms
        self.dur_ms_peak = max(self.dur_ms_peak, ms)

    def note_wait(self, ms: float) -> None:
        self.wait_ms_sum += ms
        self.wait_ms_peak = max(self.wait_ms_peak, ms)

    def snapshot(self, limits: dict | None = None) -> dict:
        n = max(self.total, 1)
        d = {"active": self.active, "peak": self.peak, "total": self.total,
             "queued": self.queued, "rejected": self.rejected, "errors": self.errors,
             "avg_ms": round(self.dur_ms_sum / n, 1), "max_ms": round(self.dur_ms_peak, 1),
             "avg_wait_ms": round(self.wait_ms_sum / n, 2),
             "max_wait_ms": round(self.wait_ms_peak, 1),
             "uptime_s": round(time.time() - self.t0, 1),
             "rps": round(self.total / max(time.time() - self.t0, 1e-6), 3),
             "by_provider": {k: dict(v) for k, v in sorted(self.by_provider.items())}}
        if limits:
            d.update(limits)
        return d


@dataclass
class Lease:
    """Holds the acquired semaphore(s); release() is idempotent."""
    gate: "Gate"
    provider: str
    alias: str
    t0: float
    parked_ms: float = 0.0     # time spent on the stack before admission
    grabbed: bool = False      # permits came from Gate.try_grab(), not acquire()
    _done: bool = False

    def release(self, error: bool = False) -> None:
        if self._done:
            return
        self._done = True
        ms = (time.perf_counter() - self.t0) * 1000
        self.gate._release(self, ms, error)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release(bool(exc[0]))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release(bool(exc[0]))


class Grab:
    """Result of Gate.try_grab(): bool-like *and* awaitable.

    Grabbing must stay synchronous - a permit checked at an await point can be
    stolen before it is taken - but StackScheduler.take_slot() has shipped in
    both flavours (`return self.gate.try_grab(p)` and `return await ...`).
    Awaiting a plain bool raises TypeError, drain() logs it and the stack never
    empties, so the verdict answers to both callers.
    """

    __slots__ = ("ok",)

    def __init__(self, ok: bool) -> None:
        self.ok = bool(ok)

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"Grab({self.ok})"

    def __await__(self):
        yield from ()            # never yields: awaiting gives the verdict now
        return self.ok


class Gate:
    """Admission control over the upstream call path."""

    def __init__(self, max_concurrency: int = 0, queue_wait: float = 30.0,
                 per_provider: int = 0, meter: Meter | None = None) -> None:
        self.max_concurrency = max(0, int(max_concurrency or 0))
        self.queue_wait = max(0.0, float(queue_wait or 0.0))
        self.per_provider = max(0, int(per_provider or 0))
        self.meter = meter or Meter()
        self._sem: asyncio.Semaphore | None = None
        self._prov: dict[str, asyncio.Semaphore] = {}
        self._loop = None
        # stack scheduler (stacksched.StackScheduler) or None = plain 429 path
        self.stack = None
        self.grabbed = 0
        # grabs are scoped to a loop generation: _global_sem() rebuilds on a
        # loop swap (uvicorn reload / TestClient) and a permit taken on the
        # dead loop must never be released into the fresh semaphore
        self._grab_loop = None

    # -- internals ----------------------------------------------------------
    def _global_sem(self) -> asyncio.Semaphore | None:
        if not self.max_concurrency:
            return None
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            # a new event loop (uvicorn reload / TestClient) -> rebuild
            self._sem = asyncio.Semaphore(self.max_concurrency)
            self._prov = {}
            self._loop = loop
        return self._sem

    def _provider_sem(self, provider: str) -> asyncio.Semaphore | None:
        if not self.per_provider or not provider:
            return None
        sem = self._prov.get(provider)
        if sem is None:
            sem = self._prov[provider] = asyncio.Semaphore(self.per_provider)
        return sem

    def _release(self, lease: Lease, ms: float, error: bool) -> None:
        if lease.grabbed:
            # the reservation this lease fulfils is no longer outstanding
            self.grabbed = max(0, self.grabbed - 1)
        self.meter.exit(lease.provider, ms, error)
        if lease.provider and lease.provider in self._prov:
            self._prov[lease.provider].release()
        if self._sem is not None:
            try:
                self._sem.release()
            except ValueError:      # released after a loop swap
                pass

    # -- stack admission ----------------------------------------------------
    def attach_stack(self, stack) -> None:
        """Point the gate at a StackScheduler (None restores plain 429s)."""
        self.stack = stack

    @staticmethod
    def _try_take(sem: asyncio.Semaphore) -> bool:
        """Take one permit without waiting; False when none is free.

        asyncio.Semaphore has no acquire_nowait, so this repeats the fast path
        of acquire(): `locked()` is True when the counter is exhausted *or* a
        task is already queued, so a grab never overtakes a regular waiter.
        """
        if sem.locked() or not hasattr(sem, "_value"):
            return False
        sem._value -= 1
        return True

    @staticmethod
    def _give_back(sem: asyncio.Semaphore) -> None:
        """Return a permit taken by _try_take (no Lease exists at that point)."""
        try:
            sem.release()
        except (ValueError, RuntimeError):      # loop swapped/closed mid-grab
            pass

    def try_grab(self, provider: str = "") -> "Grab":
        """Reserve admission for a caller the stack is about to wake.

        Synchronous on purpose: StackScheduler.take_slot() may call this without
        an await, so an `async def` here would hand back a coroutine - truthy
        either way, so the waker would "grant" an entry holding no permit and the
        concurrency cap would silently disappear. It takes exactly the permits
        acquire() would have taken, so the woken caller must not acquire again;
        its Lease.release() hands them back once through _release().
        """
        sem = self._global_sem()                 # rebuilds on a new event loop
        if self._grab_loop is not self._loop:
            self._grab_loop = self._loop         # old generation died with it
            self.grabbed = 0
        psem = self._provider_sem(provider)
        if sem is not None and not self._try_take(sem):
            return Grab(False)
        if psem is not None and not self._try_take(psem):
            if sem is not None:
                self._give_back(sem)             # never hold half a slot
            return Grab(False)
        if sem is not None or psem is not None:
            self.grabbed += 1
        return Grab(True)

    def release_grab(self, provider: str = "") -> None:
        """Undo a try_grab() whose caller never arrived (drop / cancel / timeout).

        Only permits come back: the meter is entered by _park() once the woken
        caller resumes, so an abandoned grab never touched it. Bounded by the
        grab counter and the loop generation, so a stale release cannot inflate
        a freshly rebuilt semaphore past max_concurrency.
        """
        if self.grabbed <= 0 or self._grab_loop is not self._loop:
            return
        self.grabbed -= 1
        if provider and provider in self._prov:
            self._give_back(self._prov[provider])
        if self._sem is not None:
            self._give_back(self._sem)

    async def _park(self, alias: str, provider: str, reason: str,
                    message: str, retry_after: float) -> Lease:
        """Join the stack instead of refusing; raises the 429 when it fails.

        The park budget is min(stack.wait, queue_wait): a caller that refuses to
        queue longer than queue_wait must not be parked any longer either, or
        the 429 latency would grow past the budget the client configured.
        """
        stack = self.stack
        limit = None if self.queue_wait <= 0 else min(stack.wait, self.queue_wait)
        entry = await stack.park(alias, provider, reason, timeout=limit, grab=True)
        if entry.granted and entry.grabbed:
            # the waker already holds the permits - account for the wait exactly
            # like the fast path so active / avg_wait_ms still mean what they did
            self.meter.note_wait(entry.parked_ms)
            self.meter.enter(provider)
            lease = Lease(self, provider, alias, time.perf_counter())
            lease.parked_ms = entry.parked_ms
            lease.grabbed = True          # _release() gives the slot back once
            return lease
        # refused after all (timeout / stack_full / dropped): keep the old
        # accounting, every rejection must stay visible in the meter
        self.meter.rejected += 1
        raise SMSocketBusy(f"{message} (parked {entry.parked_ms:.0f}ms, "
                           f"stack depth {len(stack.stack)})", retry_after,
                           parked_ms=entry.parked_ms, reason=entry.reason)

    # -- public -------------------------------------------------------------
    async def acquire(self, alias: str = "", provider: str = "") -> Lease:
        """Wait for admission. Raises SMSocketBusy when the queue is too slow.

        With a stack attached (`stack.enabled: true`) a caller that would have
        been refused is parked instead and woken when the limit frees up; only
        after `stack.wait` seconds does it still get its 429.
        """
        stack = self.stack
        if stack is not None and stack.parks_on("rpm") and provider \
                and stack.rpm_blocked(provider, alias):
            return await self._park(alias, provider, "rpm",
                                    f"provider '{provider}' is rpm-limited", 1.0)
        sem = self._global_sem()
        m = self.meter
        t0 = time.perf_counter()
        queued_here = sem is not None and sem.locked()
        if queued_here:
            m.queued += 1
        try:
            if sem is not None:
                if self.queue_wait <= 0:
                    if sem.locked():
                        if stack is not None and stack.parks_on("saturated"):
                            return await self._park(
                                alias, provider, "saturated",
                                f"gateway saturated ({self.max_concurrency} "
                                f"in flight)", 0.5)
                        m.rejected += 1
                        raise SMSocketBusy(
                            f"gateway saturated ({self.max_concurrency} in flight)", 0.5)
                    await sem.acquire()
                else:
                    try:
                        await asyncio.wait_for(sem.acquire(), self.queue_wait)
                    except asyncio.TimeoutError:
                        msg = (f"queue wait exceeded {self.queue_wait}s "
                               f"({self.max_concurrency} in flight, "
                               f"{m.queued} queued)")
                        if stack is not None and stack.parks_on("saturated"):
                            return await self._park(alias, provider,
                                                    "saturated", msg,
                                                    max(0.5, min(self.queue_wait, 5.0)))
                        m.rejected += 1
                        raise SMSocketBusy(msg, max(0.5, min(self.queue_wait, 5.0)))
            psem = self._provider_sem(provider)
            if psem is not None:
                try:
                    if self.queue_wait > 0:
                        await asyncio.wait_for(psem.acquire(), self.queue_wait)
                    else:
                        await psem.acquire()
                except asyncio.TimeoutError:
                    if sem is not None:
                        sem.release()
                    msg = (f"provider '{provider}' saturated "
                           f"({self.per_provider} in flight)")
                    if stack is not None and stack.parks_on("provider_saturated"):
                        return await self._park(alias, provider,
                                                "provider_saturated", msg, 1.0)
                    m.rejected += 1
                    raise SMSocketBusy(msg, 1.0)
        finally:
            if queued_here:
                m.queued = max(0, m.queued - 1)
        wait_ms = (time.perf_counter() - t0) * 1000
        m.note_wait(wait_ms)
        m.enter(provider)
        return Lease(self, provider, alias, time.perf_counter())

    def limits(self) -> dict:
        out = {"max_concurrency": self.max_concurrency or None,
               "per_provider_concurrency": self.per_provider or None,
               "queue_wait": self.queue_wait,
               "saturated": bool(self.max_concurrency and
                                 self.meter.active >= self.max_concurrency)}
        # only with a stack attached: the disabled payload stays byte-identical
        if self.stack is not None:
            out["grabbed"] = self.grabbed
            out["stack"] = self.stack.snapshot(entries=0)
        return out
