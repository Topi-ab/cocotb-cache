"""Content-addressed store on S3 (docs/format.md).

Layout under ``<prefix>v1/``:

* ``blobs/<h[0:2]>/<h>.gz``: file contents, gzip-compressed with a
  deterministic header (mtime 0, no file name, level 6). The key is the
  SHA-256 of the *uncompressed* content.
* ``src/<h[0:2]>/<h>.json``: SRC manifests as uncompressed canonical JSON; the
  key is the SHA-256 of exactly those bytes.
* ``result/<h[0:2]>/<h>.json``: RESULT manifests, keyed by their SRC key and
  written with ``If-None-Match: *`` so the first writer wins.

Every read is verified: a blob or manifest that does not match its key is
reported as corrupt and never used.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import IO, Any

from botocore.exceptions import ClientError

from .hashing import CHUNK_SIZE, is_digest, sha256_bytes, sha256_file
from .manifest import ManifestError, ResultManifest, SrcManifest

SCHEMA_PREFIX = "v1/"
GZIP_LEVEL = 6
GZIP_CONTENT_TYPE = "application/gzip"
JSON_CONTENT_TYPE = "application/json"

# Compressed blobs up to this size are kept in memory while uploading or
# downloading; larger ones spill to a temporary file.
SPOOL_MAX = 8 * 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    """One object in the store, as listed."""

    kind: str  # "blobs", "src" or "result"
    digest: str
    key: str
    size: int
    last_modified: datetime


class StoreError(Exception):
    """Base class for store failures."""


class BlobMissing(StoreError):
    """The blob does not exist in the store."""


class BlobCorrupt(StoreError):
    """The blob's content does not match its key (or its expected size)."""


class ManifestCorrupt(StoreError):
    """A stored manifest is malformed or does not match its key."""


DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 30.0
DEFAULT_MAX_ATTEMPTS = 3


def _env_number(name: str, default: float) -> float:
    import os

    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def make_client() -> Any:
    """S3 client from the standard ``AWS_*`` environment, path-style.

    Endpoint, credentials and region come from ``AWS_ENDPOINT_URL``,
    ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` and ``AWS_DEFAULT_REGION``.

    Timeouts are short, unlike boto3's defaults (60 s + retries), so that an
    unreachable S3 costs a run seconds, not minutes:
    ``COCOTB_CACHE_S3_CONNECT_TIMEOUT`` (default 5 s),
    ``COCOTB_CACHE_S3_READ_TIMEOUT`` (30 s), ``COCOTB_CACHE_S3_MAX_ATTEMPTS``
    (3, all tries including the first).
    """
    import boto3
    from botocore.config import Config

    config = Config(
        s3={"addressing_style": "path"},
        connect_timeout=_env_number("COCOTB_CACHE_S3_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT_S),
        read_timeout=_env_number("COCOTB_CACHE_S3_READ_TIMEOUT", DEFAULT_READ_TIMEOUT_S),
        # total_max_attempts counts the first try too (botocore's max_attempts
        # would count only the retries).
        retries={
            "mode": "standard",
            "total_max_attempts": int(_env_number("COCOTB_CACHE_S3_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        },
    )
    # A fresh session: boto3's default session caches credentials for the
    # life of the process, so a changed environment would go unnoticed.
    return boto3.session.Session().client("s3", config=config)


def _not_found(e: ClientError) -> bool:
    code = e.response.get("Error", {}).get("Code")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def _gzip_to(src: IO[bytes], dst: IO[bytes]) -> None:
    # filename="" and mtime=0 make the compressed bytes depend only on content.
    with gzip.GzipFile(filename="", mode="wb", fileobj=dst, compresslevel=GZIP_LEVEL, mtime=0) as gz:
        shutil.copyfileobj(src, gz, CHUNK_SIZE)


class Store:
    """Blob and manifest operations on one bucket under one key prefix."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    # --- keys -----------------------------------------------------------------

    def blob_key(self, digest: str) -> str:
        if not is_digest(digest):
            raise ValueError(f"not a SHA-256 hex digest: {digest!r}")
        return f"{self.prefix}{SCHEMA_PREFIX}blobs/{digest[:2]}/{digest}.gz"

    # --- queries --------------------------------------------------------------

    def has_blob(self, digest: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.blob_key(digest))
        except ClientError as e:
            if _not_found(e):
                return False
            raise
        return True

    # --- writes ---------------------------------------------------------------

    def put_bytes(self, data: bytes) -> tuple[str, bool]:
        """Store *data*. Returns ``(digest, uploaded)``; existing blobs are skipped."""
        digest = sha256_bytes(data)
        if self.has_blob(digest):
            return digest, False
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX) as buf:
            _gzip_to(io.BytesIO(data), buf)
            self._upload(digest, buf)
        return digest, True

    def put_file(self, path: str | PathLike[str], digest: str | None = None) -> tuple[str, bool]:
        """Store the file at *path*, streaming. Returns ``(digest, uploaded)``.

        *digest* may be passed when already known (e.g. from building a
        manifest); it is re-checked against the file while compressing, so a
        file that changed in between is not stored under a wrong key.
        """
        if digest is None:
            digest, _ = sha256_file(path)
        if self.has_blob(digest):
            return digest, False
        with open(path, "rb") as src, tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX) as buf:
            hashing = _HashingReader(src)
            _gzip_to(hashing, buf)
            if hashing.hexdigest() != digest:
                raise StoreError(f"{path}: content changed while storing (expected {digest})")
            self._upload(digest, buf)
        return digest, True

    def _upload(self, digest: str, buf: IO[bytes]) -> None:
        buf.seek(0)
        # upload_fileobj switches to multipart for large blobs.
        self.client.upload_fileobj(
            buf, self.bucket, self.blob_key(digest), ExtraArgs={"ContentType": GZIP_CONTENT_TYPE}
        )

    # --- reads ----------------------------------------------------------------

    @contextmanager
    def _download(self, digest: str) -> Iterator[IO[bytes]]:
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX) as buf:
            try:
                self.client.download_fileobj(self.bucket, self.blob_key(digest), buf)
            except ClientError as e:
                if _not_found(e):
                    raise BlobMissing(digest) from e
                raise
            buf.seek(0)
            yield buf

    def get_bytes(self, digest: str, expected_size: int | None = None) -> bytes:
        """Fetch and verify a blob into memory (for small blobs such as console logs)."""
        with self._download(digest) as buf:
            try:
                data = gzip.GzipFile(fileobj=buf, mode="rb").read()
            except (OSError, EOFError, zlib.error) as e:
                raise BlobCorrupt(f"{digest}: not valid gzip ({e})") from e
        if sha256_bytes(data) != digest:
            raise BlobCorrupt(f"{digest}: content does not match key")
        if expected_size is not None and len(data) != expected_size:
            raise BlobCorrupt(f"{digest}: size {len(data)} != expected {expected_size}")
        return data

    def get_file(
        self,
        digest: str,
        dest: str | PathLike[str],
        *,
        expected_size: int | None = None,
        executable: bool = False,
    ) -> None:
        """Fetch a blob to *dest*, streaming, verified before it appears.

        The content is written to a temporary file next to *dest* and renamed
        into place only after the hash (and size, if given) matched, so *dest*
        is never left with corrupt or partial content.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".cocotb-cache")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, self._download(digest) as buf:
                hashing = _HashingWriter(out)
                try:
                    with gzip.GzipFile(fileobj=buf, mode="rb") as gz:
                        shutil.copyfileobj(gz, hashing, CHUNK_SIZE)
                except (OSError, EOFError, zlib.error) as e:
                    raise BlobCorrupt(f"{digest}: not valid gzip ({e})") from e
            if hashing.hexdigest() != digest:
                raise BlobCorrupt(f"{digest}: content does not match key")
            if expected_size is not None and hashing.size != expected_size:
                raise BlobCorrupt(f"{digest}: size {hashing.size} != expected {expected_size}")
            mode = 0o666 & ~_umask()
            if executable:
                mode |= 0o111 & ~_umask()
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    # --- manifests ------------------------------------------------------------

    def src_key(self, key: str) -> str:
        if not is_digest(key):
            raise ValueError(f"not a SHA-256 hex digest: {key!r}")
        return f"{self.prefix}{SCHEMA_PREFIX}src/{key[:2]}/{key}.json"

    def result_key(self, key: str) -> str:
        if not is_digest(key):
            raise ValueError(f"not a SHA-256 hex digest: {key!r}")
        return f"{self.prefix}{SCHEMA_PREFIX}result/{key[:2]}/{key}.json"

    def put_src(self, manifest: SrcManifest) -> tuple[str, bool]:
        """Store an SRC manifest. Returns ``(key, written)``.

        Content-addressed, so an existing object already has these bytes and
        ``written`` is ``False``.
        """
        data = manifest.to_bytes()
        key = sha256_bytes(data)
        return key, self._put_if_absent(self.src_key(key), data)

    def get_src(self, key: str) -> SrcManifest | None:
        """Fetch an SRC manifest, or ``None`` if absent.

        Raises:
            ManifestCorrupt: the bytes do not hash to *key* or do not parse.
        """
        data = self._get_or_none(self.src_key(key))
        if data is None:
            return None
        if sha256_bytes(data) != key:
            raise ManifestCorrupt(f"src {key}: content does not match key")
        try:
            return SrcManifest.from_bytes(data)
        except ManifestError as e:
            raise ManifestCorrupt(f"src {key}: {e}") from e

    def put_result(self, manifest: ResultManifest) -> bool:
        """Publish a RESULT manifest under its SRC key; the first writer wins.

        This is the commit point of a miss (docs/format.md): the caller must
        have stored every blob in ``manifest.blobs()`` before. Returns ``True``
        if this call wrote it, ``False`` if a result already existed.
        """
        return self._put_if_absent(self.result_key(manifest.src), manifest.to_bytes())

    def get_result(self, key: str) -> ResultManifest | None:
        """Fetch the RESULT manifest for SRC *key*, or ``None`` on a miss.

        Raises:
            ManifestCorrupt: it does not parse or belongs to another key.
        """
        data = self._get_or_none(self.result_key(key))
        if data is None:
            return None
        try:
            manifest = ResultManifest.from_bytes(data)
        except ManifestError as e:
            raise ManifestCorrupt(f"result {key}: {e}") from e
        if manifest.src != key:
            raise ManifestCorrupt(f"result {key}: belongs to src {manifest.src}")
        return manifest

    def _put_if_absent(self, key: str, data: bytes) -> bool:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=JSON_CONTENT_TYPE, IfNoneMatch="*"
            )
        except ClientError as e:
            code, status = _error_code(e)
            if status == 412 or code == "PreconditionFailed":
                return False  # already there
            if status == 409 or code == "ConditionalRequestConflict":
                return False  # a concurrent conditional write of the same key
            if status == 501 or code == "NotImplemented":
                # Server without conditional writes: last writer wins, which is
                # safe because both results describe the same inputs.
                self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=JSON_CONTENT_TYPE)
                return True
            raise
        return True

    # --- listing and deletion (CLI: stat, gc, inspect) -----------------------------

    KINDS = ("blobs", "src", "result")

    def kind_prefix(self, kind: str) -> str:
        if kind not in self.KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        return f"{self.prefix}{SCHEMA_PREFIX}{kind}/"

    def list_objects(self, kind: str, start: str = "") -> Iterator[StoredObject]:
        """All objects of *kind* (optionally whose digest starts with *start*)."""
        prefix = self.kind_prefix(kind)
        if len(start) >= 2:
            prefix += f"{start[:2]}/{start}"
        elif start:
            prefix += start  # matches the two-character directories
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                digest = name.split(".", 1)[0]
                if is_digest(digest):
                    yield StoredObject(kind, digest, obj["Key"], obj["Size"], obj["LastModified"])

    def delete_key(self, key: str) -> None:
        if not key.startswith(self.prefix + SCHEMA_PREFIX):
            raise ValueError(f"refusing to delete outside the store: {key}")
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def _get_or_none(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if _not_found(e):
                return None
            raise


def _error_code(e: ClientError) -> tuple[str | None, int | None]:
    return (
        e.response.get("Error", {}).get("Code"),
        e.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
    )


def _umask() -> int:
    """Current umask without changing it (os.umask() would, racing other threads)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except OSError:
        pass
    return 0o022


class _HashingReader:
    """File-like wrapper that hashes everything read through it."""

    def __init__(self, f: IO[bytes]) -> None:
        self._f = f
        self._h = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        self._h.update(data)
        return data

    def hexdigest(self) -> str:
        return self._h.hexdigest()


class _HashingWriter:
    """File-like wrapper that hashes and counts everything written through it."""

    def __init__(self, f: IO[bytes]) -> None:
        self._f = f
        self._h = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self._h.update(data)
        self.size += len(data)
        return self._f.write(data)

    def hexdigest(self) -> str:
        return self._h.hexdigest()
