"""Container bootstrap. Single server, persistent vault, explicit private network."""
from __future__ import annotations

import json
import os
import sys
from ipaddress import ip_address, ip_network
from pathlib import Path

from .config import Settings
from .connectors.secret_store import initialize_key, protect, read_key
from .domain import DomainError


def network_config():
    subnet = os.environ.get("IDI_VPS_SUBNET", "172.30.80.0/24")
    gateway = os.environ.get("IDI_VPS_GATEWAY", "172.30.80.1")
    app_ip = os.environ.get("IDI_VPS_APP_IP", "172.30.80.10")
    proxy_ip = os.environ.get("IDI_VPS_PROXY_IP", "172.30.80.20")
    domain = os.environ.get("IDI_DOMAIN", "").strip()
    network = ip_network(subnet, strict=True)
    addresses = [ip_address(value) for value in (gateway, app_ip, proxy_ip)]
    if len(set(addresses)) != 3 or any(value not in network or value in (network.network_address, network.broadcast_address) for value in addresses):
        raise ValueError("VPS 网关、应用及代理必须使用网段内三个不同的有效地址")
    value = dict(deployment="vps", host="0.0.0.0", lan_hosts=[app_ip], lan_subnets=[subnet],
                 local_admin_proxies=[gateway], trusted_proxies=[proxy_ip],
                 public_origins=["https://" + domain] if domain else [])
    # Reuse the application's private-network and HTTPS-host validation.
    Settings(Path("/unused"), **{k: tuple(v) if isinstance(v, list) else v for k, v in value.items()})
    return value


def prepare():
    os.umask(0o077)
    data_dir = Path(os.environ.get("IDI_DATA_DIR", "/var/lib/idi"))
    key_file = Path(os.environ.get("IDI_SECRET_KEY_FILE", "/var/lib/idi-key/master.key"))
    if key_file.resolve().is_relative_to(data_dir.resolve()):
        raise DomainError("VPS 主密钥必须保存在档案目录之外的独立持久卷")
    data_dir.mkdir(parents=True, exist_ok=True)
    initialize_key(key_file, data_dir)
    os.environ["IDI_SECRET_KEY_FILE"] = str(key_file)
    read_key(key_file)
    # Check all saved credentials before workers start or external calls are made.
    for path in (data_dir / "secrets").glob("*.bin"):
        protect(path.read_bytes(), decrypt=True)
    config_path = Path(os.environ.get("IDI_NETWORK_FILE", "/run/idi/network.json"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(network_config()), encoding="utf-8")
    os.environ["IDI_NETWORK_FILE"] = str(config_path)
    os.environ["IDI_DATA_DIR"] = str(data_dir)
    Settings.from_env()


def main():
    try:
        prepare()
    except (DomainError, OSError, ValueError) as error:
        raise SystemExit(f"IDI VPS 启动检查失败：{error}") from None
    # exec preserves signal delivery and the existing cross-platform instance lock.
    os.execv(sys.executable, [sys.executable, "-m", "idi.server", "--port", os.environ.get("IDI_PORT", "8766")])


if __name__ == "__main__":
    main()
