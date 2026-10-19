"""Admin API tests: config CRUD, custom provider, key pool, safety rails."""
import json
import pytest
import yaml
from fastapi.testclient import TestClient

from SMSocket.config import load_config
from SMSocket.gateway import create_app

KEY = "sk-admin-test"
CFG = """listen: 127.0.0.1:8011
db_path: "{db}"
strategy: priority
currency: USD
providers:
  - name: Alpha
    base_url: https://a.example/v1
    keys:
      - sk-alpha-secret-1
    models:
      alpha-large: alpha-large-v1
    priority: 10
"""


def H():
    return {"authorization": "Bearer " + KEY}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    db = str(tmp_path / "usage.sqlite3").replace("\\", "/")
    cfg.write_text(CFG.replace(chr(123)+chr(100)+chr(98)+chr(125), db), encoding="utf-8")
    monkeypatch.setenv("SMSSOCKET_CONFIG", str(cfg))
    monkeypatch.setenv("SMSSOCKET_MASTER_KEY", KEY)
    monkeypatch.delenv("SMSSOCKET_NO_KEY", raising=False)
    with TestClient(create_app(load_config(cfg))) as c:
        yield c, cfg


def read_cfg(cfg):
    return yaml.safe_load(cfg.read_text(encoding="utf-8"))


def test_admin_requires_key(client):
    c, _ = client
    assert c.get("/admin/config").status_code == 401
    assert c.get("/admin/config", headers=H()).status_code == 200


def test_snapshot_masks_secrets(client):
    c, _ = client
    data = c.get("/admin/config", headers=H()).json()
    assert "sk-alpha-secret-1" not in json.dumps(data)
    assert data["providers"][0]["key_count"] == 1
    assert data["models"] == ["alpha-large"]
    assert data["listen"] == "127.0.0.1:8011"


def test_self_endpoint_gives_base_url_and_key(client):
    c, _ = client
    masked = c.get("/admin/self", headers=H()).json()
    full = c.get("/admin/self?reveal=1", headers=H()).json()
    assert masked["base_url"].endswith("/v1")
    assert KEY not in json.dumps(masked)
    assert full["key"] == KEY
    assert full["models"] == ["alpha-large"]


def test_add_custom_provider(client):
    c, cfg = client
    r = c.post("/admin/providers", headers=H(), json={
        "name": "Custom", "base_url": "https://my.llm.local:1234/v1/",
        "style": "openai", "keys": ["sk-custom-1"], "models": {"gpt-x": "gpt-x-2"}})
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["providers"] == 2
    provs = read_cfg(cfg)["providers"]
    assert [p["name"] for p in provs] == ["Alpha", "Custom"]
    assert provs[1]["base_url"] == "https://my.llm.local:1234/v1"
    assert provs[1]["keys"] == ["sk-custom-1"]
    aliases = [m["id"] for m in c.get("/v1/models", headers=H()).json()["data"]]
    assert "gpt-x" in aliases and "alpha-large" in aliases


def test_patch_keeps_secret_and_disables(client):
    c, cfg = client
    r = c.patch("/admin/providers/Alpha", headers=H(),
                json={"priority": 3, "enabled": False, "keys": ["guessed"]})
    assert r.status_code == 200, r.text
    alpha = read_cfg(cfg)["providers"][0]
    assert alpha["priority"] == 3 and alpha["enabled"] is False
    assert alpha["keys"] == ["sk-alpha-secret-1"]
    owners = [m["owned_by"] for m in c.get("/v1/models", headers=H()).json()["data"]]
    assert "Alpha" not in owners


def test_rejects_bad_input(client):
    c, cfg = client
    assert c.post("/admin/providers", headers=H(),
                  json={"name": "ok", "base_url": "ftp://x/v1"}).status_code == 400
    assert c.post("/admin/providers", headers=H(),
                  json={"name": "Alpha", "base_url": "https://x/v1"}).status_code == 409
    assert c.patch("/admin/providers/Alpha", headers=H(),
                   json={"style": "grpc"}).status_code == 400
    assert len(read_cfg(cfg)["providers"]) == 1


def test_key_pool_routes(client):
    c, cfg = client
    assert c.post("/admin/providers/Alpha/keys", headers=H(),
                  json={"key": "sk-alpha-secret-2"}).status_code == 200
    assert read_cfg(cfg)["providers"][0]["keys"] == ["sk-alpha-secret-1", "sk-alpha-secret-2"]
    assert c.post("/admin/providers/Alpha/keys", headers=H(),
                  json={"key": "sk-alpha-secret-2"}).status_code == 409
    assert c.delete("/admin/providers/Alpha/keys/0", headers=H()).status_code == 200
    assert read_cfg(cfg)["providers"][0]["keys"] == ["sk-alpha-secret-2"]
    assert c.delete("/admin/providers/Alpha/keys/0", headers=H()).status_code == 409


def test_model_add_update_delete_routes(client):
    c, cfg = client
    # add a new chat alias on an existing provider
    r = c.post("/admin/providers/Alpha/models", headers=H(),
               json={"alias": "alpha-fast", "upstream": "alpha-fast-v1"})
    assert r.status_code == 200, r.text
    assert read_cfg(cfg)["providers"][0]["models"]["alpha-fast"] == "alpha-fast-v1"
    assert "alpha-fast" in c.get("/v1/models", headers=H()).json()["data"][0]["id"] or \
           "alpha-fast" in [m["id"] for m in c.get("/v1/models", headers=H()).json()["data"]]
    # update the same alias (overwrite, not duplicate)
    c.post("/admin/providers/Alpha/models", headers=H(),
           json={"alias": "alpha-fast", "upstream": "alpha-fast-v2"})
    assert read_cfg(cfg)["providers"][0]["models"]["alpha-fast"] == "alpha-fast-v2"
    # add an embedding alias on a separate map
    c.post("/admin/providers/Alpha/models", headers=H(),
           json={"alias": "alpha-emb", "upstream": "alpha-embed-1", "embed": True})
    assert read_cfg(cfg)["providers"][0]["embeddings"]["alpha-emb"] == "alpha-embed-1"
    # delete the chat alias
    assert c.delete("/admin/providers/Alpha/models/alpha-fast", headers=H()).status_code == 200
    assert "alpha-fast" not in read_cfg(cfg)["providers"][0]["models"]
    # deleting an unknown alias 404s
    assert c.delete("/admin/providers/Alpha/models/ghost", headers=H()).status_code == 404
    # bad input rejected
    assert c.post("/admin/providers/Alpha/models", headers=H(),
                  json={"alias": "", "upstream": "x"}).status_code == 400


def test_delete_provider_guard(client):
    c, cfg = client
    assert c.delete("/admin/providers/Alpha", headers=H()).status_code == 409
    c.post("/admin/providers", headers=H(),
           json={"name": "Beta", "base_url": "https://b.example/v1", "keys": ["sk-b-1"]})
    assert c.delete("/admin/providers/Beta", headers=H()).status_code == 200
    assert [p["name"] for p in read_cfg(cfg)["providers"]] == ["Alpha"]


def test_put_globals_and_pricing(client):
    c, cfg = client
    r = c.put("/admin/config", headers=H(), json={
        "strategy": "round_robin", "currency": "CNY",
        "pricing": {"alpha-large": {"prompt": 1.0, "completion": 2.0}}})
    assert r.status_code == 200, r.text
    data = read_cfg(cfg)
    assert data["strategy"] == "round_robin"
    assert data["pricing"]["alpha-large"]["completion"] == 2.0
    assert data["providers"][0]["keys"] == ["sk-alpha-secret-1"]
    assert (cfg.parent / "config.yaml.bak").exists()
    assert c.get("/pool", headers=H()).json()["strategy"] == "round_robin"


def test_put_config_refuses_empty_providers(client):
    c, cfg = client
    assert c.put("/admin/config", headers=H(), json={"providers": []}).status_code == 409
    assert len(read_cfg(cfg)["providers"]) == 1
