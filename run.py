"""Entry point: python run.py [--config config.yaml] [--host --port --reload]
                       [--no-key | --rotate-key]

Startup prints a banner in the console: web dashboard URL, OpenAI-compatible
base URL, and the client key in effect (randomly generated on first run and
reused afterwards, stored in smsocket.key next to the config).
"""
from __future__ import annotations
import argparse
import os


def _load_env_file():
    """Fallback key source: smsocket.env beside the config (same dir), KEY=VALUE lines.
    Process env always wins (setdefault); this keeps a bare `python run.py` launch
    from silently starting with a 0-key pool when the upstream key vars weren't
    exported into the launching shell. Also sets SMSSOCKET_ENV_FILE so config._env()
    picks up the same file as a ${VAR} fallback on any later re-expansion (reload)."""
    cfg = os.environ.get("SMSSOCKET_CONFIG") or "config.yaml"
    env_file = os.path.join(os.path.dirname(cfg) or ".", "smsocket.env")
    if not os.path.exists(env_file):
        return
    os.environ.setdefault("SMSSOCKET_ENV_FILE", env_file)
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main() -> None:
    ap = argparse.ArgumentParser(description="SMSocket gateway")
    ap.add_argument("--config", default=None, help="config file (yaml/json)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-key", action="store_true",
                    help="start without client key auth (open gateway)")
    ap.add_argument("--rotate-key", action="store_true",
                    help="mint a fresh random key (overwrites smsocket.key)")
    a = ap.parse_args()
    if a.config:
        os.environ["SMSSOCKET_CONFIG"] = a.config
    for new, old in (("SMSSOCKET_CONFIG", "LLMROUTER_CONFIG"),
                  ("SMSSOCKET_NO_KEY", "LLMROUTER_NO_KEY"),
                  ("SMSSOCKET_MASTER_KEY", "LLMROUTER_MASTER_KEY"),
                  ("SMSSOCKET_KEY_FILE", "LLMROUTER_KEY_FILE")):
        if old in os.environ and new not in os.environ:
            os.environ[new] = os.environ[old]
    if a.no_key:
        os.environ["SMSSOCKET_NO_KEY"] = "1"
    _load_env_file()
    cfg = os.environ.get("SMSSOCKET_CONFIG", os.environ.get("LLMROUTER_CONFIG", "config.yaml"))
    if a.rotate_key:
        from SMSocket.keys import key_path
        try:
            key_path(cfg).unlink()
        except OSError:
            pass
    from SMSocket.config import load_config
    st = load_config(cfg)
    host, _, port = st.listen.rpartition(":")
    host = a.host or host or "127.0.0.1"
    port = a.port or int(port or 8000)
    if not st.providers:
        print("no providers configured - copy config.example.yaml to config.yaml")
    banner(st, host, port)
    live = [p for p in st.providers if p.enabled]
    dead = [p.name for p in live if not p.keys and not getattr(p, "keyless", False)]
    if live and all(not p.keys for p in live):
        # Surface the actual placeholders the operator forgot to inject, instead
        # of hard-coding a few provider names that drift out of sync with config.
        import re
        names = []
        try:
            names = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                               open(cfg, encoding="utf-8").read())
        except OSError:
            pass
        missing = sorted({n for n in names if not os.environ.get(n)})
        hint = ", ".join(missing) if missing else "(none in this config)"
        print("[warn] no usable upstream keys in the running process - ${VAR} placeholders in "
              "config.yaml expanded to empty (missing env vars: %s). "
              "Every /v1/ chat request will answer 503 until keys are added via "
              "POST /admin/providers/<name>/keys and the config is reloaded; restart smsocket "
              "with the upstream key environment variables present instead." % hint, flush=True)
    elif dead:
        print("[warn] %d of %d enabled provider(s) have no usable keys: %s - "
              "requests for their aliases will 503" % (len(dead), len(live), ", ".join(dead)),
              flush=True)
    import uvicorn
    uvicorn.run("SMSocket.gateway:create_app", host=host, port=port,
                factory=True, reload=a.reload)


def banner(st, host: str, port: int) -> None:
    """Console banner: web address + key (the user asked for both on startup)."""
    shown = "http://127.0.0.1:%d" % port if host in ("0.0.0.0", "", "*") \
        else "http://%s:%d" % (host, port)
    key = st.master_keys[0] if st.master_keys else None
    label = {"generated": "随机生成（已存 smsocket.key，重启复用）",
             "reused": "复用 smsocket.key",
             "env": "来自环境变量",
             "config": "来自配置文件 master_keys",
              "disabled": "未启用（--no-key，任何人可调用）"}.get(st.key_source, st.key_source)
    models = len(st.model_index())
    print("=" * 56)
    print("  SMSocket 模型路由网关")
    print("  网页端   %s/" % shown)
    print("  API 地址 %s/v1" % shown)
    print("  密钥     %s" % (key or "-"))
    print("           %s" % label)
    print("  提供商   %d 个 · 上游密钥 %d 把 · 模型 %d 个"
          % (len(st.providers), sum(len(p.keys) for p in st.providers), models))
    print("=" * 56, flush=True)


if __name__ == "__main__":
    main()
