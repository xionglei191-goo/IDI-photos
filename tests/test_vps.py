import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from idi.api import create_app
from idi.cli import restore
from idi.config import Settings
from idi.connectors.secret_store import MAGIC, SecretStore, VisionSecretStore, initialize_key, protect, read_key
from idi.domain import DomainError
from idi.platform_account import PlatformAccountStore
from idi.vps import network_config, prepare
from idi.xhey_camera import XheySettings


@pytest.fixture
def vault(tmp_path, monkeypatch):
    key = tmp_path / "key" / "master.key"
    data = tmp_path / "data"
    initialize_key(key, data)
    monkeypatch.setenv("IDI_SECRET_KEY_FILE", str(key))
    return data, key


def test_vault_random_nonce_restart_and_child_process(vault):
    data, key = vault
    value = b"private-credential-for-test"
    first, second = protect(value), protect(value)
    assert first != second and value not in first
    assert first.startswith(MAGIC) and protect(first, decrypt=True) == value
    path = data / "credential.bin"
    path.parent.mkdir()
    path.write_bytes(first)
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys;from pathlib import Path;from idi.connectors.secret_store import protect;"
         "assert protect(Path(sys.argv[1]).read_bytes(),decrypt=True)==b'private-credential-for-test'",
         str(path)], capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr
    initialize_key(key, data)
    assert protect(first, decrypt=True) == value


def test_vault_rejects_tamper_wrong_key_and_dpapi(vault):
    _, key = vault
    encrypted = protect(b"test-password")
    for damaged in (encrypted[:-1], encrypted[:-1] + bytes([encrypted[-1] ^ 1]), b"Windows-DPAPI"):
        with pytest.raises(DomainError):
            protect(damaged, decrypt=True)
    key.write_bytes(os.urandom(32))
    with pytest.raises(DomainError, match="主密钥不匹配"):
        protect(encrypted, decrypt=True)


def test_missing_or_invalid_key_never_replaces_credentials(vault):
    data, key = vault
    store = SecretStore(data)
    store.save("a" * 32, "test-only-app-secret")
    encrypted = store.path("a" * 32).read_bytes()
    key.unlink()
    with pytest.raises(DomainError, match="主密钥缺失"):
        initialize_key(key, data)
    assert not key.exists() and store.path("a" * 32).read_bytes() == encrypted
    for invalid in (b"", b"x" * 31, b"x" * 33):
        key.write_bytes(invalid)
        if os.name != "nt":
            key.chmod(0o600)
        with pytest.raises(DomainError, match="长度"):
            read_key(key)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_key_permissions_and_symlink(vault):
    _, key = vault
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.chmod(0o644)
    with pytest.raises(DomainError, match="权限"):
        read_key(key)
    key.chmod(0o600)
    alias = key.with_name("link.key")
    alias.symlink_to(key)
    with pytest.raises(DomainError):
        read_key(alias)
    fifo = key.with_name("pipe.key")
    os.mkfifo(fifo, 0o600)
    with pytest.raises(DomainError, match="普通文件"):
        read_key(fifo)


def test_all_credential_types_use_portable_store_and_backup_excludes_vault(vault):
    data, _ = vault
    feishu = SecretStore(data)
    feishu.save("a" * 32, "test-only-app-secret")
    vision = VisionSecretStore(data)
    vision.save("test-only-vision-key")
    account = PlatformAccountStore(data)
    value = dict(username="test-user", password="test-only-password", auto_connect=False, revision="test")
    account.save(value)
    camera = XheySettings(data)
    camera.save(dict(enabled=True, sdk_app_id="test-app", sdk_secret_key="test-only-sdk-secret"))
    assert feishu.load("a" * 32) == "test-only-app-secret"
    assert vision.load() == "test-only-vision-key"
    assert account.load() == value
    assert camera.load()["sdk_secret_key"] == "test-only-sdk-secret"
    app = create_app(Settings(data, worker_enabled=False))
    result = app.state.services["reporter"].backup({}, lambda *_: None)
    archive = data / "backups" / result["filename"]
    with zipfile.ZipFile(archive) as zipped:
        assert all("secret" not in name and ".key" not in name for name in zipped.namelist())
    target = data.parent / "restored"
    restore(archive, target)
    restored = create_app(Settings(target, worker_enabled=False))
    assert not (target / "secrets").exists()
    assert restored.state.services["db"].setting("automatic_upload_policy", {})["enabled"] is False


def test_vps_bootstrap_checks_existing_vault_and_persists_key(vault, monkeypatch):
    data, key = vault
    monkeypatch.setenv("IDI_DATA_DIR", str(data))
    monkeypatch.setenv("IDI_NETWORK_FILE", str(data.parent / "run" / "network.json"))
    monkeypatch.setenv("IDI_DOMAIN", "idi.example.test")
    monkeypatch.delenv("IDI_HOST", raising=False)
    original = key.read_bytes()
    prepare()
    settings = Settings.from_env()
    assert settings.public_origins == ("https://idi.example.test",)
    assert settings.host == "0.0.0.0"
    assert settings.deployment == "vps" and settings.lan_urls == []
    assert settings.local_admin_proxies == ("172.30.80.1",)
    SecretStore(data).save("a" * 32, "test-only-app-secret")
    prepare()
    assert key.read_bytes() == original
    key.write_bytes(os.urandom(32))
    with pytest.raises(DomainError, match="主密钥不匹配"):
        prepare()


@pytest.mark.parametrize("name,value", [
    ("IDI_DOMAIN", "http://idi.example.test"),
    ("IDI_DOMAIN", "idi.example.test/path"),
    ("IDI_VPS_SUBNET", "0.0.0.0/0"),
    ("IDI_VPS_APP_IP", "172.30.80.1"),
    ("IDI_VPS_PROXY_IP", "203.0.113.5"),
])
def test_vps_invalid_network_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        network_config()


def test_docker_gateway_camera_admin_requires_local_host_and_no_proxy_headers(tmp_path):
    network = network_config()
    app = create_app(Settings(tmp_path, worker_enabled=False, **network))
    with TestClient(app, base_url="http://127.0.0.1:18766", client=("172.30.80.1", 1234), headers={'X-IDI-Request': '1'}) as client:
        assert client.get('/api/auth/status').json() == {'setup_required': True, 'setup_allowed': True}
        assert client.get('/api/auth/status', headers={'X-Forwarded-Proto': 'https'}).json()['setup_allowed'] is False
        assert client.get('/api/auth/status', headers={'Host': '172.30.80.10:8766'}).json()['setup_allowed'] is False
        result = client.post('/api/auth/setup', json={'username': 'vps-admin', 'display_name': 'VPS测试管理员',
            'password': 'vps-test-password'})
        assert result.status_code == 200, result.text
        assert client.get("/api/system").json()["deployment"] == "vps"
        assert client.get("/api/system").json()["lan_urls"] == []
        assert client.get("/api/camera/admin").status_code == 200
        assert client.get("/api/camera/admin", headers={"X-Forwarded-Proto": "https"}).status_code == 403
        assert client.get("/api/camera/admin", headers={"Host": "172.30.80.10:8766"}).status_code == 403
    with TestClient(app, client=("172.30.80.20", 1234)) as client:
        client.post('/api/auth/login', json={'username': 'vps-admin', 'password': 'vps-test-password'},
                    headers={'X-IDI-Request': '1'})
        assert client.get("/api/camera/admin").status_code == 403
    with TestClient(app, client=("203.0.113.5", 1234)) as client:
        assert client.get("/api/cases", headers={"X-Forwarded-For": "172.30.80.1"}).status_code == 403
