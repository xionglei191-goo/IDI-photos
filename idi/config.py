from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8766
    max_file_bytes: int = 64 * 1024 * 1024
    max_batch_bytes: int = 1024 * 1024 * 1024
    max_batch_files: int = 5000
    worker_enabled: bool = True
    platform_write_enabled: bool = False
    lan_hosts: tuple[str, ...] = ()
    lan_subnets: tuple[str, ...] = ()
    public_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()

    def __post_init__(self):
        address = ip_address(self.host)
        if address.version != 4:
            raise ValueError("服务监听地址必须是 IPv4 地址")
        private_ranges = tuple(ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        networks = tuple(ip_network(value, strict=False) for value in self.lan_subnets)
        if any(net.version != 4 or not any(net.subnet_of(private) for private in private_ranges) for net in networks):
            raise ValueError("局域网访问范围必须是私有 IPv4 网段")
        hosts = tuple(ip_address(value) for value in self.lan_hosts)
        if any(host.version != 4 or not any(host in net for net in networks) for host in hosts):
            raise ValueError("局域网地址必须位于配置的访问网段内")
        if not address.is_loopback:
            if not hosts or not networks:
                raise ValueError("开放局域网前必须配置本机局域网地址和允许访问的网段")
            if not address.is_unspecified and address not in hosts:
                raise ValueError("监听地址不在本机局域网地址列表中")
        for value in self.trusted_proxies:
            proxy = ip_address(value)
            if proxy.version != 4 or (not proxy.is_loopback and not any(proxy in net for net in networks)):
                raise ValueError("受信代理必须是本机或已允许局域网内的明确 IPv4 地址")
        for origin in self.public_origins:
            parsed = urlsplit(origin)
            hostname = parsed.hostname or ""
            if (len(hostname) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname)
                    or origin != "https://" + hostname):
                raise ValueError("公网入口必须是完整 HTTPS 域名，不带通配符、端口、路径或凭据")
        if self.public_origins and not self.trusted_proxies:
            raise ValueError("配置公网入口时必须指定 Tunnel 或反向代理的实际来源 IP")

    @property
    def local_only(self) -> bool:
        return ip_address(self.host).is_loopback

    @property
    def trusted_hosts(self) -> list[str]:
        local = [self.host] if self.local_only else list(self.lan_hosts)
        return ["127.0.0.1", "localhost", "[::1]", "testserver", *local, *self.public_hosts]

    @property
    def public_hosts(self) -> list[str]:
        return [urlsplit(origin).hostname for origin in self.public_origins]

    def proxy_origin(self, peer: str, host: str, forwarded_proto: str) -> str | None:
        if peer not in self.trusted_proxies or forwarded_proto != "https":
            return None
        origin = "https://" + host.lower()
        return origin if origin in self.public_origins else None

    @property
    def lan_urls(self) -> list[str]:
        return [] if self.local_only else [f"http://{host}:{self.port}" for host in self.lan_hosts]

    def allows_client(self, host: str) -> bool:
        if host == "testclient":
            return True
        try:
            address = ip_address(host)
        except ValueError:
            return False
        if address.is_loopback:
            return True
        return not self.local_only and any(address in ip_network(value, strict=False) for value in self.lan_subnets)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("IDI_DATA_DIR", ROOT / "data")).resolve()
        path = data_dir / "network.json"
        network = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        if not isinstance(network, dict):
            raise ValueError("network.json 必须是网络配置对象")
        for key in ("lan_hosts", "lan_subnets", "public_origins", "trusted_proxies"):
            if not isinstance(network.get(key, []), list) or any(not isinstance(value, str) for value in network.get(key, [])):
                raise ValueError(f"network.json 的 {key} 必须是字符串列表")
        return cls(
            data_dir=data_dir,
            host=os.environ.get("IDI_HOST", network.get("host", "127.0.0.1")),
            port=int(os.environ.get("IDI_PORT", "8766")),
            worker_enabled=os.environ.get("IDI_WORKER", "1") != "0",
            platform_write_enabled=os.environ.get("IDI_PLATFORM_WRITE", "0") == "1",
            lan_hosts=tuple(network.get("lan_hosts", [])),
            lan_subnets=tuple(network.get("lan_subnets", [])),
            public_origins=tuple(network.get("public_origins", [])),
            trusted_proxies=tuple(network.get("trusted_proxies", [])),
        )

    def prepare(self) -> None:
        for name in ("originals", "thumbnails", "staging", "exports", "backups", "logs", "inbox"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
