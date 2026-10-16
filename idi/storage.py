from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .db import Database, now, uid

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".m4v", ".webm", ".mkv"}
DOCUMENT_EXTS = {".pdf", ".docx", ".xlsx", ".txt", ".csv"}
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS | DOCUMENT_EXTS
Image.MAX_IMAGE_PIXELS = 60_000_000


def safe_name(name: str, limit=110) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    value = value[:limit].rstrip(" .") or "未命名"
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


class Storage:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def resolve(self, relative: str) -> Path:
        path = (self.settings.data_dir / relative).resolve()
        if not path.is_relative_to(self.settings.data_dir):
            raise ValueError("档案路径越界")
        return path

    def put(self, data: bytes, name: str) -> dict:
        if not data or len(data) > self.settings.max_file_bytes:
            raise ValueError("文件为空或超过单文件大小限制")
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_EXTS:
            raise ValueError(f"不支持的文件类型：{suffix or '无扩展名'}")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.db.one("SELECT * FROM blobs WHERE hash=?", (digest,))
        if existing:
            if self.resolve(existing["stored_path"]).is_file():
                if not self.verify(existing):
                    raise ValueError("已归档原件哈希不一致，请从备份核对恢复")
                return {**existing, "exif_time": self._exif(data) if existing["media_type"] == "image" else None}
            suffix = existing["extension"]
        width = height = None
        exif_time = None
        thumb = None
        media_type = "image" if suffix in IMAGE_EXTS else "video" if suffix in VIDEO_EXTS else "document"
        if media_type == "image":
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    if width * height > Image.MAX_IMAGE_PIXELS:
                        raise ValueError("图片像素尺寸超过限制")
                    exif_time = self._exif(data)
                    preview = ImageOps.exif_transpose(image).convert("RGB")
                    preview.thumbnail((640, 640))
                    out = io.BytesIO()
                    preview.save(out, "JPEG", quality=82)
                    thumb = f"thumbnails/{digest[:2]}/{digest}.jpg"
                    self._atomic(self.resolve(thumb), out.getvalue())
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
                raise ValueError("文件无法解码为图片") from error
        stored = f"originals/{digest[:2]}/{digest}{suffix}"
        self._atomic(self.resolve(stored), data)
        blob = {"hash": digest, "stored_path": stored, "thumbnail_path": thumb, "extension": suffix,
                "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "media_type": media_type,
                "size_bytes": len(data), "width": width, "height": height, "created_at": now()}
        with self.db.tx() as con:
            con.execute("INSERT OR IGNORE INTO blobs(hash,stored_path,thumbnail_path,extension,mime_type,media_type,size_bytes,width,height,created_at) VALUES(:hash,:stored_path,:thumbnail_path,:extension,:mime_type,:media_type,:size_bytes,:width,:height,:created_at)", blob)
        return {**blob, "exif_time": exif_time}

    @staticmethod
    def _atomic(path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uid() + ".tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _exif(data: bytes):
        try:
            with Image.open(io.BytesIO(data)) as image:
                exif = image.getexif()
                raw = exif.get(36867) or exif.get(306)
                if raw and re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", str(raw)):
                    return str(raw)[:10].replace(":", "-") + str(raw)[10:]
        except Exception:
            pass
        return None

    def verify(self, blob: dict) -> bool:
        path = self.resolve(blob["stored_path"])
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == blob["hash"]
