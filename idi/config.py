from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("IDI_DATA_DIR", ROOT / "data")).resolve(),
            host=os.environ.get("IDI_HOST", "127.0.0.1"),
            port=int(os.environ.get("IDI_PORT", "8766")),
            worker_enabled=os.environ.get("IDI_WORKER", "1") != "0",
            platform_write_enabled=os.environ.get("IDI_PLATFORM_WRITE", "0") == "1",
        )

    def prepare(self) -> None:
        for name in ("originals", "thumbnails", "staging", "exports", "backups", "logs", "inbox"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
