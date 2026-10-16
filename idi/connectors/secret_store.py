"""Write-only application credentials protected by the current Windows account.

Deliberately excluded from portable archive backups. Never put plaintext in
connector config, audit entries, logs, child arguments, or API responses.
"""
from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

from ..domain import DomainError
from ..storage import Storage


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise DomainError("凭据加密存储需要 Windows 当前用户环境")
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
