"""Store against the local versitygw (docs/format.md)."""

from __future__ import annotations

import gzip
import io
import os
import stat
from pathlib import Path

import pytest

from cocotb_cache.hashing import sha256_bytes
from cocotb_cache.store import GZIP_CONTENT_TYPE, BlobCorrupt, BlobMissing, Store, StoreError
from s3_local import S3Endpoint

pytestmark = pytest.mark.s3


@pytest.fixture
def store(local_s3: S3Endpoint, s3_prefix: str) -> Store:
    return Store(local_s3.client(), local_s3.bucket, s3_prefix)


def _raw(store: Store, digest: str) -> dict:
    return store.client.get_object(Bucket=store.bucket, Key=store.blob_key(digest))


def test_key_layout(store: Store) -> None:
    d = sha256_bytes(b"x")
    assert store.blob_key(d) == f"{store.prefix}v1/blobs/{d[:2]}/{d}.gz"
    with pytest.raises(ValueError):
        store.blob_key("nothex")


def test_bytes_roundtrip_and_dedupe(store: Store) -> None:
    data = b"hello blob\n"
    d, uploaded = store.put_bytes(data)
    assert (d, uploaded) == (sha256_bytes(data), True)
    assert store.has_blob(d)
    assert store.put_bytes(data) == (d, False)  # already there: skipped
    assert store.get_bytes(d, expected_size=len(data)) == data


def test_empty_blob(store: Store) -> None:
    d, _ = store.put_bytes(b"")
    assert store.get_bytes(d) == b""


def test_stored_object_format(store: Store) -> None:
    data = b"format check " * 100
    d, _ = store.put_bytes(data)
    obj = _raw(store, d)
    body = obj["Body"].read()
    assert obj["ContentType"] == GZIP_CONTENT_TYPE
    assert obj.get("ContentEncoding") in (None, "")  # never Content-Encoding: gzip
    assert body[:2] == b"\x1f\x8b"
    assert body[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0
    assert body[3] & 0x08 == 0  # FNAME flag not set
    assert gzip.decompress(body) == data


def test_compression_is_deterministic(local_s3: S3Endpoint, store: Store, s3_prefix: str) -> None:
    other = Store(local_s3.client(), local_s3.bucket, s3_prefix + "other/")
    data = os.urandom(5000)
    d, _ = store.put_bytes(data)
    other.put_bytes(data)
    assert _raw(store, d)["Body"].read() == _raw(other, d)["Body"].read()


def test_missing_blob(store: Store, tmp_path: Path) -> None:
    d = sha256_bytes(b"never stored")
    assert not store.has_blob(d)
    with pytest.raises(BlobMissing):
        store.get_bytes(d)
    with pytest.raises(BlobMissing):
        store.get_file(d, tmp_path / "x")
    assert not (tmp_path / "x").exists()


def _plant(store: Store, digest: str, body: bytes) -> None:
    store.client.put_object(Bucket=store.bucket, Key=store.blob_key(digest), Body=body)


def test_corrupt_content_rejected_and_dest_untouched(store: Store, tmp_path: Path) -> None:
    d = sha256_bytes(b"the real content")
    _plant(store, d, gzip.compress(b"something else"))
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"previous")
    with pytest.raises(BlobCorrupt):
        store.get_bytes(d)
    with pytest.raises(BlobCorrupt):
        store.get_file(d, dest)
    assert dest.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]  # no temp file left


def test_not_gzip_rejected(store: Store, tmp_path: Path) -> None:
    d = sha256_bytes(b"x")
    _plant(store, d, b"plain, not gzip")
    with pytest.raises(BlobCorrupt):
        store.get_bytes(d)
    with pytest.raises(BlobCorrupt):
        store.get_file(d, tmp_path / "x")


def test_size_mismatch_rejected(store: Store, tmp_path: Path) -> None:
    d, _ = store.put_bytes(b"12345")
    with pytest.raises(BlobCorrupt, match="size"):
        store.get_bytes(d, expected_size=4)
    with pytest.raises(BlobCorrupt, match="size"):
        store.get_file(d, tmp_path / "x", expected_size=6)


def test_large_file_streaming_roundtrip(store: Store, tmp_path: Path) -> None:
    # Larger than the spool limit and the multipart threshold (8 MiB).
    src = tmp_path / "big.bin"
    data = os.urandom(12 * 1024 * 1024)
    src.write_bytes(data)
    d, uploaded = store.put_file(src)
    assert uploaded and d == sha256_bytes(data)
    dest = tmp_path / "sub" / "dir" / "big.out"
    store.get_file(d, dest, expected_size=len(data))
    assert dest.read_bytes() == data


def test_put_file_with_known_digest_and_change_detection(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_bytes(b"version 1")
    d1 = sha256_bytes(b"version 1")
    assert store.put_file(f, d1) == (d1, True)
    f.write_bytes(b"version 2")  # changed after its digest was taken
    with pytest.raises(StoreError, match="changed"):
        store.put_file(f, sha256_bytes(b"stale digest"))


def test_executable_bit(store: Store, tmp_path: Path) -> None:
    d, _ = store.put_bytes(b"#!/bin/sh\necho hi\n")
    exe, plain = tmp_path / "run", tmp_path / "data"
    store.get_file(d, exe, executable=True)
    store.get_file(d, plain)
    assert exe.stat().st_mode & stat.S_IXUSR
    assert not plain.stat().st_mode & stat.S_IXUSR
    assert plain.stat().st_mode & stat.S_IRUSR


def test_get_file_replaces_existing(store: Store, tmp_path: Path) -> None:
    d, _ = store.put_bytes(b"new")
    dest = tmp_path / "f"
    dest.write_bytes(b"old content")
    store.get_file(d, dest)
    assert dest.read_bytes() == b"new"


def test_prefix_isolation(local_s3: S3Endpoint, s3_prefix: str) -> None:
    a = Store(local_s3.client(), local_s3.bucket, s3_prefix + "a")
    b = Store(local_s3.client(), local_s3.bucket, s3_prefix + "b/")
    d, _ = a.put_bytes(b"only in a")
    assert a.has_blob(d)
    assert not b.has_blob(d)
    assert io.BytesIO(a.get_bytes(d)).read() == b"only in a"
