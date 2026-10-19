# SMSocket

Self-hosted **LLM API aggregation gateway**: one OpenAI-compatible endpoint in front of
many providers and many API keys per provider — rotation, failover, usage accounting and a
built-in dashboard.

```
client (OpenAI SDK / curl / any app)
   │  POST /v1/chat/completions   Authorization: Bearer <master key>
   │  POST /v1/responses          the same, on the Responses API surface
   │  POST /v1/messages           the same, on the Anthropic Messages surface
   ▼
SMSocket ── key pool      (rotation, 401/429 cooldown, per-key max_rpm)
           ── routing       (priority | round_robin | weighted, cross-model failover)
           ── adapters      (openai chat  <->  openai responses  <->  anthropic)
           ── gate + meter  (max_concurrency, queueing, live in-flight)
           ── batch/async   (parallel send, background jobs)
           ── discovery     (test-message probe -> models + parameters, automatic)
           ── usage store   (SQLite -> /stats + /v1/usage + dashboard)
   ▼
provider A / provider B / provider C ...
```


## EGC v1：出站标准层（`SMSocket/egc.py`）

**DSM 省的是内部那一跳，EGC 省的是出网那一跳。** 信封物化后的 body 在发给提供商之前，
一律过 `egc.egress()`——它是出网的最后一道，`router._request()` 是唯一收口（chat /
流式 / anthropic / responses / embeddings 全走它）。

- **默认档＝实测零语义损失**：R1 线格式、R2 稳定键序、R3c 保留 required、R4 lane 工具子集、
  R5 content 收敛、R6 调用 id 短化、R7 删 null/默认值、R8 易变内容挪尾部、R9 省默认参数、
  R10 稳定前缀复用。删 description 一类**有精准度代价**的开关默认关，按 lane 显式打开。
- **往返无损**：R6 的短 id 只存在于线上，响应（含流式 delta）经 `egc.restore_ids()`
  换回原 id——`tests/test_egc.py` 与 `test_dsm_integration.py::test_I2` 两面夹住。
- **毫秒门**：`budget_ms`（默认 5ms）是这一层的验收条件，超预算计入 `over_budget` 账。
  实测 40 工具＋20 轮（18.8 KB 线宽）p50 0.52ms / p95 0.68ms。
- **基线口径（别拿错）**：接 EGC 之前路由器走 httpx `json=`，它**本来就是**紧凑＋非转义，
  所以规范 §0 的「−38.8% 线上字节」是相对 SMS 客户端 `json.dumps` 默认档那一跳；
  在 SMSocket 这一侧实测省 7.9% 线宽（R6/R1⁺/R7/R9），真收益在 token 与缓存命中（R2/R8）。
- **可回滚**：`egc.enabled: false` → `wire is None` → 交回 httpx 的 `json=`，逐字节旧行为。
- 观测：`/healthz` 的 `egc` 段（档位、p50/p95、超预算次数、直通次数）；
  离线验收 `python -B -m SMSocket.egc selftest`。

## Features (v0.3)

- **OpenAI-compatible API**: `POST /v1/chat/completions` (JSON + SSE streaming), `GET /v1/models`
- **Multi-key pools**: a failing key (`401/403/408/429/5xx/529`) is parked for `cooldown`
  seconds and the request fails over to the next candidate automatically
- **Routing strategies**: `priority` (fallback chain), `round_robin`, `weighted`
- **Alias mapping**: publish `gpt-4o-mini` while upstream ids stay private; the same alias on
  two providers = automatic failover between them
- **Anthropic bridge**: `style: anthropic` providers are served as OpenAI chat format,
  including SSE translation and token accounting
- **Per-key `max_rpm`** limiting and `${ENV_VAR}` secret expansion (no keys in the file)
- **Observability**: `/stats` (per provider / per model / recent calls), `/pool` (live key
  health), `/v1/usage?days=14` (daily totals + estimated cost), dashboard at `/`
- **Estimated cost per call**: a `pricing:` table (per 1M tokens, `'*'` = default row) is
  applied to every logged call, so `/v1/usage` and the dashboard show spend per model,
  provider and day
- **Embeddings**: declare `embeddings:` on any OpenAI-compatible provider and the alias is
  published in `/v1/models` and served by `POST /v1/embeddings` with the same failover rules
- **Spec-faithful streaming**: every SSE chunk is rewritten to carry
  `object: "chat.completion.chunk"` plus the public alias, so strict OpenAI SDK clients work
- **Safe `POST /admin/reload`**: a missing or provider-less config is refused (409) instead of
  wiping live routing, and key health (fails / cooldown) carries over across the reload

### v0.3 — billing currency, discovery, parallel/async

- **Billing currency of record** (`billing:` block): choose any display currency, keep a rate
  table, and price each provider in *its own* currency — a USD-priced provider and a
  CNY-priced provider add up into one correct total. `GET /admin/billing/convert` dry-runs a
  conversion, `POST /admin/billing/rates/refresh` pulls a live rate table, and
  `/stats` / `/v1/usage` take `?currency=` to re-render any report on the fly
- **Model discovery by test message**: `POST /admin/discover` asks a provider what it really
  serves (`GET /v1/models`), sends a minimal chat per id to prove the key can use it,
  measures latency + token usage, then probes the parameter set (`temperature`, `top_p`,
  `tools`, `response_format`, `reasoning_effort`, `stream`, ...) and classifies each as
  supported / rejected / unknown. Run it as a background job and poll
  `/admin/discover/status`; the catalog is cached (`GET /admin/models`) and
  `POST /admin/models/apply` publishes probed ids as aliases — add-only, never overwriting
  an alias a client already depends on
- **Parallel sending**: `POST /v1/batch` fires N payloads at once under its own
  `concurrency` cap, keeps input order, prices every item, and reports per-item status
  instead of failing the whole call; `fail_fast` stops after the first error
- **Async processing**: `POST /v1/batch?async=1` returns a job id immediately; poll
  `GET /v1/batches/{job}` (progress, partial results, `wait=5` blocks until done),
  list with `GET /v1/batches`, cancel with `DELETE /v1/batches/{job}`
- **Concurrency metering + admission gate**: `GET /concurrency` reports live in-flight,
  peak, queue depth, average wait, rejections and a per-provider breakdown;
  `max_concurrency` / `queue_wait` / `per_provider_concurrency` cap simultaneous upstream
  calls, and overflow gets a clean `429` + `Retry-After` instead of a pile-up

## Features (v0.4)
- **Clash net plane**: score every egress path (direct vs. each proxy/node) per provider and
  send each call through the best one — a provider only reachable through a proxy outranks a
  dead direct route. `clash.enabled: false` by default; nothing changes until you turn it on.
- **Stack scheduler**: instead of refusing the moment a limit bites, park the caller on a
  LIFO/FIFO/priority stack and wake it when capacity frees up. `stack.enabled: false` keeps
  today's behaviour (saturated → immediate 429). Parked callers still get a 429 after
  `stack.wait`, now carrying how long they waited.
- **Model + reachability assessment**: `assess` mirrors the traffic you already serve
  (`live`, free) *and* runs scheduled sweeps that really call each model over each egress
  path (`probe`), producing per-cell verdicts — healthy / slow / blocked / unstable — so
  "is this model usable from this network?" is answered from measurements, not folklore.
- **Dashboard**: the socket page gains stack counters and a reachability heat table
  (model × egress); both blocks stay out of the DOM entirely while disabled.

### 内部网络接口 / internal net plane
`/internal/net/*` is the operator surface for the clash plane (`state`, `proxies`, `probes`,
`scores`, `select`, `mode`, `flush`). It is mounted with `include_in_schema=False`: it appears
in neither `/openapi.json` nor `/docs`, and the dashboard exposes it only as a muted footer
glyph that is absent from the DOM when `clash.enabled` is false. It is authenticated by the
same master key as everything else — the discretion is about not advertising the surface,
not about replacing auth.

```bash
curl -H "Authorization: Bearer $KEY" localhost:8000/internal/net/state
curl -X POST -H "Authorization: Bearer $KEY" localhost:8000/internal/net/probes   # re-measure
curl -X PUT  -H "Authorization: Bearer $KEY" -d '{"group":"PROXY-GRP","node":"hk-1"}' \
     localhost:8000/internal/net/select
```

### Assessment + scheduling examples
```bash
# verdicts for the last 24h, per model x egress path
curl -H "Authorization: Bearer $KEY" localhost:8000/assess

# sweep now (async, then poll) / change the schedule live
curl -X POST -H "Authorization: Bearer $KEY" -d '{"async":true}' localhost:8000/assess/run
curl -X PUT  -H "Authorization: Bearer $KEY" -d '{"interval_s":900,"at":"03:30"}' \
     localhost:8000/assess/schedule
```

## v0.5 — what the console does by itself

**No more 401 in the dashboard.** Opening `http://127.0.0.1:8011/` from the same
machine mints a `sms_console` session cookie (HttpOnly, SameSite=Strict, 12h) and
every panel then authenticates with it — the key box is only needed by remote
callers, and a typed key is remembered in the browser. A forged cookie is still
refused, and non-loopback clients still need `Authorization: Bearer <key>`.

**The theme is saved, not remembered by one browser.** `ui.skin` lives in the
config file; clicking a skin writes it (`PUT /admin/ui`) and the page is rendered
with it, so a new profile, a private window or another machine gets the same skin.

**The currency list is never empty.** The rate table (30 currencies + symbols) is
rendered into the page, so the billing selects are populated even before the
first authenticated call; `PUT /admin/billing` and `POST /admin/billing/rates/refresh`
still refine it.

**Adding a model probes it.** `POST /admin/providers` (and the edit form) now runs
probe → publish aliases → measure, in the background (`discover.on_add`, default on).
You do not press "探测", you do not copy ids into the alias box. The response says
`"auto_discover": "queued"`.
**Probing has no page of its own any more.** The console's second tab is
`提供商与大模型` (was `提供商与API`): level 1 is the provider, clicking a row expands
level 2 — every model that provider serves, with the alias it is published under,
availability, latency, context window and the parameter verdicts (`temperature`,
`tools`, `response_format`, 流式 …). Nothing there is typed by hand and there is no
"开始探测" button: opening the page calls `POST /admin/provider-models/refresh`, which
re-probes only the providers whose cache is older than `discover.fresh_seconds`
(default 900s) or that were never probed, and polls `/admin/discover/status` until
the tree repaints. `GET /admin/provider-models` is the tree itself; a model a probe
found but nobody published shows as `未发布`.

**SMSC is configured in 总设置 → 扩展程序.** The block is labelled **SMSC 网络平面**
in the UI (it used to read "Clash 网络平面"; the `clash:` key in `config.yaml` and the
`/admin/config` payload are the wire contract and did not change). The form
(controller, secret, mixed port, mode, groups, per-provider egress, health url,
smart, thresholds) sits under the 扩展程序 heading; saving rebuilds the egress plane
immediately. smsc itself now routes on demand: only sites unreachable from
mainland China / Hong Kong go through the proxy, everything else is direct. The discreet
`/internal/net` panel stays for live switching. A stored secret is never echoed
back — leaving the field blank keeps it.

The 网络平面 block also carries an **打开 SMSC** button: it opens the smsc console
(`http://<controller host:port>/ui/`) built from the controller field, so the two
control planes are one click apart.

**Extensions are bundles, not code drops.** Under 扩展程序 the **导入扩展程序** form takes
an **扩展程序路径** - a directory holding an `asset/` folder with `extension.json`
(name / title / version / panel) and `panel.html`, the bundle's own panel. Import only
succeeds when `asset/SMSocket.identity` equals this install's unique code (minted once
into `smsocket.identity` beside the config, masked in every response, never committed);
**配对并导入** writes that code into the bundle for you. Verification is re-run on every
read, so a bundle copied to another machine drops back to 未验证 and
`GET /admin/extensions/{name}/panel` answers 403 - an unverified bundle can never paint
UI inside the console. Verified bundles are appended to the 扩展程序 entries
automatically and their panel is mounted in an iframe (smsc is the first such bundle;
its `asset/` folder lives in the smsc repository).

**Two OpenAI surfaces, both directions.** `/v1/responses` (create, stream,
retrieve, delete) is served even when the upstream only has chat completions, and
a provider declared `style: openai-responses` serves `/v1/chat/completions` too.
Streaming uses the Responses named events (`response.created`,
`response.output_text.delta`, `response.completed`), not the chat sentinel.

**A third surface: Anthropic Messages.** `POST /v1/messages` answers in Anthropic's shape
(`system` + block `content` + `max_tokens` in, a `content` block list + `stop_reason` out,
`message_start` / `content_block_delta` / `message_stop` over SSE) no matter what the
upstream speaks — chat, responses or anthropic. An Anthropic-style client (opencode,
kilocode) points its anthropic provider `baseURL` at this gateway and its API key at a
master key, authenticated via `x-api-key` as well as `Authorization: Bearer`. `system`
becomes a system message, `tool_result` becomes a `role: tool` message, `tool_use` becomes
`tool_calls`; tool definitions and `tool_choice` map to OpenAI's function schema.

**Weight and priority are measured, not typed.** `tune.enabled` (default on)
ranks providers from the last window of *real conversations* plus the scheduled
probe sweeps: `score = ok_ratio·0.55 + speed·0.30 + throughput·0.15`, normalised
inside the window and shrunk toward neutral below `min_samples`. The ranking goes
into the live pool (`GET /tune`, `POST /tune/apply`, shown as 自动 in the provider
table); the numbers in the config file stay yours unless `tune.persist: true`.
A provider with `auto: false` keeps exactly what you typed.

## Launch entry points (kernel start / stop / status)

The gateway core is started through per-shell entry points in the repo root. Every one of
them resolves the interpreter in this order: `SMSSOCKET_PYTHON` -> `.venv` -> `python`,
and prints the web URL, the `/v1` base URL and the client key in effect. Detached runs
write `runtime/SMSocket.pid` + `runtime/SMSocket*.log` (gitignored).

| shell | start | stop | status |
|---|---|---|---|
| PowerShell | `./start.ps1 [-Background] [-Port 8011] [-Config f] [-Reload] [-NoKey] [-RotateKey]` | `./stop.ps1 [-Force]` | `./status.ps1 [-Tail 15]` |
| bash / zsh | `./start.sh [-d] [-p 8011] [-c f] [--reload] [--no-key] [--rotate-key]` | `./stop.sh` | `./status.sh` |
| cmd.exe | `start.cmd [any run.py flag]` | Ctrl-C (foreground) | - |
| python | `python run.py ...` / `python -m SMSocket ...` | Ctrl-C | - |

```powershell
./start.ps1 -Background    # detached kernel; banner + runtime\SMSocket.log
./status.ps1               # up (pid ...) + web/api/key + last log lines
./stop.ps1                 # stops the kernel and its --reload children
```

```bash
./start.sh -d && ./status.sh && ./stop.sh
```

### v0.3 examples

```bash
# billing: display in CNY, price providers in their own currency, re-render in EUR
curl -X PUT localhost:8000/admin/billing -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"currency":"CNY","base":"USD","rates":{"USD":1,"CNY":7.1,"EUR":0.92}}'
curl "localhost:8000/v1/usage?days=30&currency=EUR" -H "Authorization: Bearer $KEY"
curl "localhost:8000/admin/billing/convert?amount=10&frm=USD&to=CNY" -H "Authorization: Bearer $KEY"

# the two-level tree the console renders (provider -> its models + parameters)
curl "localhost:8000/admin/provider-models" -H "Authorization: Bearer $KEY"
# auto-probe the stale ones only - the console fires this on page load, no button
curl -X POST localhost:8000/admin/provider-models/refresh -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{}'
# discovery: what does this provider really serve, and with which parameters
curl -X POST localhost:8000/admin/discover -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"providers":["openai"],"probe_params":true,"concurrency":8,"async":1}'
curl localhost:8000/admin/discover/status -H "Authorization: Bearer $KEY"
curl -X POST localhost:8000/admin/models/apply -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"providers":["openai"]}'

# parallel batch (waits) / background job (polls)
curl -X POST localhost:8000/v1/batch -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"concurrency":8,"requests":[{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]},
       {"model":"gpt-4o-mini","messages":[{"role":"user","content":"again"}]}]}'
curl -X POST "localhost:8000/v1/batch?async=1" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"requests":[...],"concurrency":16}'   # -> 202 {"job":...}
curl "localhost:8000/v1/batches/job_xxx?wait=5" -H "Authorization: Bearer $KEY"
curl -X DELETE localhost:8000/v1/batches/job_xxx -H "Authorization: Bearer $KEY"

# live concurrency
curl localhost:8000/concurrency
```

`-Background` / `-d` refuses to double-start (it reports the live pid instead), and
`stop.*` also clears children so `--reload` does not leave an orphan worker behind.

## v0.6 — DSM v1 envelope (additive layer, off by default)

DSM (Delta Session Mesh) is a second surface next to the OpenAI one: the client sends
an **envelope of references and policy** (`sch` fingerprint, `mem` chain-fragment ids,
positional `d` turns, `fan`/`lane`/`dep`), and the gateway materialises it into whatever
the picked provider actually speaks. Nothing about `/v1/chat/completions` changes while
the switches are off — that is the point of the truth table below.

```yaml
dsm:
  enabled: false                    # serve /v1/dsm/* (404 when false)
  openai_compat: true               # keep /v1/chat/completions + /v1/responses (410 when false)
  schema_store: runtime/dsm_schemas.json   # ref -> {system, tools}; sessions sit beside it
  max_materialize_bytes: 2000000    # 413 above this (a runaway SYS costs bytes, not silence)
  budget_map: {low: 1024, mid: 4096, high: 16384}   # server-side thinking tiers
  require_registered_schema: false  # true = refuse chat for an unregistered sch
```

### Switch truth table (both toggles, both sides)

| `enabled` | `openai_compat` | legacy `/v1/chat/completions` | `/v1/dsm/*` | who wins |
|---|---|---|---|---|
| false | true (default) | served, byte-for-byte as today (acceptance A) | **404** → client downgrades to legacy + one warning (H) | legacy |
| true | true | served unchanged | served; response decodes to OpenAI shape, tool loop intact (B) | both — safe pair |
| true | false | **410** with the fix hint (C) | served | DSM only |
| false | false | — | — | **refused at config load** (`ValueError`, D) — both roads closed would brick the gateway |

`dsm.enabled=false` + `openai_compat=false` can never start: `dsm_from_raw()` raises, so a
typo in the file is a loud boot failure rather than a gateway that answers nothing.

### Endpoints

| path | body | notes |
|---|---|---|
| `POST /v1/dsm/chat` | envelope, `Content-Type: application/dsm+json` | 415 wrong type · 422 invalid envelope · 409 water-level mismatch · 413 over the byte cap |
| `POST /v1/dsm/schema` | `{ref, system, tools}` | first-frame registration, idempotent; `ref` must equal the content fingerprint |
| `GET /v1/dsm/schema?ref=sha1:…` | — | inspect a stored schema (debug) |
| `POST /v1/dsm/fan` | envelope with `fan` | dry-run returns the split; `?run=1` submits the ready lanes to `/v1/batch` |
| `GET /healthz` | — | now carries `dsm: {enabled, openai_compat, schemas, sessions, …}` |

`out: delta` streams `application/x-ndjson` — one envelope per `seq`, last frame metadata-only
(usage/stop), so the client's `merge_stream` never double-counts the answer text.

### Three-way accounting (why this release exists)

`calls` gains `sid, cid, lane, skill, "in", "out_reason", "out_answer", cache_read, cache_write`
(`in` is a reserved word, hence the quoting). Old databases migrate in place. Two defects closed:

* provider cache/reasoning counts were never extracted (`upstream._details`, `_resp_usage`) —
  emitted only when non-zero, so legacy response shapes stay byte-identical;
* `billing.cost()` read a `cache_read` rate and then ignored it — cached tokens are now priced
  at that rate, with `prompt = miss + hit + write` partitioned so no token is billed twice.

`GET /v1/usage` is unchanged; `usage.dsm_summary()` / `usage.by_lane()` answer
"which task row spent this, and did the cache actually hit?".

### What happens when the two sides disagree

| situation | server answers | client does |
|---|---|---|
| no DSM route / disabled | 404, 415 | downgrade to legacy + one warning, **and latch a 120 s cooldown** so the next turns stop paying 4 wasted round-trips |
| water-level mismatch | 409 | drop its delta mark, resend the whole history once (`resync`); only a second failure downgrades + latches |
| invalid envelope | 422 | report the error — **never** latch (that is our bug, not a missing server; hiding it for 120 s would lose the signal) |
| body over the byte cap | 413 | report; the cap is a guard, not a downgrade trigger |
| server comes back | 200 | success clears the latch; `dsm.py on/off` clears it too (an operator flipping the switch means it) |

`dsm.py status` prints `degraded: {seconds_left, reason}` while the latch is live — a silent
fallback is undiagnosable, and "I enabled DSM but it still posts OpenAI bodies" is exactly the
question status has to answer.

### Keeping the two codecs identical

`SMSocket/dsm.py` is a **generated** copy:

```sh
python -B tools/gen_server_dsm.py \
    "$SMS_CORE/skill/scripts/dsm.py" SMSocket/dsm.py \
    tools/dsm_head.py tools/dsm_tail.py
```

(`$SMS_CORE` = `~/.kilocode/skills/skill_manage_system`; `SMS_CORE_SCRIPTS` is what
`tests/test_dsm_integration.py` reads.) It extracts the shared algorithms verbatim from the
SMS core file and aborts if a name or marker vanished. `tools/dsm_head.py` / `dsm_tail.py`
are the server-only preamble and tail — they are **in the repo on purpose**: regenerating
must be reproducible from a clean checkout, not from some session's scratch directory. `tests/test_dsm_drift.py` then fails if either side is hand-edited (verified by
mutation: changing one hex slice or reordering `ROLE` turns it red). Server-only additions
(`SessionStore`, `encode_response`, `materialize`) live in the tail; client-only concerns
(`build_env`, chain reads, settings) must not appear server-side — also asserted.

Chain memory is resolved on the SMS machine only. Server-side `mem` ids cannot resolve, so
`canon_mem` returns empty **and counts** `mem_unsupported` (surfaced on
`x-dsm-mem-unsupported` and `/healthz`) — never silently treated as "no memory".

## Renamed from `llm_router` (v0.2 → v0.3)

| old | new |
|---|---|
| package `llm_router/` | package `SMSocket/` |
| env `LLMROUTER_CONFIG` / `_NO_KEY` / `_MASTER_KEY` / `_KEY_FILE` | `SMSSOCKET_*` (old names still read as fallback) |
| key file `router.key` | `smsocket.key` (an existing `router.key` keeps working) |
| generated key prefix `sk-router-` | `sk-socket-` |
| stream header `x-router-upstream` | `x-socket-upstream` (both sent) |

Routes, config keys and the SQLite file are unchanged, so an existing deployment only has to
restart; `import llm_router` is what breaks, and that is the point of the rename.

## Quick start

```bash
pip install -r requirements.txt
copy config.example.yaml config.yaml      # Windows (cp on *nix), then edit providers
set OPENAI_API_KEY=***                    # ${...} is expanded from the environment
python run.py                             # http://127.0.0.1:8000  (dashboard at /)
```
On startup the console prints a banner: web dashboard URL, `.../v1` base URL, the
client key in effect, and provider/key/model counts. If `master_keys` is empty the
gateway mints a random `sk-socket-<32 bytes>` key and stores it in `smsocket.key`
next to the config, so restarts and `--reload` keep the same key (it is gitignored).
Resolution order: config `master_keys` > `SMSSOCKET_MASTER_KEY` > `smsocket.key`
(generated on first run). Flags: `--no-key` runs unauthenticated, `--rotate-key`
mints a fresh key and overwrites the file.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-socket-..." -H "content-type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

OpenAI SDK:

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="***")
c.chat.completions.create(model="demo", messages=[{"role": "user", "content": "hi"}])
```
