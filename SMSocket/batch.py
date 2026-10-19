"""Parallel batch sending + async jobs.

Two ways to push many payloads at once:

  POST /v1/batch            {"requests": [...], "concurrency": 8}   -> one answer
  POST /v1/batch?async=1    -> 202 {"job": "..."}  then poll GET /v1/batches/{job}

Every item goes through the normal Router path, so key rotation, failover,
cooldown, usage accounting and the admission gate all still apply. The batch
adds its own semaphore on top (`concurrency`), so one client cannot monopolise
the gateway, and results always keep their input order.

Jobs live in memory (bounded by `keep`, oldest finished job evicted first); a
restart drops unfinished jobs, which is why the poll response carries the
per-item results accumulated so far.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from .concurrency import SMSocketBusy


class BatchTooLarge(Exception):
    pass


# The batch orchestrator bounds each item so one stuck model cannot hang the
# whole batch.  The router itself never converges; this timeout only applies at
# the batch layer.  A single chat call made directly against the router still
# polls forever.
BATCH_ITEM_TIMEOUT = 5.0


@dataclass
class Job:
    id: str
    total: int
    concurrency: int = 8
    status: str = "pending"          # pending|running|done|cancelled|error
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    results: list[dict] = field(default_factory=list)
    task: "asyncio.Task | None" = None
    fail_fast: bool = False
    error: str = ""

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.get("done"))

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results
                   if r.get("done") and int(r.get("status", 0) or 0) >= 400)

    @property
    def cancelled(self) -> int:
        return sum(1 for r in self.results if r.get("cancelled"))

    def view(self, with_results: bool = True, limit: int = 0) -> dict:
        out = {"job": self.id, "status": self.status, "total": self.total,
               "completed": self.completed, "failed": self.failed,
               "cancelled": self.cancelled,
               "concurrency": self.concurrency, "fail_fast": self.fail_fast,
               "created": round(self.created, 3),
               "started": round(self.started, 3) or None,
               "finished": round(self.finished, 3) or None,
               "elapsed": round((self.finished or time.time())
                                - (self.started or self.created), 3),
               "progress": round(self.completed / self.total, 4) if self.total else 0.0}
        if self.error:
            out["error"] = self.error
        if with_results:
            res = self.results
            if limit:
                res = res[:limit]
            out["results"] = res
        return out


class BatchRunner:
    """Runs batches synchronously (awaited) or as tracked background jobs."""

    def __init__(self, keep: int = 50, max_items: int = 500) -> None:
        self.keep = max(1, int(keep))
        self.max_items = max(1, int(max_items))
        self.jobs: dict[str, Job] = {}

    # ---- core --------------------------------------------------------------
    async def run_items(self, router, items: list[dict], concurrency: int = 8,
                        fail_fast: bool = False, job: Job | None = None) -> list[dict]:
        """Send every item through the router; results keep input order."""
        if len(items) > self.max_items:
            raise BatchTooLarge(f"{len(items)} items, limit {self.max_items}")
        n = len(items)
        stack = getattr(getattr(router, "gate", None), "stack", None)
        reparks = int(getattr(getattr(stack, "cfg", None), "repark", 0) or 0) \
            if stack is not None else 0
        results: list[dict] = [{"index": i, "done": False} for i in range(n)]
        sem = asyncio.Semaphore(max(1, min(int(concurrency or 8), n or 1)))
        stop = asyncio.Event()

        def publish() -> None:
            if job is not None:
                job.results = results

        async def one(i: int, item: dict) -> None:
            slot = results[i]
            slot.update({"custom_id": item.get("custom_id") or "",
                         "model": str(item.get("model") or "")})
            if fail_fast and stop.is_set():
                slot.update({"status": 0, "skipped": True, "done": True})
                return
            async with sem:
                if fail_fast and stop.is_set():
                    slot.update({"status": 0, "skipped": True, "done": True})
                    publish()
                    return
                t0 = time.time()
                tries = 0
                while True:                   # a busy item re-joins the stack
                    try:
                        body = await asyncio.wait_for(
                            router.complete(item), timeout=BATCH_ITEM_TIMEOUT)
                        slot.update({"status": 200, "response": body,
                                     "usage": body.get("usage") or {}})
                        break
                    except asyncio.TimeoutError:
                        # Router itself never converges (single chat keeps
                        # polling), but the batch orchestrator must bound each
                        # item so one stuck model cannot hang the whole batch.
                        slot.update({"status": 504, "reparks": tries,
                                     "error": {"message":
                                               f"upstream did not answer within "
                                               f"{BATCH_ITEM_TIMEOUT:.0f}s",
                                               "type": "gateway_timeout"}})
                        if fail_fast:
                            stop.set()
                        break
                    except SMSocketBusy as e:
                        # A batch item is a patient client: park it instead of
                        # handing back a 429 result, up to `reparks` times.
                        if stack is None or tries >= reparks:
                            slot.update({"status": 429, "reparks": tries,
                                         "error": {"message": str(e),
                                                   "type": "rate_limited"}})
                            if fail_fast:
                                stop.set()
                            break
                        tries += 1
                        slot["reparks"] = tries
                        entry = await stack.park(str(item.get("model") or ""),
                                                 "", "saturated",
                                                 timeout=stack.wait, grab=False)
                        # grab=False: the retry runs gate.acquire itself, so a
                        # pre-granted slot would be taken twice.
                        if not entry.granted:
                            slot.update({"status": 429, "reparks": tries,
                                         "error": {"message":
                                                  f"still busy after {tries} "
                                                  f"re-park(s)",
                                                  "type": "rate_limited"}})
                            if fail_fast:
                                stop.set()
                            break
                    except Exception as e:    # NoUpstream / UpstreamError / httpx
                        status = int(getattr(e, "status", 502) or 502)
                        detail = getattr(e, "detail", None)
                        slot.update({"status": status, "error": {
                            "message": str(detail if detail is not None else e)[:500],
                            "type": "upstream_error" if detail is not None
                            else e.__class__.__name__}})
                        if fail_fast and status != 429:
                            stop.set()
                        break                 # real errors do not retry here
                slot["ms"] = round((time.time() - t0) * 1000, 1)
                slot["done"] = True
                u = slot.get("usage") or {}
                if u:
                    slot["cost"] = router.s.cost_detail(
                        slot["model"], int(u.get("prompt_tokens", 0) or 0),
                        int(u.get("completion_tokens", 0) or 0))
                publish()

        tasks = [asyncio.create_task(one(i, it)) for i, it in enumerate(items)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for slot in results:
                if not slot.get("done"):
                    slot.update({"status": 0, "cancelled": True})
            publish()
            raise
        return results

    # ---- sync mode ---------------------------------------------------------
    async def run(self, router, items: list[dict], concurrency: int = 8,
                  fail_fast: bool = False) -> dict:
        t0 = time.time()
        results = await self.run_items(router, items, concurrency, fail_fast, None)
        return self.summary(results, concurrency,
                            round((time.time() - t0) * 1000, 1), router.s.billing)

    @staticmethod
    def summary(results: list[dict], concurrency: int, ms: float, billing=None) -> dict:
        ok = [r for r in results if r.get("status") == 200]
        bad = [r for r in results
               if r.get("done") and int(r.get("status", 0) or 0) >= 400]
        tokens = sum(int((r.get("usage") or {}).get("total_tokens", 0) or 0)
                     for r in results)
        cost = round(sum(float((r.get("cost") or {}).get("display", 0) or 0)
                         for r in results), 8)
        return {"results": results,
                "summary": {"total": len(results), "ok": len(ok), "failed": len(bad),
                            "skipped": sum(1 for r in results if r.get("skipped")),
                            "cancelled": sum(1 for r in results if r.get("cancelled")),
                            "tokens": tokens, "cost": cost,
                            "currency": billing.currency if billing else "USD",
                            "concurrency": concurrency, "wall_ms": ms,
                            "avg_ms": round(sum(r.get("ms", 0) for r in results) /
                                            max(len(results), 1), 1)}}

    # ---- async jobs --------------------------------------------------------
    def start(self, router, items: list[dict], concurrency: int = 8,
              fail_fast: bool = False) -> Job:
        job = Job(id="job_" + uuid.uuid4().hex[:12], total=len(items),
                  concurrency=max(1, int(concurrency or 8)), fail_fast=fail_fast,
                  status="running", started=time.time(),
                  results=[{"index": i, "done": False} for i in range(len(items))])
        self._evict()
        self.jobs[job.id] = job

        async def body():
            try:
                await self.run_items(router, items, job.concurrency, fail_fast, job)
                job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
            except Exception as e:
                job.status = "error"
                job.error = str(e)[:300]
            finally:
                job.finished = time.time()

        job.task = asyncio.create_task(body())
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.view(with_results=False) for j in
                sorted(self.jobs.values(), key=lambda x: x.created, reverse=True)]

    def running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def cancel(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j or j.status not in ("running", "pending"):
            return False
        if j.task:
            j.task.cancel()
        j.status = "cancelled"
        j.finished = time.time()
        return True

    def _evict(self) -> None:
        finished = [j for j in self.jobs.values() if j.status != "running"]
        while len(self.jobs) > self.keep and finished:
            oldest = min(finished, key=lambda x: x.finished or x.created)
            self.jobs.pop(oldest.id, None)
            finished.remove(oldest)
