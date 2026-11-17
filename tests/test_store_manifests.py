"""Manifest storage and the conditional RESULT write (docs/format.md)."""

from __future__ import annotations

import threading

import pytest

from cocotb_cache.hashing import canonical_json, sha256_bytes
from cocotb_cache.manifest import (
    InputFile,
    Outcome,
    OutputFile,
    ResultManifest,
    SrcManifest,
    Toolchain,
)
from cocotb_cache.store import JSON_CONTENT_TYPE, ManifestCorrupt, Store
from s3_local import S3Endpoint

pytestmark = pytest.mark.s3

D1 = sha256_bytes(b"one")
D2 = sha256_bytes(b"two")
TOOLCHAIN = Toolchain(python="3.12.3", platform="linux-x86_64-glibc2.39", cocotb_version="2.1.0", cocotb_dist=D2, simulator="sim 1", shim="0.1.0")


@pytest.fixture
def store(local_s3: S3Endpoint, s3_prefix: str) -> Store:
    return Store(local_s3.client(), local_s3.bucket, s3_prefix)


def make_src(n: int = 0) -> SrcManifest:
    return SrcManifest(
        phase="build",
        runner="Icarus",
        cmds=[["iverilog", f"-DN={n}", "${ROOT}/a.sv"]],
        cwd="${ROOT}",
        env={},
        inputs=[InputFile("a.sv", D1, 3)],
        toolchain=TOOLCHAIN,
    )


def make_result(src_key: str, tag: str = "a") -> ResultManifest:
    return ResultManifest(
        src=src_key,
        phase="build",
        outputs=[OutputFile("build_dir", "sim.vvp", D2, 3)],
        outcome=Outcome(ok=True),
        info={"writer": tag},
    )


def _raw(store: Store, key: str) -> dict:
    return store.client.get_object(Bucket=store.bucket, Key=key)


def _plant(store: Store, key: str, body: bytes) -> None:
    store.client.put_object(Bucket=store.bucket, Key=key, Body=body)


# --- SRC --------------------------------------------------------------------------


def test_key_layout(store: Store) -> None:
    assert store.src_key(D1) == f"{store.prefix}v1/src/{D1[:2]}/{D1}.json"
    assert store.result_key(D1) == f"{store.prefix}v1/result/{D1[:2]}/{D1}.json"
    with pytest.raises(ValueError):
        store.src_key("x")


def test_src_roundtrip_and_idempotent(store: Store) -> None:
    m = make_src()
    key, written = store.put_src(m)
    assert key == m.key() and written
    assert store.put_src(m) == (key, False)
    assert store.get_src(key) == m


def test_src_stored_as_uncompressed_canonical_json(store: Store) -> None:
    m = make_src()
    key, _ = store.put_src(m)
    obj = _raw(store, store.src_key(key))
    assert obj["ContentType"] == JSON_CONTENT_TYPE
    assert obj["Body"].read() == m.to_bytes()  # plain JSON, exactly the hashed bytes


def test_src_missing_is_none(store: Store) -> None:
    assert store.get_src(D1) is None


def test_src_content_must_match_key(store: Store) -> None:
    m = make_src()
    # Same manifest, non-canonical bytes: parses fine but is not what the key hashes.
    pretty = canonical_json(m.to_json()).replace(b",", b", ")
    _plant(store, store.src_key(m.key()), pretty)
    with pytest.raises(ManifestCorrupt, match="does not match"):
        store.get_src(m.key())


def test_src_invalid_content_is_corrupt(store: Store) -> None:
    body = b'{"schema":"cocotb-cache/src/v1"}'
    key = sha256_bytes(body)  # hash matches, but it is not a valid manifest
    _plant(store, store.src_key(key), body)
    with pytest.raises(ManifestCorrupt):
        store.get_src(key)


# --- RESULT -----------------------------------------------------------------------


def test_result_roundtrip(store: Store) -> None:
    key = make_src().key()
    r = make_result(key)
    assert store.put_result(r) is True
    assert store.get_result(key) == r
    obj = _raw(store, store.result_key(key))
    assert obj["ContentType"] == JSON_CONTENT_TYPE
    assert obj["Body"].read() == r.to_bytes()


def test_result_missing_is_a_miss(store: Store) -> None:
    assert store.get_result(D1) is None


def test_result_first_writer_wins(store: Store) -> None:
    key = make_src().key()
    assert store.put_result(make_result(key, "first")) is True
    assert store.put_result(make_result(key, "second")) is False
    assert store.get_result(key).info == {"writer": "first"}


def test_result_for_another_key_is_corrupt(store: Store) -> None:
    key, other = make_src(1).key(), make_src(2).key()
    _plant(store, store.result_key(key), make_result(other).to_bytes())
    with pytest.raises(ManifestCorrupt, match="belongs to"):
        store.get_result(key)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"schema":"cocotb-cache/result/v9"}'])
def test_result_invalid_is_corrupt(store: Store, body: bytes) -> None:
    _plant(store, store.result_key(D1), body)
    with pytest.raises(ManifestCorrupt):
        store.get_result(D1)


def test_concurrent_result_writers_exactly_one_wins(local_s3: S3Endpoint, s3_prefix: str) -> None:
    key = make_src().key()
    n = 8
    barrier = threading.Barrier(n)
    outcomes: dict[int, bool] = {}
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        s = Store(local_s3.client(), local_s3.bucket, s3_prefix)  # own client per thread
        try:
            barrier.wait()
            outcomes[i] = s.put_result(make_result(key, f"w{i}"))
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    winners = [i for i, won in outcomes.items() if won]
    assert len(outcomes) == n and len(winners) == 1
    stored = Store(local_s3.client(), local_s3.bucket, s3_prefix).get_result(key)
    assert stored.info == {"writer": f"w{winners[0]}"}
