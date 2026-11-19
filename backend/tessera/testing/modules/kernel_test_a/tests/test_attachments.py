"""Attachments, object storage and binary fields."""

from __future__ import annotations

import base64
import hashlib

import pytest

from tessera.exceptions import AccessError
from tessera.storage import FilesystemStorage, content_key, get_storage

pytestmark = pytest.mark.db


def test_attachment_roundtrip_and_metadata(env):  # type: ignore[no-untyped-def]
    payload = b"%PDF-1.4 fake content " * 20
    book = env.ref("kernel_test_a.book_pride")
    att = env["core.attachment"].create(
        {"name": "quote.pdf", "model": "ktest.book", "res_id": book.ids[0], "raw": payload}
    )
    assert att.file_size == len(payload)
    assert att.checksum == hashlib.sha256(payload).hexdigest()
    assert att.mimetype == "application/pdf"
    assert att.raw == payload
    storage = get_storage(env.cr.dbname)
    assert storage.exists(att.elevated().store_key)
    # content-addressed: identical content shares one object
    twin = env["core.attachment"].create({"name": "copy.pdf", "raw": payload})
    assert twin.elevated().store_key == att.elevated().store_key
    read = att.read(["name", "raw", "file_size"])[0]
    assert read["raw"] == base64.b64encode(payload).decode()
    # replacing the content updates checksum and size
    att.write({"raw": b"new"})
    assert att.file_size == 3 and att.raw == b"new"
    att.write({"raw": None})
    assert att.file_size == 0 and att.raw is None
    token = att.generate_access_token()
    assert len(token) > 20 and att.generate_access_token() == token
    assert att.action_download()["url"].endswith(f"/attachments/{att.ids[0]}/content")


def test_attachment_access_follows_record(env, env_for):  # type: ignore[no-untyped-def]
    draft = env.ref("kernel_test_a.book_draft")
    att = env["core.attachment"].create(
        {"name": "secret.txt", "model": "ktest.book", "res_id": draft.ids[0], "raw": b"shh"}
    )
    reader = env_for("reader")
    reader_att = reader["core.attachment"].browse(att.ids)
    assert reader_att.name == "secret.txt"  # metadata is readable through the ACL
    with pytest.raises(AccessError):
        reader_att.check_record_access("read")  # but the linked draft is not visible to readers
    with pytest.raises(AccessError):
        _ = reader_att.store_key  # field groups
    public = env["core.attachment"].create(
        {"name": "logo.png", "public": True, "raw": b"png", "model": "ktest.book", "res_id": draft.ids[0]}
    )
    reader["core.attachment"].browse(public.ids).check_record_access("read")


def test_filesystem_storage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = FilesystemStorage(tmp_path / "store")
    digest, key = content_key(b"hello")
    assert digest == hashlib.sha256(b"hello").hexdigest()
    assert key.startswith(digest[:2] + "/" + digest[2:4] + "/")
    storage.put(key, b"hello")
    storage.put(key, b"hello")  # idempotent
    assert storage.exists(key) and storage.get(key) == b"hello"
    storage.delete(key)
    assert not storage.exists(key)
    with pytest.raises(ValueError):
        storage.put("../escape", b"x")
    storage.wipe()
    assert not (tmp_path / "store").exists()


def test_inline_binary_field(env):  # type: ignore[no-untyped-def]
    company = env.ref("core.main_company")
    company.write({"logo": base64.b64encode(b"\x89PNG").decode()})
    assert company.logo == b"\x89PNG"
    assert company.read(["logo"])[0]["logo"] == base64.b64encode(b"\x89PNG").decode()
    assert "logo" not in company.read(["name"])[0]
