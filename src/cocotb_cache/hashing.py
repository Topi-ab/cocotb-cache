"""Content hashing and canonical JSON (docs/format.md).

Every key in the store is a SHA-256 digest in lowercase hex:

* a file's key is the hash of its raw bytes (no newline or encoding
  normalisation);
* a manifest's key is the hash of its canonical JSON encoding.

The canonical encoding is part of the on-disk format: changing it changes
every manifest key, so it must only change together with the schema version.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from os import PathLike
from typing import Any, BinaryIO

ALGORITHM = "sha256"
DIGEST_HEX_LEN = 64

# Read size for streaming file hashes; large enough to be fast, small enough
# that hashing a multi-GB waveform does not load it into memory.
CHUNK_SIZE = 1024 * 1024

_HEX_RE = re.compile(r"[0-9a-f]{64}")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """Hash a binary stream incrementally.

    Returns:
        ``(hex_digest, size_in_bytes)``.
    """
    h = hashlib.sha256()
    size = 0
    while chunk := stream.read(chunk_size):
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


def sha256_file(path: str | PathLike[str], chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """Hash a file's raw bytes without reading it into memory at once.

    Returns:
        ``(hex_digest, size_in_bytes)``.
    """
    with open(path, "rb") as f:
        return sha256_stream(f, chunk_size)


def is_digest(value: object) -> bool:
    """Return ``True`` if *value* is a well-formed key: 64 lowercase hex chars."""
    return isinstance(value, str) and _HEX_RE.fullmatch(value) is not None


def _check(obj: Any, path: str) -> None:
    """Reject values whose JSON encoding would be ambiguous or lossy."""
    if obj is None or isinstance(obj, (bool, str, int)):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"{path}: non-finite float {obj!r} has no canonical JSON form")
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: object key {key!r} is not a string")
            _check(value, f"{path}.{key}")
        return
    if isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            _check(value, f"{path}[{i}]")
        return
    raise TypeError(f"{path}: {type(obj).__name__} is not JSON-serialisable in canonical form")


def canonical_json(obj: Any) -> bytes:
    """Encode *obj* as canonical JSON (UTF-8 bytes).

    Rules (docs/format.md): object keys sorted, separators ``,`` and ``:`` without
    whitespace, UTF-8 without ``\\u`` escapes for non-ASCII text, no trailing
    newline. List order is preserved; callers that need a canonical order
    (such as a manifest's ``files`` list sorted by ``path``) sort before
    encoding.

    Raises:
        TypeError: for non-string object keys or non-JSON types (bytes, sets, ...).
        ValueError: for NaN or infinite floats.
    """
    _check(obj, "$")
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(obj: Any) -> str:
    """SHA-256 of :func:`canonical_json` of *obj*: the key of a manifest."""
    return sha256_bytes(canonical_json(obj))
