"""Write-only credentials: Windows DPAPI or an external AES-256-GCM key file.

Deliberately excluded from portable archive backups. Never put plaintext in
connector config, audit entries, logs, child arguments, or API responses.
"""
from __future__ import annotations

import ctypes
import os
import re
import stat
from ctypes import wintypes
from pathlib import Path

from ..domain import DomainError
from ..storage import Storage


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


MAGIC = b"IDI-SECRET\x00\x01"


def read_key(path: Path) -> bytes:
    """Read a bounded regular file; Unix keys belong to the service user only."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise DomainError("主密钥必须是普通文件")
            if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise DomainError("主密钥必须属于服务用户，权限为 600 或 400")
            key = stream.read(33)
        if len(key) != 32:
            raise DomainError("主密钥长度无效，必须为 32 字节")
        return key
    except OSError as error:
        raise DomainError("无法读取主密钥文件，请检查 IDI_SECRET_KEY_FILE 和文件权限") from error


def initialize_key(path: Path, data_dir: Path) -> None:
    """Create once. A missing key must never silently replace an existing vault."""
    if path.exists() or path.is_symlink():
        read_key(path)
        return
    if any((data_dir / "secrets").glob("*.bin")):
        raise DomainError("已有加密凭据但主密钥缺失；请恢复原主密钥，不能重新生成")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        read_key(path)
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    read_key(path)


def portable_protect(data: bytes, key: bytes, *, decrypt=False) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    cipher = AESGCM(key)
    if decrypt:
        if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 12 + 16:
            raise DomainError("凭据格式不兼容或已损坏；从 Windows 迁移后请重新填写凭据")
        offset = len(MAGIC)
        try:
            return cipher.decrypt(data[offset:offset + 12], data[offset + 12:], MAGIC)
        except InvalidTag as error:
            raise DomainError("凭据无法解密：主密钥不匹配或文件已损坏") from error
    nonce = os.urandom(12)
    return MAGIC + nonce + cipher.encrypt(nonce, data, MAGIC)


def protect(data: bytes, *, decrypt=False) -> bytes:
    key_file = os.environ.get("IDI_SECRET_KEY_FILE")
    if key_file:
        return portable_protect(data, read_key(Path(key_file)), decrypt=decrypt)
    if os.name != "nt":
        raise DomainError("Linux 凭据存储需要配置 IDI_SECRET_KEY_FILE 主密钥文件")
    if decrypt and data.startswith(MAGIC):
        raise DomainError("此凭据使用主密钥加密，请配置原 IDI_SECRET_KEY_FILE")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN. No machine-wide scope.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise DomainError("凭据无法加密或解密，请使用保存凭据时的 Windows 用户重新配置")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class SecretStore:
    def __init__(self, data_dir):
        self.root = data_dir / "secrets"

    def path(self, connector_id):
        if not re.fullmatch(r"[a-f0-9]{32}", connector_id):
            raise DomainError("采集入口标识无效")
        return self.root / ("feishu-" + connector_id + ".bin")

    def exists(self, connector_id):
        return self.path(connector_id).is_file()

    def save(self, connector_id, value):
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 256:
            raise DomainError("App Secret 格式无效")
        Storage._atomic(self.path(connector_id), protect(value.strip().encode("utf-8")))

    def load(self, connector_id):
        path = self.path(connector_id)
        if not path.is_file():
            raise DomainError("尚未保存飞书 App Secret；恢复备份或更换电脑后需要重新填写")
        return protect(path.read_bytes(), decrypt=True).decode("utf-8")


class VisionSecretStore:
    def __init__(self, data_dir):
        self.path = data_dir / 'secrets' / 'vision-api-key.bin'

    def exists(self):
        return self.path.is_file()

    def save(self, value):
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 4096:
            raise DomainError('API 密钥格式无效')
        Storage._atomic(self.path, protect(value.strip().encode('utf-8')))

    def load(self):
        return protect(self.path.read_bytes(), decrypt=True).decode('utf-8') if self.exists() else ''
