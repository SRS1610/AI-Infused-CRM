"""Object storage backends."""

from __future__ import annotations

from typing import Any

from tessera.storage import FilesystemStorage, S3Storage


class FakeS3:
    def __init__(self, buckets: set[str]) -> None:
        self.buckets = buckets
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []

    def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 keyword names
        self.calls.append("head_bucket")
        if Bucket not in self.buckets:
            raise RuntimeError("404 Not Found")

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803
        self.calls.append("create_bucket")
        self.buckets.add(Bucket)

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        if Bucket not in self.buckets:
            raise RuntimeError("NoSuchBucket")
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        body = self.objects[(Bucket, Key)]
        return {"Body": type("B", (), {"read": lambda self: body})()}

    def head_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("404")

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop((Bucket, Key), None)


def _s3(fake: FakeS3) -> S3Storage:
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "tessera"
    storage.client = fake
    storage._bucket_ready = False
    return storage


def test_s3_creates_a_missing_bucket_once_then_stores() -> None:
    fake = FakeS3(set())
    storage = _s3(fake)
    storage.put("a/b", b"one")
    storage.put("a/c", b"two")
    assert fake.calls == ["head_bucket", "create_bucket"]
    assert storage.get("a/b") == b"one" and storage.exists("a/c") and not storage.exists("nope")
    storage.delete("a/b")
    assert not storage.exists("a/b")


def test_s3_existing_bucket_is_not_recreated() -> None:
    fake = FakeS3({"tessera"})
    _s3(fake).put("k", b"v")
    assert fake.calls == ["head_bucket"]


def test_filesystem_round_trip(tmp_path: Any) -> None:
    storage = FilesystemStorage(tmp_path / "files")
    storage.put("ab/cd", b"data")
    assert storage.get("ab/cd") == b"data" and storage.exists("ab/cd")
    storage.delete("ab/cd")
    assert not storage.exists("ab/cd")
