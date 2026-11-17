"""cocotb-cache CLI: inspect, stat, gc."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cocotb_cache import cli
from cocotb_cache.hashing import sha256_bytes
from cocotb_cache.manifest import InputFile, Outcome, OutputFile, ResultManifest, SrcManifest, Toolchain
from cocotb_cache.store import Store
from s3_local import S3Endpoint

pytestmark = pytest.mark.s3


@pytest.fixture
def env(local_s3: S3Endpoint, s3_prefix: str, monkeypatch: pytest.MonkeyPatch) -> Store:
    for name, value in {
        "AWS_ENDPOINT_URL": local_s3.url,
        "AWS_ACCESS_KEY_ID": local_s3.access_key,
        "AWS_SECRET_ACCESS_KEY": local_s3.secret_key,
        "AWS_DEFAULT_REGION": local_s3.region,
        "COCOTB_CACHE_S3_BUCKET": local_s3.bucket,
        "COCOTB_CACHE_S3_PREFIX": s3_prefix,
    }.items():
        monkeypatch.setenv(name, value)
    return Store(local_s3.client(), local_s3.bucket, s3_prefix)


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    rc = cli.main(list(argv), out)
    return rc, out.getvalue()


def make_run(store: Store, n: int, *, with_result: bool = True) -> tuple[str, set[str]]:
    """Store one build run like the cache does; return (key, referenced blob digests)."""
    output, _ = store.put_bytes(f"build output {n}".encode())
    console, _ = store.put_bytes(f"console {n}".encode())
    tc = Toolchain(python="cpython-3.12.3", platform="linux-x86_64-glibc2.39", cocotb_version="2.1.0",
                   cocotb_dist=sha256_bytes(b"dist"), simulator="Icarus Verilog version 13.0", shim="0.1.0")
    src = SrcManifest(phase="build", runner="Icarus", cmds=[["iverilog", f"-DN={n}", "${ROOT}/a.sv"]], cwd="${BUILD_DIR}",
                      env={}, inputs=[InputFile("a.sv", sha256_bytes(b"a"), 1)], toolchain=tc)
    key, _ = store.put_src(src)
    if with_result:
        store.put_result(ResultManifest(src=key, phase="build", outputs=[OutputFile("build_dir", "sim.vvp", output, 14)],
                                        console=console, outcome=Outcome(ok=True), info={"duration_s": 1.0}))
    return key, {output, console}


def test_inspect_by_prefix(env: Store) -> None:
    key, _ = make_run(env, 1)
    rc, text = run_cli("inspect", key[:8])
    assert rc == 0
    assert key in text and "phase    build" in text and "runner   Icarus" in text
    assert "outcome  ok" in text and "build_dir:sim.vvp" in text and "iverilog -DN=1" in text


def test_inspect_json(env: Store) -> None:
    key, _ = make_run(env, 2)
    rc, text = run_cli("inspect", key, "--json")
    data = json.loads(text)
    assert rc == 0 and data["key"] == key and data["src"]["phase"] == "build" and data["result"]["outcome"]["ok"]


def test_inspect_errors(env: Store) -> None:
    with pytest.raises(SystemExit, match="at least"):
        run_cli("inspect", "ab")
    with pytest.raises(SystemExit, match="no manifest"):
        run_cli("inspect", "0000000000")


def test_inspect_ambiguous_prefix() -> None:
    from cocotb_cache.store import StoredObject

    now = datetime(2026, 11, 17, tzinfo=timezone.utc)

    class FakeStore:
        def list_objects(self, kind, start=""):
            for digest in ("abcd" + "1" * 60, "abcd" + "2" * 60):
                if digest.startswith(start):
                    yield StoredObject(kind, digest, f"v1/{kind}/{digest}", 1, now)

    with pytest.raises(SystemExit, match="ambiguous"):
        cli._resolve_key(FakeStore(), "abcd")
    assert cli._resolve_key(FakeStore(), "abcd1") == "abcd" + "1" * 60


def test_stat(env: Store) -> None:
    make_run(env, 3)
    rc, text = run_cli("stat")
    assert rc == 0
    lines = {line.split()[0]: line.split()[1] for line in text.splitlines()[1:]}
    assert lines == {"blobs": "2", "src": "1", "result": "1", "total": "4"}


def test_gc_keeps_referenced_and_recent_objects(env: Store) -> None:
    make_run(env, 4)
    env.put_bytes(b"orphan blob")  # not referenced by anything
    make_run(env, 5, with_result=False)  # incomplete run: src + its blobs, no result
    rc, text = run_cli("gc")  # default min-age 24 h: everything is too new
    assert rc == 0 and "garbage  0 objects" in text


def test_gc_dry_run_then_delete(env: Store) -> None:
    kept_key, kept_blobs = make_run(env, 6)
    orphan, _ = env.put_bytes(b"orphan blob")
    incomplete_key, incomplete_blobs = make_run(env, 7, with_result=False)
    rc, text = run_cli("gc", "--min-age", "0")
    assert rc == 0 and "dry run" in text
    assert {o.digest for o in env.list_objects("blobs")} >= {orphan, *kept_blobs}  # nothing deleted yet
    rc, text = run_cli("gc", "--min-age", "0", "--delete")
    assert rc == 0 and "deleted" in text
    blobs = {o.digest for o in env.list_objects("blobs")}
    assert blobs == kept_blobs
    assert {o.digest for o in env.list_objects("src")} == {kept_key}
    assert env.get_result(kept_key) is not None


def test_gc_window_protects_running_uploads() -> None:
    # An unreferenced blob newer than the window is kept (a run may be
    # between uploading its blobs and its RESULT).
    from cocotb_cache.store import StoredObject

    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)

    class FakeStore:
        def list_objects(self, kind):
            if kind == "blobs":
                yield StoredObject("blobs", "a" * 64, "k1", 1, now - timedelta(hours=1))
                yield StoredObject("blobs", "b" * 64, "k2", 1, now - timedelta(hours=48))

        def get_result(self, digest):
            return None

        def get_src(self, digest):
            return None

    garbage, _ = cli.find_garbage(FakeStore(), timedelta(hours=24), now=now)
    assert [o.digest for o in garbage] == ["b" * 64]


def test_delete_refuses_keys_outside_the_store(env: Store) -> None:
    with pytest.raises(ValueError):
        env.delete_key("someone-elses/object")


def test_console_script_installed(env: Store) -> None:
    script = Path(sys.executable).parent / "cocotb-cache"
    res = subprocess.run([str(script), "--version"], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.startswith("cocotb-cache ")


def test_errors_are_reported_not_raised(env: Store, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("COCOTB_CACHE_S3_BUCKET", "no-such-bucket-here")
    rc, _ = run_cli("stat")
    assert rc == 1 and "NoSuchBucket" in capsys.readouterr().err
    monkeypatch.setenv("COCOTB_CACHE_S3_BUCKET", env.bucket)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wrong")
    rc, _ = run_cli("stat")
    assert rc == 1 and "S3 error 403" in capsys.readouterr().err
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("COCOTB_CACHE_S3_MAX_ATTEMPTS", "1")
    rc, _ = run_cli("stat")
    assert rc == 1 and "check AWS_ENDPOINT_URL" in capsys.readouterr().err
