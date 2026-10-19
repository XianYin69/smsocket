"""Install identity: the unique code that pairs an extension bundle with this SMSocket.

Every SMSocket install mints one code once and keeps it beside the config file
(`smsocket.identity`) - same lifecycle as the master key (see keys.py): reused
across restarts and `run.py --reload`, regenerated only with rotate=True.

An extension bundle (SMSC is the first) stores that code inside its own `asset/`
folder as `asset/SMSocket.identity`. That file is the pairing proof: SMSocket
mounts a bundle's own panel under 总设置 → 扩展程序 only while the code in
`asset/SMSocket.identity` equals this install's code. A bundle copied to another
machine therefore verifies false and its panel is never rendered.

The code is a pairing token, not a display string: it is masked in every API
response and log line, and only the pairing write (server-side, into the bundle's
asset folder) ever handles it in full.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from pathlib import Path

from .config import envv

log = logging.getLogger("smssocket.identity")

ID_FILE = "smsocket.identity"
ENV_ID_FILE = "SMSSOCKET_IDENTITY_FILE"
ASSET_DIR = "asset"
ASSET_ID_FILE = "SMSocket.identity"
PREFIX = "SMSOCKET"


def new_code() -> str:
    """SMSOCKET-XXXX-XXXX-XXXX-XXXX (20 hex chars from a fresh uuid4 + entropy)."""
    seed = uuid.uuid4().hex + uuid.uuid4().hex + str(os.getpid()) + str(time.time())
    body = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20].upper()
    return PREFIX + "-" + "-".join(body[i:i + 4] for i in range(0, 20, 4))


def identity_path(config_path: os.PathLike | str | None = None) -> Path:
    """Where the install code lives: env override, else beside the config file."""
    env = envv(ENV_ID_FILE)
    if env:
        return Path(env)
    base = Path(config_path) if config_path else Path.cwd()
    if base.is_file():
        base = base.parent
    return base / ID_FILE


def install_id(config_path: os.PathLike | str | None = None,
               create: bool = True, rotate: bool = False) -> str:
    """This machine's SMSocket code (minted on first use, then stable)."""
    path = identity_path(config_path)
    if not rotate:
        try:
            old = path.read_text(encoding="utf-8").strip()
        except OSError:
            old = ""
        if old:
            return old
    if not create:
        return ""
    code = new_code()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("install identity not persisted (%s): the code only lives in this process - "
                    "extension pairing silently breaks on restart; fix the path's writability", exc)
    return code


def mask(code: str) -> str:
    code = str(code or "")
    if not code:
        return ""
    if len(code) <= 12:
        return code[:4] + "…"
    return code[:12] + "…" + code[-4:]


def asset_dir(bundle: os.PathLike | str) -> Path:
    return Path(bundle) / ASSET_DIR


def asset_id_path(bundle: os.PathLike | str) -> Path:
    return asset_dir(bundle) / ASSET_ID_FILE


def read_asset_id(bundle: os.PathLike | str) -> str:
    try:
        return asset_id_path(bundle).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_asset_id(bundle: os.PathLike | str,
                   config_path: os.PathLike | str | None = None) -> dict:
    """Pairing: put this install's code into the bundle's asset folder."""
    target = Path(bundle)
    if not target.is_dir():
        return {"ok": False, "reason": "扩展程序目录不存在：%s" % target, "file": str(target)}
    code = install_id(config_path)
    path = asset_id_path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": "识别码写入失败：%s" % exc, "file": str(path)}
    return {"ok": True, "file": str(path), "code": mask(code)}


def verify(bundle: os.PathLike | str,
           config_path: os.PathLike | str | None = None) -> dict:
    """asset/SMSocket.identity must equal this install's code - nothing else counts."""
    local = install_id(config_path)
    got = read_asset_id(bundle)
    matched = bool(got) and got == local
    if not got:
        reason = "asset 文件夹内没有识别码文件（未配对）"
    elif not matched:
        reason = "识别码与本机 SMSocket 不一致（他机拷贝或未配对）"
    else:
        reason = ""
    return {"verified": matched, "present": bool(got), "matched": matched,
            "reason": reason, "file": str(asset_id_path(bundle)), "code": mask(local)}
