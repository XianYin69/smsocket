"""Master key resolution: config > env > auto-generated random key (persisted).

The gateway must always answer "what key do I use?" in the startup banner, so a
missing master_keys section is not an error - we mint a random key once and keep
it in a file next to the config, reused on every restart / --reload subprocess.
Set SMSSOCKET_NO_KEY=1 (run.py --no-key) to opt out and run unauthenticated.
"""
from __future__ import annotations
import logging
import os
import secrets
from pathlib import Path

from .config import envv

log = logging.getLogger("smssocket.keys")

KEY_FILE = "smsocket.key"
LEGACY_KEY_FILE = "router.key"
PREFIX = "sk-socket-"
ENV_NO_KEY = "SMSSOCKET_NO_KEY"
ENV_KEY = "SMSSOCKET_MASTER_KEY"
ENV_KEY_FILE = "SMSSOCKET_KEY_FILE"


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def key_path(config_path: os.PathLike | str | None = None) -> Path:
    """Where the generated key lives: env override, else beside the config file."""
    env = envv(ENV_KEY_FILE)
    if env:
        return Path(env)
    base = Path(config_path) if config_path else Path.cwd()
    if base.is_file():
        base = base.parent
    new = base / KEY_FILE
    if not new.exists() and (base / LEGACY_KEY_FILE).exists():
        return base / LEGACY_KEY_FILE   # pre-rename key file keeps working
    return new


def ensure_master_key(settings, config_path=None, rotate: bool = False) -> str:
    """Fill settings.master_keys if empty; return source label.

    'config'      - master_keys came from config.yaml (unchanged)
    'env'         - taken from SMSSOCKET_MASTER_KEY
    'generated'   - random key minted now and written to smsocket.key
    'reused'      - read back from smsocket.key (restart / reload keeps one key)
    'disabled'    - no key, gateway open (SMSSOCKET_NO_KEY / --no-key)
    """
    if getattr(settings, "master_keys", None):
        return "config"
    if envv(ENV_NO_KEY):
        return "disabled"
    env_key = envv(ENV_KEY)
    if env_key:
        settings.master_keys = [env_key]
        return "env"
    path = key_path(config_path)
    if not rotate:
        try:
            old = path.read_text(encoding="utf-8").strip()
        except OSError:
            old = ""
        if old:
            settings.master_keys = [old]
            return "reused"
    key = new_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("master key not persisted (%s): the key only lives in this process "
                    "- every restart mints a different one; fix the path's writability", exc)
    settings.master_keys = [key]
    return "generated"
