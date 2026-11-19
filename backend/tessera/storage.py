"""Object storage for attachments: filesystem (development/default) or S3-compatible."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Protocol

from tessera.config import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


def content_key(data: bytes) -> tuple[str, str]:
    """Return (sha256 hex, storage key). Content-addressed: identical files share one object."""
    digest = hashlib.sha256(data).hexdigest()
    return digest, f"{digest[:2]}/{digest[2:4]}/{digest}"


class FilesystemStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def wipe(self) -> None:  # tests only
        if self.root.exists():
            shutil.rmtree(self.root)


class S3Storage:
    def __init__(
        self, endpoint: str | None, bucket: str, access_key: str | None, secret_key: str | None, region: str
    ) -> None:
        import boto3  # optional dependency (extra "storage")

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        """Create the bucket on first write if the object store does not have it yet (a fresh dev stack)."""
        if self._bucket_ready:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)
        self._bucket_ready = True

    def put(self, key: str, data: bytes) -> None:
        self._ensure_bucket()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return bytes(self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        return True


_storage: Storage | None = None


def get_storage(dbname: str | None = None) -> Storage:
    global _storage
    if _storage is None:
        settings = get_settings()
        if settings.storage_backend == "s3":
            _storage = S3Storage(
                settings.s3_endpoint,
                settings.s3_bucket,
                settings.s3_access_key,
                settings.s3_secret_key,
                settings.s3_region,
            )
        else:
            root = settings.storage_path / (dbname or "default")
            _storage = FilesystemStorage(root)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
