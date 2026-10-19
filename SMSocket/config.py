"""Config loading & validation for SMSocket."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .billing import Billing, from_raw as billing_from_raw
from pathlib import Path

LEGACY_ENV = {
    "SMSSOCKET_CONFIG": "LLMROUTER_CONFIG",
    "SMSSOCKET_NO_KEY": "LLMROUTER_NO_KEY",
    "SMSSOCKET_MASTER_KEY": "LLMROUTER_MASTER_KEY",
    "SMSSOCKET_KEY_FILE": "LLMROUTER_KEY_FILE",
}


def envv(name: str, default=None):
    """Read SMSSOCKET_* env, falling back to the pre-rename LLMROUTER_* name."""
    v = os.environ.get(name)
    if v is None:
        v = os.environ.get(LEGACY_ENV.get(name, ""), None)
    return default if v is None else v


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _env_file_fallback() -> dict:
    """Values from SMSSOCKET_ENV_FILE (.env, KEY=VALUE lines) — used only for vars
    the process doesn't already have, so an explicit launch environment always wins."""
    env_file = os.environ.get("SMSSOCKET_ENV_FILE", "")
    if not env_file or not Path(env_file).exists():
        return {}
    out = {}
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def _env(v):
    """Expand ${VAR} / $VAR from the environment, falling back to SMSSOCKET_ENV_FILE
    (.env) for vars the process doesn't already have — a launch environment that
    forgot to export AGNES_KEY_1 no longer silently produces a 0-key pool."""
    fb = _env_file_fallback()

    def _one(name):
        val = os.environ.get(name)
        if val is None:
            val = fb.get(name, "")
        return val
    if isinstance(v, str):
        return _ENV_RE.sub(lambda m: _one(m.group(1) or m.group(2)), v)
    if isinstance(v, list):
        return [_env(x) for x in v]
    if isinstance(v, dict):
        return {k: _env(x) for k, x in v.items()}
    return v

try:
    import yaml
except Exception:  # yaml optional
    yaml = None


@dataclass
class ProviderSpec:
    name: str
    base_url: str
    keys: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)  # public alias -> upstream id
    style: str = "openai"          # openai | openai-responses | anthropic
    weight: int = 1
    priority: int = 0
    timeout: float = 120.0
    max_rpm: int = 0
    enabled: bool = True
    keyless: bool = False          # provider needs no API key (OpenCode free models)
    extra_headers: dict[str, str] = field(default_factory=dict)
    embeddings: dict[str, str] = field(default_factory=dict)  # alias -> upstream embed model
    auto: bool = True                # participates in auto weight/priority tuning

    def aliases(self) -> list[str]:
        return list(self.models) or [self.name]


@dataclass
class ClashConfig:
    """`clash:` block - the optional net plane (see clash.py). Disabled by default.

    Only `controller`/`mixed_port` are needed to start; everything else has a
    safe default. Secrets belong in the environment (`secret: ${CLASH_SECRET}`).
    """

    enabled: bool = False
    controller: str = "http://127.0.0.1:9090"
    secret: str = ""
    mixed_port: int = 7890
    proxy_url: str = ""                      # overrides http://host:mixed_port
    health_url: str = "https://www.google.com/generate_204"
    timeout: float = 5.0
    interval: float = 60.0
    mode: str = "auto"                       # auto | direct | proxy
    groups: list[str] = field(default_factory=list)
    provider_proxy: dict[str, str] = field(default_factory=dict)
    smart: bool = True
    min_delay_ms: float = 0.0
    fail_ratio: float = 0.5

    def proxy_url_for(self) -> str:
        """The http proxy endpoint clash listens on ("" = no proxy path)."""
        if self.proxy_url:
            return self.proxy_url.strip()
        if self.mixed_port:
            host = self.controller.split("//")[-1].split("/")[0]
            host = host.split(":")[0] or "127.0.0.1"
            return f"http://{host}:{int(self.mixed_port)}"
        return ""

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "controller": self.controller,
                "secret": "***" if self.secret else "",
                "mixed_port": self.mixed_port, "proxy_url": self.proxy_url,
                "health_url": self.health_url, "timeout": self.timeout,
                "interval": self.interval, "mode": self.mode,
                "groups": list(self.groups), "provider_proxy": dict(self.provider_proxy),
                "smart": self.smart, "min_delay_ms": self.min_delay_ms,
                "fail_ratio": self.fail_ratio}


def clash_from_raw(raw, default: ClashConfig | None = None) -> ClashConfig:
    """Parse + validate the `clash:` block; a missing block keeps the default."""
    base = default or ClashConfig()
    if not isinstance(raw, dict):
        return base
    modes = ("auto", "direct", "proxy")
    groups = raw.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    hints = raw.get("provider_proxy") or {}
    if not isinstance(hints, dict):
        raise ValueError("clash.provider_proxy must be a mapping")
    mode = str(raw.get("mode", base.mode) or "auto").strip().lower()
    if mode not in modes:
        raise ValueError(f"clash.mode must be one of {modes}")
    out = ClashConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        controller=str(_env(raw.get("controller", base.controller)) or "").strip(),
        secret=str(_env(raw.get("secret", base.secret)) or "").strip(),
        mixed_port=int(raw.get("mixed_port", base.mixed_port) or 0),
        proxy_url=str(_env(raw.get("proxy_url", base.proxy_url)) or "").strip(),
        health_url=str(raw.get("health_url", base.health_url)),
        timeout=float(raw.get("timeout", base.timeout) or 5.0),
        interval=float(raw.get("interval", base.interval) or 60.0),
        mode=mode,
        groups=[str(g).strip() for g in _env(list(groups)) if str(g).strip()],
        provider_proxy={str(k).strip(): str(v).strip()
                        for k, v in _env(dict(hints)).items()
                        if str(k).strip() and str(v).strip()},
        smart=bool(raw.get("smart", base.smart)),
        min_delay_ms=float(raw.get("min_delay_ms", base.min_delay_ms) or 0.0),
        fail_ratio=float(raw.get("fail_ratio", base.fail_ratio) or 0.5))
    if out.timeout <= 0:
        raise ValueError("clash.timeout must be > 0")
    if out.interval < 1:
        raise ValueError("clash.interval must be >= 1 second")
    if not 0 <= out.fail_ratio <= 1:
        raise ValueError("clash.fail_ratio must be between 0 and 1")
    if out.enabled and not out.controller.startswith("http"):
        raise ValueError("clash.controller must be an http:// url when enabled")
    return out



PARK_REASONS = ("saturated", "rpm", "provider_saturated", "cooldown")
STACK_POLICIES = ("lifo", "fifo", "priority")


@dataclass
class StackConfig:
    """`stack:` block - park requests when a limit bites (see stacksched.py).

    Off by default: with `enabled: false` the gate behaves exactly as before
    (saturated caller -> immediate 429). `wait` is how long one caller accepts
    being parked, `park_on` picks which limits may park it at all.
    """

    enabled: bool = False
    max_depth: int = 1000                  # parked callers at once, then 429
    wait: float = 120.0                    # seconds a caller tolerates parking
    policy: str = "lifo"                   # lifo | fifo | priority
    park_on: list[str] = field(
        default_factory=lambda: ["saturated", "rpm", "provider_saturated", "cooldown"])
    interval: float = 0.25                 # drain poll (seconds)
    repark: int = 3                        # batch retries after a 429

    def parks_on(self, reason: str) -> bool:
        return self.enabled and reason in self.park_on

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "max_depth": self.max_depth,
                "wait": self.wait, "policy": self.policy,
                "park_on": list(self.park_on), "interval": self.interval,
                "repark": self.repark}


def stack_from_raw(raw, default: StackConfig | None = None) -> StackConfig:
    """Parse + validate the `stack:` block; a missing block keeps the default."""
    base = default or StackConfig()
    if not isinstance(raw, dict):
        return base
    policy = str(raw.get("policy", base.policy) or "lifo").strip().lower()
    if policy not in STACK_POLICIES:
        raise ValueError(f"stack.policy must be one of {STACK_POLICIES}")
    reasons = raw.get("park_on", base.park_on) or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = [str(r).strip().lower() for r in _env(list(reasons)) if str(r).strip()]
    bad = [r for r in reasons if r not in PARK_REASONS]
    if bad:
        raise ValueError(f"stack.park_on must be a subset of {PARK_REASONS} (got {bad})")
    out = StackConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        max_depth=int(raw.get("max_depth", base.max_depth) or base.max_depth),
        wait=float(raw.get("wait", base.wait) or base.wait),
        policy=policy,
        park_on=reasons or list(PARK_REASONS),
        interval=float(raw.get("interval", base.interval) or base.interval),
        repark=int(raw.get("repark", base.repark) or 0))
    if out.max_depth < 1:
        raise ValueError("stack.max_depth must be >= 1")
    if out.wait <= 0:
        raise ValueError("stack.wait must be > 0 seconds")
    if out.interval < 0.01:
        raise ValueError("stack.interval must be >= 0.01 second")
    if out.repark < 0:
        raise ValueError("stack.reparks must be 0 or more")
    return out


ASSESS_EGRESS = ("auto", "direct")
ASSESS_VERDICTS = ("healthy", "slow", "blocked", "unstable", "unknown")


@dataclass
class AssessConfig:
    """`assess:` block - how we measure model performance and reachability.

    Two sources feed one table (see assess.py): `live` rows harvested from real
    traffic (free, no extra calls) and `probe` rows from a scheduled sweep that
    actually calls the model. `egress: auto` sweeps direct plus every clash
    path, which is what makes "is this model reachable from this network
    environment?" a question we can answer from data instead of folklore.
    """

    enabled: bool = False
    interval_s: int = 3600                 # hours between sweeps
    at: str = ""                           # "HH:MM" daily slot ("" = interval only)
    models: list[str] = field(default_factory=list)      # [] = every alias
    providers: list[str] = field(default_factory=list)   # [] = every provider
    egress: str = "auto"                   # auto | direct | <egress id>
    prompt: str = "Reply with exactly one word: ok"
    max_tokens: int = 8
    concurrency: int = 4
    timeout: float = 20.0
    live: bool = True                      # harvest from real traffic
    live_sample: float = 1.0               # 0..1 share of live calls recorded
    slow_ms: float = 8000.0                # p50 above this = "slow"
    window_s: int = 86400                  # report window

    def as_dict(self) -> dict:
        return {"interval_s": self.interval_s, "max_tokens": self.max_tokens,
                "concurrency": self.concurrency, "timeout": self.timeout,
                "live_sample": self.live_sample, "slow_ms": self.slow_ms,
                "window_s": self.window_s}

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "interval_s": self.interval_s,
                "at": self.at, "models": list(self.models),
                "providers": list(self.providers), "egress": self.egress,
                "max_tokens": self.max_tokens, "concurrency": self.concurrency,
                "timeout": self.timeout, "live": self.live,
                "live_sample": self.live_sample, "slow_ms": self.slow_ms,
                "window_s": self.window_s}


def assess_from_raw(raw, default: AssessConfig | None = None) -> AssessConfig:
    """Parse + validate the `assess:` block; a missing block keeps the default."""
    base = default or AssessConfig()
    if not isinstance(raw, dict):
        return base
    at = str(raw.get("at", base.at) or "").strip()
    if at:
        parts = at.split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts) \
                or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise ValueError("assess.at must be HH:MM (24h) or empty")
        at = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    models = raw.get("models", base.models) or []
    if isinstance(models, str):
        models = [models]
    provs = raw.get("providers", base.providers) or []
    if isinstance(provs, str):
        provs = [provs]
    def num(key, cast):
        """Read one number as configured: absent/null = default, 0 = 0."""
        v = raw.get(key, None)
        return base.as_dict()[key] if v is None else cast(v)

    out = AssessConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        interval_s=int(num("interval_s", int)),
        at=at,
        models=[str(m).strip() for m in _env(list(models)) if str(m).strip()],
        providers=[str(p).strip() for p in _env(list(provs)) if str(p).strip()],
        egress=str(raw.get("egress", None) or base.egress).strip(),
        prompt=str(raw.get("prompt", None) or base.prompt),
        max_tokens=int(num("max_tokens", int)),
        concurrency=int(num("concurrency", int)),
        timeout=float(num("timeout", float)),
        live=bool(raw.get("live", base.live)),
        live_sample=float(num("live_sample", float)),
        slow_ms=float(num("slow_ms", float)),
        window_s=int(num("window_s", int)))
    if out.interval_s < 1:
        raise ValueError("assess.interval_s must be >= 1 second")
    if out.concurrency < 1 or out.concurrency > 64:
        raise ValueError("assess.concurrency must be 1..64")
    if out.timeout <= 0:
        raise ValueError("assess.timeout must be > 0 seconds")
    if out.max_tokens < 1:
        raise ValueError("assess.max_tokens must be >= 1")
    if not 0.0 <= out.live_sample <= 1.0:
        raise ValueError("assess.live_sample must be 0..1")
    if out.window_s < 1:
        raise ValueError("assess.window_s must be >= 1 second")
    return out


SKINS = ("a", "b", "c", "d")


@dataclass
class UIConfig:
    """`ui:` block - console presentation, remembered server-side.

    A browser-only preference (localStorage) is lost in a new profile or a
    private window; the operator asked for the theme to be *saved*, so the
    chosen skin lives in the config file and the page is rendered with it.
    """

    skin: str = "a"

    def as_config(self) -> dict:
        return {"skin": self.skin}


def ui_from_raw(raw, default: UIConfig | None = None) -> UIConfig:
    base = default or UIConfig()
    if not isinstance(raw, dict):
        return base
    skin = str(raw.get("skin", base.skin) or base.skin).strip().lower()
    if skin not in SKINS:
        raise ValueError(f"ui.skin must be one of {SKINS}")
    return UIConfig(skin=skin)


@dataclass
class DiscoverConfig:
    """`discover:` block - probing happens on its own when a model appears.

    The operator adds a provider and walks away; the gateway then probes it,
    caches the catalog, publishes the aliases it found and lets the assessor
    measure them. Every step is opt-out, never opt-in.
    """

    on_add: bool = True
    apply_aliases: bool = True
    auto_assess: bool = True
    concurrency: int = 8
    timeout: float = 30.0
    probe_params: bool = True
    probe_stream: bool = True
    max_models: int = 200
    # how old a cached probe may get before the console asks for a
    # fresh one on page load; 0 = only providers with no data at all
    fresh_seconds: int = 900

    def as_config(self) -> dict:
        return {"on_add": self.on_add, "apply_aliases": self.apply_aliases,
                "auto_assess": self.auto_assess, "concurrency": self.concurrency,
                "timeout": self.timeout, "probe_params": self.probe_params,
                "probe_stream": self.probe_stream, "max_models": self.max_models,
                "fresh_seconds": self.fresh_seconds}


def discover_from_raw(raw, default: DiscoverConfig | None = None) -> DiscoverConfig:
    base = default or DiscoverConfig()
    if not isinstance(raw, dict):
        return base
    out = DiscoverConfig(
        on_add=bool(raw.get("on_add", base.on_add)),
        apply_aliases=bool(raw.get("apply_aliases", base.apply_aliases)),
        auto_assess=bool(raw.get("auto_assess", base.auto_assess)),
        concurrency=int(raw.get("concurrency", base.concurrency) or 1),
        timeout=float(raw.get("timeout", base.timeout) or 1.0),
        probe_params=bool(raw.get("probe_params", base.probe_params)),
        probe_stream=bool(raw.get("probe_stream", base.probe_stream)),
        max_models=int(raw.get("max_models", base.max_models) or 1),
        fresh_seconds=int(raw.get("fresh_seconds", base.fresh_seconds) or 0))
    if not 1 <= out.concurrency <= 64:
        raise ValueError("discover.concurrency must be 1..64")
    if out.timeout <= 0:
        raise ValueError("discover.timeout must be > 0 seconds")
    if not 1 <= out.max_models <= 2000:
        raise ValueError("discover.max_models must be 1..2000")
    if not 0 <= out.fresh_seconds <= 86400 * 30:
        raise ValueError("discover.fresh_seconds must be 0..2592000 seconds")
    return out


@dataclass
class TuneConfig:
    """`tune:` block - weight/priority come from measurement, not from typing.

    Live traffic (every real call the gateway serves) and the scheduled probe
    sweeps are the only inputs; ranking is recomputed on `interval_s` and the
    window is `window_s`. `persist` writes the derived numbers back into the
    config file, off by default so the file stays the operator's intent.
    """

    enabled: bool = True
    interval_s: int = 600
    window_s: int = 86400
    min_samples: int = 3
    persist: bool = False
    priority_spread: int = 10
    weight_spread: int = 10

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "interval_s": self.interval_s,
                "window_s": self.window_s, "min_samples": self.min_samples,
                "persist": self.persist, "priority_spread": self.priority_spread,
                "weight_spread": self.weight_spread}


def tune_from_raw(raw, default: TuneConfig | None = None) -> TuneConfig:
    base = default or TuneConfig()
    if not isinstance(raw, dict):
        return base
    out = TuneConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        interval_s=int(raw.get("interval_s", base.interval_s) or base.interval_s),
        window_s=int(raw.get("window_s", base.window_s) or base.window_s),
        min_samples=int(raw.get("min_samples", base.min_samples) or 1),
        persist=bool(raw.get("persist", base.persist)),
        priority_spread=int(raw.get("priority_spread", base.priority_spread) or 1),
        weight_spread=int(raw.get("weight_spread", base.weight_spread) or 1))
    if out.interval_s < 30:
        raise ValueError("tune.interval_s must be >= 30 seconds")
    if out.window_s < 60:
        raise ValueError("tune.window_s must be >= 60 seconds")
    if out.min_samples < 1:
        raise ValueError("tune.min_samples must be >= 1")
    for k in ("priority_spread", "weight_spread"):
        if not 1 <= getattr(out, k) <= 100:
            raise ValueError("tune.%s must be 1..100" % k)
    return out


DSM_STYLES = ("openai-chat", "openai-responses", "anthropic")
UPSTREAM_STYLES = ("openai", "openai-responses", "anthropic", "opencode")
DSM_BUDGET_TIERS = ("low", "mid", "high")


@dataclass
class DSMConfig:
    """`dsm:` block - serve the DSM v1 envelope alongside legacy OpenAI (see dsm.py).

    Additive layer, never a replacement: with `enabled: false` (default) no /v1/dsm/*
    route exists and /v1/chat/completions behaves byte-for-byte like today.
    `openai_compat: false` closes the legacy endpoints with a loud 410 instead of
    silently downgrading - and `enabled: false` + `openai_compat: false` is refused
    at load, because that combination would leave the gateway with no usable
    endpoint at all (self-brick guard, contract §2 truth table FF).
    """

    enabled: bool = False
    openai_compat: bool = True
    schema_store: str = "runtime/dsm_schemas.json"
    max_materialize_bytes: int = 2_000_000
    budget_map: dict = field(default_factory=lambda: {"low": 1024, "mid": 4096, "high": 16384})
    require_registered_schema: bool = False
    default_max_tokens: int = 0    # >0 时：DSM 请求未自带 max_tokens 即用此值兜底（防提供商低默认截断复杂任务）

    def serves_dsm(self) -> bool:
        return bool(self.enabled)

    def serves_legacy(self) -> bool:
        return bool(self.openai_compat)

    def abs_path(self, base: str | os.PathLike, name: str = "dsm_schemas.json") -> Path:
        """Resolve a runtime store path relative to the data dir (db_path's dir).

        `schema_store` may be a file or a directory; the session store always sits
        next to the schema store so one config line controls both.
        """
        p = Path(self.schema_store)
        if not p.is_absolute():
            p = Path(base) / p
        if p.suffix.lower() != ".json":
            p = p / name
        return p

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "openai_compat": self.openai_compat,
                "schema_store": self.schema_store,
                "max_materialize_bytes": self.max_materialize_bytes,
                "budget_map": dict(self.budget_map),
                "require_registered_schema": self.require_registered_schema,
                "default_max_tokens": self.default_max_tokens}


def dsm_from_raw(raw, default: "DSMConfig | None" = None) -> DSMConfig:
    """Parse + validate the `dsm:` block; a missing block keeps the safe default."""
    base = default or DSMConfig()
    if not isinstance(raw, dict):
        return base
    enabled = bool(raw.get("enabled", base.enabled))
    compat = bool(raw.get("openai_compat", base.openai_compat))
    if not enabled and not compat:
        raise ValueError(
            "dsm.enabled=false 且 dsm.openai_compat=false：信封与 legacy 两条路同时关闭，"
            "网关将没有任何可用端点（拒绝加载，绝不自毁）。保留 openai_compat=true 或改 enabled=true。")
    store = str(raw.get("schema_store", base.schema_store) or base.schema_store)
    if ".." in Path(store).parts:
        raise ValueError("dsm.schema_store 不得包含 ..（路径穿越）")
    mb = int(raw.get("max_materialize_bytes", base.max_materialize_bytes) or 0)
    if mb < 1024:
        raise ValueError("dsm.max_materialize_bytes 必须 >= 1024")
    bm = raw.get("budget_map", base.budget_map) or {}
    if not isinstance(bm, dict):
        raise ValueError("dsm.budget_map 必须是 {low|mid|high: tokens} 映射")
    bad = [k for k in bm if str(k).strip().lower() not in DSM_BUDGET_TIERS]
    if bad:
        raise ValueError(f"dsm.budget_map 键必须是 {DSM_BUDGET_TIERS}（got {bad}）")
    tiers = {}
    for k, v in bm.items():
        try:
            tiers[str(k).strip().lower()] = max(0, int(v))
        except (TypeError, ValueError):
            raise ValueError(f"dsm.budget_map.{k} 必须是整数 token 数")
    out = DSMConfig(
        enabled=enabled, openai_compat=compat, schema_store=store,
        max_materialize_bytes=mb,
        budget_map=tiers or dict(base.budget_map),
        require_registered_schema=bool(raw.get("require_registered_schema",
                                               base.require_registered_schema)),
        default_max_tokens=max(0, int(raw.get("default_max_tokens", base.default_max_tokens) or 0)))
    return out


@dataclass
class EgcConfig:
    """`egc:` block - EGC v1 (Egress Compact), the outbound standard layer (see egc.py).

    位置是硬约束：DSM 信封物化之后、序列化之前——出网的最后一道。默认档只开「实测
    零语义损失」的开关（R1/R2/R3c/R4/R5/R6/R7/R8/R9/R10）；有语义代价的（删
    description 等）一律关，由调用方按 lane 显式打开——省 token 不能靠悄悄改变
    模型看到的东西。

    `budget_ms` 是这一层的验收门槛：转换属于出网标准的一部分，不能变成延迟税。
    `enabled: false` 一行即回到旧行为（出网字节与今天同形），灰度回滚零成本。
    """

    enabled: bool = True
    ff: dict = field(default_factory=dict)          # 开关覆盖：{R3a_drop_fn_desc: true}
    lane_tools: dict = field(default_factory=dict)  # R4 白名单：{lane: [工具名]}，"*" 兜底
    budget_ms: float = 5.0                          # 毫秒门（转换 p95）
    validate: bool = True                           # 出网前机检（不过则退回未精简 body）

    def as_config(self) -> dict:
        return {"enabled": self.enabled, "ff": dict(self.ff),
                "lane_tools": {str(k): [str(x) for x in (v or [])]
                               for k, v in (self.lane_tools or {}).items()},
                "budget_ms": self.budget_ms, "validate": self.validate}


def egc_from_raw(raw, default: "EgcConfig | None" = None) -> EgcConfig:
    """Parse + validate the `egc:` block; a missing block keeps the default profile."""
    from . import egc as _egc            # stdlib-only module, no import cycle
    base = default or EgcConfig()
    if not isinstance(raw, dict):
        return base
    ff = raw.get("ff", {}) or {}
    if not isinstance(ff, dict):
        raise ValueError("egc.ff 必须是 {开关名: true|false} 映射")
    unknown = sorted(str(k) for k in ff if str(k) not in _egc.FF)
    if unknown:
        raise ValueError("egc.ff 含未知开关 %s（可用：%s）"
                         % (",".join(unknown), ",".join(sorted(_egc.FF))))
    lt = raw.get("lane_tools", {}) or {}
    if not isinstance(lt, dict):
        raise ValueError("egc.lane_tools 必须是 {lane: [工具名]} 映射")
    for k, v in lt.items():
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"egc.lane_tools.{k} 必须是工具名列表")
    try:
        bm = float(raw.get("budget_ms", base.budget_ms))
    except (TypeError, ValueError):
        raise ValueError("egc.budget_ms 必须是毫秒数")
    if not (0.0 < bm <= 1000.0):
        raise ValueError("egc.budget_ms 必须在 (0, 1000] 毫秒之间")
    return EgcConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        ff={str(k): bool(v) for k, v in ff.items()},
        lane_tools={str(k): [str(x) for x in v] for k, v in lt.items()},
        budget_ms=bm,
        validate=bool(raw.get("validate", base.validate)))


@dataclass
class ExtensionSpec:
    """One entry of the `extensions:` list - a bundle imported into 总设置 → 扩展程序.

    `path` is the bundle directory on disk (it must contain an `asset/` folder);
    `panel` is relative to that asset folder. `verified` is only a remembered
    hint: the live check re-reads `asset/SMSocket.identity` on every console
    read (see extensions.py), so a stale true here can never mount a panel.
    """

    name: str = ""
    title: str = ""
    path: str = ""
    panel: str = "panel.html"
    version: str = ""
    verified: bool = False
    added_at: float = 0.0

    def as_config(self) -> dict:
        return {"name": self.name, "title": self.title, "path": self.path,
                "panel": self.panel, "version": self.version,
                "verified": bool(self.verified), "added_at": float(self.added_at)}


def extensions_from_raw(raw, default: list | None = None) -> list["ExtensionSpec"]:
    """Parse + validate the `extensions:` list; junk entries are dropped, not fatal."""
    if raw in (None, ""):
        return list(default or [])
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("extensions must be a list of objects")
    out: list[ExtensionSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(_env(item.get("path", "")) or "").strip()
        if not path:
            continue
        name = str(item.get("name") or "").strip() or path.replace("\\", "/").rstrip("/").split("/")[-1]
        if not name or name in seen:
            continue
        seen.add(name)
        panel = str(item.get("panel") or "panel.html").strip() or "panel.html"
        out.append(ExtensionSpec(
            name=name, title=str(item.get("title") or name).strip(), path=path,
            panel=panel, version=str(item.get("version") or ""),
            verified=bool(item.get("verified", False)),
            added_at=float(item.get("added_at") or 0.0)))
    return out


@dataclass
class Settings:
    listen: str = "127.0.0.1:8000"
    master_keys: list[str] = field(default_factory=list)
    strategy: str = "priority"     # priority | round_robin | weighted
    cooldown: float = 60.0         # seconds a dead key/provider is skipped
    db_path: str = "usage.sqlite3"
    log_level: str = "INFO"
    max_concurrency: int = 0       # 0 = unlimited in-flight upstream calls
    queue_wait: float = 30.0       # seconds a caller may wait for a free slot
    per_provider_concurrency: int = 0   # 0 = no per-provider cap
    currency: str = "USD"          # display currency (mirrors billing.currency)
    billing: Billing = field(default_factory=Billing)
    clash: ClashConfig = field(default_factory=ClashConfig)
    stack: StackConfig = field(default_factory=StackConfig)
    assess: AssessConfig = field(default_factory=AssessConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    discover: DiscoverConfig = field(default_factory=DiscoverConfig)
    tune: TuneConfig = field(default_factory=TuneConfig)
    dsm: DSMConfig = field(default_factory=DSMConfig)
    egc: EgcConfig = field(default_factory=EgcConfig)
    extensions: list[ExtensionSpec] = field(default_factory=list)
    pricing: dict[str, dict] = field(default_factory=dict)   # alias -> {prompt, completion} per 1M tokens
    providers: list[ProviderSpec] = field(default_factory=list)

    def embed_index(self) -> dict[str, list[ProviderSpec]]:
        idx: dict[str, list[ProviderSpec]] = {}
        for p in self.providers:
            if p.enabled and p.style == "openai":
                for alias in p.embeddings:
                    idx.setdefault(alias, []).append(p)
        return idx
    def pricing_row(self, alias: str) -> dict:
        """Per-1M-token price row for an alias ('*' = default row)."""
        return self.pricing.get(alias) or self.pricing.get("*") or {}

    def cost_of(self, alias: str, prompt: int, completion: int, cached: int = 0) -> float:
        """Estimated cost in the pricing row's own currency."""
        return float(self.cost_detail(alias, prompt, completion, cached)["amount"])

    def cost_detail(self, alias: str, prompt: int, completion: int,
                    cached: int = 0, cache_write: int = 0) -> dict:
        """Cost of a call: amount in pricing currency + converted display amount.

        {"amount", "currency", "display", "display_currency", "rate"} - `display`
        is always in settings.billing.currency, so mixed-currency providers
        add up correctly.
        """
        return self.billing.cost(prompt, completion, self.pricing_row(alias),
                                 cached=cached, cache_write=cache_write)
    def model_index(self) -> dict[str, list[ProviderSpec]]:
        idx: dict[str, list[ProviderSpec]] = {}
        for p in self.providers:
            if not p.enabled:
                continue
            for alias in p.aliases():
                idx.setdefault(alias, []).append(p)
        return idx

_TOP = {"listen", "master_keys", "strategy", "cooldown", "db_path", "log_level"}


def load_config(path: str | os.PathLike | None = None) -> Settings:
    """Load settings from YAML/JSON file (default: ./config.yaml, env SMSSOCKET_CONFIG;
    the pre-rename LLMROUTER_CONFIG is still honoured)."""
    path = Path(path or envv("SMSSOCKET_CONFIG") or "config.yaml")
    raw: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix == ".json" else (
            yaml.safe_load(text) if yaml else json.loads(text))
    provs = []
    for item in raw.get("providers", []):
        keys = item.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        provs.append(ProviderSpec(
            name=item["name"], base_url=_env(item["base_url"]).rstrip("/"),
            keys=[k for k in _env(keys) if k],
            models=dict(_env(item.get("models") or {})),
            style=item.get("style", "openai"), weight=int(item.get("weight", 1)),
            priority=int(item.get("priority", 0)), timeout=float(item.get("timeout", 120.0)),
            max_rpm=int(item.get("max_rpm", 0)), enabled=bool(item.get("enabled", True)),
            keyless=bool(item.get("keyless", False)),
            extra_headers=dict(_env(item.get("extra_headers") or {})),
            embeddings=dict(_env(item.get("embeddings") or {})),
            auto=bool(item.get("auto", True))))
    for p in provs:
        if p.style not in UPSTREAM_STYLES:
            raise ValueError(
                f"provider '{p.name}': style must be one of {UPSTREAM_STYLES}")
    settings = Settings(
        listen=raw.get("listen", Settings.listen),
        master_keys=[k for k in _env(raw.get("master_keys") or []) if k],
        strategy=raw.get("strategy", Settings.strategy),
        cooldown=float(raw.get("cooldown", Settings.cooldown)),
        db_path=raw.get("db_path", Settings.db_path),
        log_level=raw.get("log_level", Settings.log_level),
        max_concurrency=int(raw.get("max_concurrency", 0) or 0),
        queue_wait=float(raw.get("queue_wait", 30.0) or 0),
        per_provider_concurrency=int(raw.get("per_provider_concurrency", 0) or 0),
        currency=raw.get("currency", Settings.currency),
        billing=billing_from_raw(raw.get("billing"), raw.get("currency", "USD")),
        clash=clash_from_raw(raw.get("clash")),
        stack=stack_from_raw(raw.get("stack")),
        assess=assess_from_raw(raw.get("assess")),
        ui=ui_from_raw(raw.get("ui")),
        discover=discover_from_raw(raw.get("discover")),
        tune=tune_from_raw(raw.get("tune")),
        dsm=dsm_from_raw(raw.get("dsm")),
        egc=egc_from_raw(raw.get("egc")),
        extensions=extensions_from_raw(raw.get("extensions")),
        pricing={k: dict(v or {}) for k, v in (raw.get("pricing") or {}).items()},
        providers=provs)
    from .keys import ensure_master_key
    settings.key_source = ensure_master_key(settings, path)
    return settings
