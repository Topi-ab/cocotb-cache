"""Unit tests for cocotb_cache.hashing (docs/format.md)."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from cocotb_cache.hashing import (
    canonical_hash,
    canonical_json,
    is_digest,
    sha256_bytes,
    sha256_file,
    sha256_stream,
)

EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# --- raw bytes and files ----------------------------------------------------


def test_known_vectors() -> None:
    assert sha256_bytes(b"") == EMPTY
    assert sha256_bytes(b"abc") == ABC


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 1024 * 1024])
def test_stream_matches_one_shot(chunk_size: int) -> None:
    data = os.urandom(10_000)
    digest, size = sha256_stream(io.BytesIO(data), chunk_size)
    assert digest == hashlib.sha256(data).hexdigest()
    assert size == len(data)


def test_file_hash_and_size(tmp_path: Path) -> None:
    data = os.urandom(3 * 1024 * 1024 + 5)  # spans several default chunks
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    assert sha256_file(p) == (hashlib.sha256(data).hexdigest(), len(data))


def test_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert sha256_file(p) == (EMPTY, 0)


def test_file_bytes_are_not_normalised(tmp_path: Path) -> None:
    # CRLF vs LF must give different keys: the hash is over raw bytes.
    a, b = tmp_path / "lf", tmp_path / "crlf"
    a.write_bytes(b"x\n")
    b.write_bytes(b"x\r\n")
    assert sha256_file(a)[0] != sha256_file(b)[0]


def test_is_digest() -> None:
    assert is_digest(EMPTY)
    assert not is_digest(EMPTY.upper())
    assert not is_digest(EMPTY[:-1])
    assert not is_digest(EMPTY + "0")
    assert not is_digest("g" * 64)
    assert not is_digest(None)


# --- canonical JSON ---------------------------------------------------------


def test_key_order_does_not_matter() -> None:
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 3}}) == canonical_json(
        {"a": {"x": 3, "y": 2}, "b": 1}
    )


def test_exact_encoding() -> None:
    assert canonical_json({"b": [1, True, None], "a": "x"}) == b'{"a":"x","b":[1,true,null]}'


def test_no_trailing_newline_or_whitespace() -> None:
    out = canonical_json({"a": [1, 2], "b": {"c": "d"}})
    assert not out.endswith(b"\n")
    assert b" " not in out


def test_list_order_is_preserved() -> None:
    assert canonical_json([2, 1]) != canonical_json([1, 2])


def test_unicode_is_utf8_not_escaped() -> None:
    out = canonical_json({"polku": "äö/€.vhd"})
    assert out == '{"polku":"äö/€.vhd"}'.encode()
    assert b"\\u" not in out


def test_bool_and_int_are_distinct() -> None:
    assert canonical_hash({"x": True}) != canonical_hash({"x": 1})


def test_tuple_encodes_as_list() -> None:
    assert canonical_json({"t": (1, 2)}) == canonical_json({"t": [1, 2]})


def test_roundtrip_is_stable() -> None:
    obj = {"files": [{"path": "a.vhd", "sha256": EMPTY, "size": 0}], "n": 1.5}
    once = canonical_json(obj)
    assert canonical_json(json.loads(once)) == once


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_floats_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": bad})


@pytest.mark.parametrize("bad", [{1: "a"}, {"x": b"bytes"}, {"x": {1, 2}}, {"x": object()}])
def test_ambiguous_types_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        canonical_json(bad)


def test_golden_hash() -> None:
    # Pins the canonical format. If this changes, every manifest key changes:
    # bump the schema version (docs/format.md) instead of editing this value.
    obj = {
        "schema": "cocotb-cache/src/v1",
        "files": [{"path": "smoke/counter.vhd", "sha256": EMPTY, "size": 0}],
        "run": {"seed": 12345, "waves": False, "timescale": None},
        "text": "ä",
    }
    assert canonical_json(obj) == (
        '{"files":[{"path":"smoke/counter.vhd","sha256":"' + EMPTY + '","size":0}],'
        '"run":{"seed":12345,"timescale":null,"waves":false},'
        '"schema":"cocotb-cache/src/v1","text":"ä"}'
    ).encode()
    # Cross-checked with coreutils `sha256sum` over the literal string above.
    assert canonical_hash(obj) == "3536e0a479d21a0431e2f15ee657da5b61cb8e580729224096b8cd7a51e3f131"
