"""Extension registry: import a bundle, verify the identity code, mount its panel.

Wire contract (dashboard 总设置 → 扩展程序, all master-key / console-cookie gated):

  GET    /admin/extensions                 this install's code (masked) + every bundle
  POST   /admin/extensions/import          {"path": "..."}  verify + register
  POST   /admin/extensions/pair            {"path": "..."}  write our code into <path>/asset, then import
  POST   /admin/extensions/remove          {"name": "..."}  unregister
  GET    /admin/extensions/{name}/panel    the bundle's own panel HTML (verified bundles only)

A bundle is a directory holding an `asset/` folder:
  asset/SMSocket.identity   this install's unique code - the pairing proof
  asset/extension.json      optional manifest {name, title, version, panel}
  asset/panel.html          the panel shipped with the bundle (default entry)

Verification is re-run on every read, not cached: the code file is compared with
the live install identity each time, so copying a registered bundle to another
machine - or deleting its code - drops it back to 未验证 and its panel stops
being served (403). An unverified bundle can never paint UI inside the console.

Paths are confined to the bundle directory: the manifest may point at any file
inside it, never outside (resolved path must stay under `asset/`'s parent).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import identity
from .config import ExtensionSpec

MANIFEST = "extension.json"
DEFAULT_PANEL = "panel.html"
MAX_PANEL_BYTES = 512 * 1024


def _err(msg: str, status: int = 400) -> HTTPException:
    return HTTPException(status, detail={"error": {"message": msg,
                                                   "type": "invalid_request_error"}})


def manifest(bundle: Path) -> dict:
    """asset/extension.json (absent or broken = empty manifest, never fatal)."""
    path = bundle / identity.ASSET_DIR / MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _panel_rel(bundle: Path, spec: ExtensionSpec, man: dict) -> str:
    """Panel location inside the bundle: manifest wins over the stored spec.

    `..` segments are refused outright (400) rather than silently rewritten, so a
    manifest can never point the console at a file outside the bundle directory.
    """
    raw = str(man.get("panel") or spec.panel or DEFAULT_PANEL).strip().replace("\\", "/")
    if ".." in raw.split("/"):
        raise _err("扩展程序面板路径不得越出 bundle 目录：%s" % raw, 400)
    raw = raw.lstrip("./")
    if raw.startswith(identity.ASSET_DIR + "/"):
        raw = raw[len(identity.ASSET_DIR) + 1:]
    return raw or DEFAULT_PANEL


def panel_path(bundle: Path, spec: ExtensionSpec, man: dict | None = None) -> Path:
    """Resolve the panel file, refusing anything outside the bundle directory."""
    man = man if man is not None else manifest(bundle)
    root = bundle.resolve()
    rel = Path(_panel_rel(bundle, spec, man))
    cand = (root / identity.ASSET_DIR / rel).resolve()
    if not cand.is_file():
        cand = (root / rel).resolve()
    if cand != root and root not in cand.parents:
        raise _err("扩展程序面板路径越出 bundle 目录：%s" % rel, 400)
    return cand


def status_of(spec: ExtensionSpec, config_path=None) -> dict:
    """Live view of one bundle: identity check + manifest, for the console list."""
    bundle = Path(spec.path)
    man = manifest(bundle)
    ver = identity.verify(bundle, config_path)
    name = str(man.get("name") or spec.name or bundle.name).strip() or bundle.name
    title = str(man.get("title") or spec.title or name).strip()
    version = str(man.get("version") or spec.version or "").strip()
    panel_ok = False
    panel_file = ""
    if ver["verified"]:
        try:
            p = panel_path(bundle, spec, man)
            panel_ok = p.is_file() and p.stat().st_size <= MAX_PANEL_BYTES
            panel_file = str(p)
        except HTTPException:
            panel_ok = False
    return {"name": name, "title": title, "path": str(bundle),
            "exists": bundle.is_dir(), "version": version,
            "description": str(man.get("description") or ""),
            "verified": bool(ver["verified"]), "reason": ver["reason"],
            "identity_file": ver["file"], "code": ver["code"],
            "panel": panel_file, "panel_ready": bool(panel_ok),
            "added_at": float(spec.added_at or 0)}


def list_public(settings, config_path=None) -> dict:
    """GET /admin/extensions payload: masked code + every registered bundle."""
    items = [status_of(s, config_path) for s in (settings.extensions or [])]
    items.sort(key=lambda x: (not x["verified"], x["name"].lower()))
    return {"identity": identity.mask(identity.install_id(config_path)),
            "identity_file": str(identity.identity_path(config_path)),
            "asset_name": identity.ASSET_ID_FILE, "items": items,
            "count": len(items), "mounted": sum(1 for i in items if i["panel_ready"])}


def find_spec(settings, name: str) -> ExtensionSpec:
    for spec in (settings.extensions or []):
        if spec.name == name:
            return spec
    raise _err("unknown extension: %s" % name, 404)


def import_bundle(raw_path: str, config_path=None) -> ExtensionSpec:
    """Verify a bundle directory and return the spec to register (raises on junk)."""
    if not str(raw_path or "").strip():
        raise _err("扩展程序路径不能为空")
    bundle = Path(str(raw_path).strip()).expanduser()
    if not bundle.is_absolute():
        # relative paths resolve against the CWD (what an operator sees in the
        # shell they launched the gateway from), not the config file's folder
        bundle = (Path.cwd() / bundle).resolve()
    if not bundle.is_dir():
        raise _err("扩展程序目录不存在：%s" % bundle, 404)
    man = manifest(bundle)
    ver = identity.verify(bundle, config_path)
    if not ver["verified"]:
        raise _err("识别码验证未通过：%s（可点「配对并导入」写入本机识别码）"
                   % ver["reason"], 403)
    try:
        panel_path(bundle, ExtensionSpec(panel=str(man.get("panel") or DEFAULT_PANEL)), man)
    except HTTPException:
        raise _err("扩展程序自带面板缺失或越界：%s/asset/%s"
                   % (bundle, man.get("panel") or DEFAULT_PANEL), 400)
    name = str(man.get("name") or bundle.name).strip() or bundle.name
    return ExtensionSpec(
        name=name, title=str(man.get("title") or name).strip(),
        path=str(bundle), panel=str(man.get("panel") or DEFAULT_PANEL),
        version=str(man.get("version") or ""), verified=True, added_at=time.time())


def persist(path: Path, specs: list[ExtensionSpec]) -> None:
    """Write the `extensions:` list into the config file (validate-then-swap)."""
    from .admin import read_raw, write_raw        # late import: admin owns the swap
    data = read_raw(path)
    data["extensions"] = [s.as_config() for s in specs]
    write_raw(path, data)


def merged(existing: list[ExtensionSpec], item: ExtensionSpec) -> list[ExtensionSpec]:
    out = [s for s in existing if s.name != item.name]
    out.append(item)
    return out


def build(st, dep) -> APIRouter:
    """Extension routes for the console; `dep` = master-key dependency list."""
    r = APIRouter(dependencies=dep)

    def cfg():
        from .admin import cfg_path
        return cfg_path()

    def specs() -> list[ExtensionSpec]:
        return list(st.settings.extensions or [])

    @r.get("/admin/extensions")
    async def get_extensions():
        return list_public(st.settings, cfg())

    @r.post("/admin/extensions/import")
    async def post_import(request: Request):
        body = await _json(request)
        item = import_bundle(str(body.get("path") or ""), cfg())
        persist(cfg(), merged(specs(), item))
        from .admin import apply_state
        applied = apply_state(st)
        return {"ok": True, "item": status_of(item, cfg()),
                "applied": applied, **list_public(st.settings, cfg())}

    @r.post("/admin/extensions/pair")
    async def post_pair(request: Request):
        body = await _json(request)
        raw = str(body.get("path") or "").strip()
        wrote = identity.write_asset_id(raw or ".", cfg())
        if not wrote["ok"]:
            raise _err(wrote["reason"], 400)
        item = import_bundle(raw, cfg())
        persist(cfg(), merged(specs(), item))
        from .admin import apply_state
        applied = apply_state(st)
        return {"ok": True, "paired": wrote, "item": status_of(item, cfg()),
                "applied": applied, **list_public(st.settings, cfg())}

    @r.post("/admin/extensions/remove")
    async def post_remove(request: Request):
        body = await _json(request)
        name = str(body.get("name") or "").strip()
        cur = specs()
        find_spec(st.settings, name)
        persist(cfg(), [s for s in cur if s.name != name])
        from .admin import apply_state
        applied = apply_state(st)
        return {"ok": True, "removed": name, "applied": applied,
                **list_public(st.settings, cfg())}

    @r.get("/admin/extensions/{name}/panel")
    async def get_panel(name: str):
        spec = find_spec(st.settings, name)
        view = status_of(spec, cfg())
        if not view["verified"]:
            raise _err("识别码验证未通过，面板不挂载：%s" % view["reason"], 403)
        if not view["panel_ready"]:
            raise _err("扩展程序自带面板缺失或过大：%s" % view["panel"], 404)
        html = Path(view["panel"]).read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(html)

    return r


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _err("body must be a json object")
    if not isinstance(body, dict):
        raise _err("body must be a json object")
    return body
