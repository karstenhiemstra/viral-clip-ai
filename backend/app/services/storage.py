"""File storage abstraction: local filesystem (default) or any S3-compatible bucket
(AWS S3, Cloudflare R2, MinIO, Supabase Storage S3 endpoint)."""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class Storage(Protocol):
    def put_file(self, key: str, src: Path, content_type: str | None = None) -> str: ...
    def local_path(self, key: str) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def public_url(self, key: str) -> str | None: ...


def _safe_key(key: str) -> str:
    key = key.replace("\\", "/").lstrip("/")
    if ".." in key.split("/"):
        raise ValueError("invalid storage key")
    return key


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    def put_file(self, key: str, src: Path, content_type: str | None = None) -> str:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(src).resolve() != dst.resolve():
            shutil.move(str(src), dst)
        return key

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def public_url(self, key: str) -> str | None:
        return None  # served by the API (/api/media/...)


class S3Storage:  # pragma: no cover - needs a bucket
    def __init__(self) -> None:
        import boto3  # optional dependency: pip install '.[s3]'

        s = get_settings()
        self.bucket = s.s3_bucket
        self.public_base = s.s3_public_base_url.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            aws_access_key_id=s.s3_access_key_id or None,
            aws_secret_access_key=s.s3_secret_access_key or None,
            region_name=s.s3_region or None,
        )
        self.cache = s.data_dir / "s3-cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def put_file(self, key: str, src: Path, content_type: str | None = None) -> str:
        key = _safe_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(str(src), self.bucket, key, ExtraArgs=extra)
        cached = self.cache / key
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), cached)
        return key

    def local_path(self, key: str) -> Path:
        key = _safe_key(key)
        cached = self.cache / key
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, key, str(cached))
        return cached

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=_safe_key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=_safe_key(key))
        cached = self.cache / _safe_key(key)
        if cached.exists():
            cached.unlink()

    def public_url(self, key: str) -> str | None:
        if self.public_base:
            return f"{self.public_base}/{_safe_key(key)}"
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": _safe_key(key)}, ExpiresIn=3600
        )


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend == "s3":
        return S3Storage()
    return LocalStorage(s.storage_dir)
