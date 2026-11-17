"""The cache end to end: miss, hit and bypass.

See cache_harness.py: every test runs a user-style runner in a subprocess
with the cache on, against this worker's local versitygw and its own key
prefix, on a private copy of a test design.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from cache_harness import Project, Run, ok  # noqa: F401
from s3_local import S3Endpoint

pytestmark = [pytest.mark.sim, pytest.mark.s3]

@pytest.fixture
def project(tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str):
    def make(design: str = "counter") -> Project:
        return Project(tmp_path, design, local_s3, s3_prefix)

    return make


# --- miss, then hit, then a partial hit, on every simulator -----------------------------


@pytest.mark.parametrize("sim", ["icarus", "verilator", "ghdl", "nvc"])
def test_miss_then_hit(project, sim: str) -> None:
    p = project("counter")
    first = ok(p.run(sim))
    assert first.phase_events("build") == ["miss", "stored"]
    assert first.phase_events("test") == ["miss", "stored"]
    assert first.summary == (1, 0)

    second = ok(p.run(sim))  # fresh build_dir and test_dir
    assert second.phase_events("build") == ["hit"]
    assert second.phase_events("test") == ["hit"]
    assert second.summary == first.summary
    assert second.results_xml == first.results_xml  # restored, not re-simulated
    assert "PASS=1" in second.stdout  # the original console output is replayed

    # Same build, other seed: the restored build_dir must really work.
    third = ok(p.run(sim, seed=2))
    assert third.phase_events("build") == ["hit"]
    assert third.phase_events("test") == ["miss", "stored"]
    assert third.summary == (1, 0)


# --- what changes the key -----------------------------------------------------------------


def test_hdl_change_misses_build_and_test(project) -> None:
    p = project("counter")
    ok(p.run("icarus"))
    sv = p.design_dir / "counter.sv"
    sv.write_text(sv.read_text() + "\n// comment changes the content\n")
    run = ok(p.run("icarus"))
    assert run.phase_events("build") == ["miss", "stored"]
    assert run.phase_events("test") == ["miss", "stored"]


def test_testbench_change_misses_only_test(project) -> None:
    p = project("counter")
    ok(p.run("icarus"))
    tb = p.design_dir / "tb_counter.py"
    tb.write_text(tb.read_text() + "\n# testbench change\n")
    run = ok(p.run("icarus"))
    assert run.phase_events("build") == ["hit"]
    assert run.phase_events("test") == ["miss", "stored"]


def test_parameter_change_misses(project) -> None:
    p = project("adder")
    ok(p.run("icarus", extra_env={"ADDER_WIDTH": "8"}))
    run = ok(p.run("icarus", extra_env={"ADDER_WIDTH": "8"}))
    assert run.phase_events("test") == ["hit"]
    run = ok(p.run("icarus", extra_env={"ADDER_WIDTH": "9"}))
    assert run.phase_events("build") == ["hit"] and run.phase_events("test") == ["miss", "stored"]


def test_cache_inputs_change_misses_build(project) -> None:
    p = project("counter")
    data = p.root / "data" / "mem.hex"
    data.parent.mkdir()
    data.write_text("00\n")
    kw = {"cache_inputs": ["data/*.hex"]}
    ok(p.run("icarus", build_kw=kw))
    assert ok(p.run("icarus", build_kw=kw)).phase_events("build") == ["hit"]
    data.write_text("01\n")
    assert ok(p.run("icarus", build_kw=kw)).phase_events("build") == ["miss", "stored"]


def test_whitelisted_variable_is_in_the_key_others_are_not(project) -> None:
    p = project("counter")
    kw = {"cache_env_whitelist": ["MY_TB_MODE"]}
    ok(p.run("icarus", build_kw=kw, env={"MY_TB_MODE": "a", "UNDECLARED": "1"}))
    run = ok(p.run("icarus", build_kw=kw, env={"MY_TB_MODE": "a", "UNDECLARED": "2"}))
    assert run.phase_events("test") == ["hit"]  # undeclared: not seen, not in the key
    run = ok(p.run("icarus", build_kw=kw, env={"MY_TB_MODE": "b", "UNDECLARED": "2"}))
    assert run.phase_events("build") == ["miss", "stored"]


# --- failures, modes and bypasses -----------------------------------------------------------


def test_failing_test_result_is_cached(project) -> None:
    p = project("failing")
    first = ok(p.run("icarus"))
    assert first.summary == (2, 1)
    second = ok(p.run("icarus"))
    assert second.phase_events("test") == ["hit"]
    assert second.summary == (2, 1) and second.results_xml == first.results_xml


def test_failing_build_is_cached_and_fails_again(project) -> None:
    p = project("counter")
    sv = p.design_dir / "counter.sv"
    sv.write_text(sv.read_text() + "\nthis is not verilog\n")
    first = p.run("icarus")
    assert first.returncode != 0 and first.phase_events("build") == ["miss", "stored"]
    second = p.run("icarus")
    assert second.returncode != 0 and second.phase_events("build") == ["hit"]
    assert "Command failed with return code" in second.stderr


def test_without_seed_the_test_phase_is_not_cached(project) -> None:
    p = project("counter")
    run = ok(p.run("icarus", seed=None))
    assert run.phase_events("build") == ["miss", "stored"]
    assert run.phase_events("test") == ["bypass"]
    assert "random seed" in next(e["reason"] for e in run.events if e.get("event") == "bypass")


def test_read_mode_uses_but_never_writes(project) -> None:
    p = project("counter")
    run = ok(p.run("icarus", mode="read"))
    assert run.phase_events("build") == ["miss"] and p.stored_keys() == []
    ok(p.run("icarus", mode="rw"))
    run = ok(p.run("icarus", mode="read"))
    assert run.phase_events("build") == ["hit"] and run.phase_events("test") == ["hit"]


def test_unreachable_s3_runs_normally(project) -> None:
    p = project("counter")
    run = ok(p.run("icarus", env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9", "AWS_MAX_ATTEMPTS": "1"}))
    assert run.summary == (1, 0)
    assert "S3 lookup failed" in run.stderr + run.stdout


def test_corrupt_blob_falls_back_to_running(project) -> None:
    p = project("counter")
    ok(p.run("icarus"))
    s3 = p.local_s3.client()
    for key in p.stored_keys():
        if "/blobs/" in key:
            s3.put_object(Bucket=p.local_s3.bucket, Key=p.prefix + key, Body=gzip.compress(b"garbage"))
    run = ok(p.run("icarus"))
    assert "restore-failed" in run.phase_events("build")
    assert run.summary == (1, 0)


def test_stored_objects_layout_and_no_secrets(project) -> None:
    p = project("counter")
    ok(p.run("icarus"))
    keys = p.stored_keys()
    assert any(k.startswith("v1/blobs/") and k.endswith(".gz") for k in keys)
    srcs = [k for k in keys if k.startswith("v1/src/")]
    results = [k for k in keys if k.startswith("v1/result/")]
    assert len(srcs) == 2 and len(results) == 2  # build + test
    s3 = p.local_s3.client()
    for key in srcs + results:
        body = s3.get_object(Bucket=p.local_s3.bucket, Key=p.prefix + key)["Body"].read().decode()
        json.loads(body)  # plain JSON
        assert p.local_s3.secret_key not in body and "AWS_" not in body
        assert str(p.tmp) not in body  # per-run paths are normalised away
        # Regressions: only the venv maps to ${VENV} (not /usr), the
        # image's own variable stays out, no build_dir paths among the inputs.
        assert "${VENV}/lib/x86_64-linux-gnu" not in body
        assert "COCOTB_CACHE_IMAGE" not in body and "COCOTB_IMAGE" not in body
        manifest = json.loads(body)
        assert all("/build/" not in f["path"] for f in manifest.get("inputs", []))


# --- cache_fix_seed ----------------------------------------------------------

SEED_TB = '''
import os, random
import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def seed_view(dut):
    await Timer(1, unit="ns")
    print("SEEDVIEW seed=%s hashseed=%s hash=%d rand=%r" % (
        os.environ.get("COCOTB_RANDOM_SEED"), os.environ.get("PYTHONHASHSEED"), hash("cocotb-cache"), random.random()))
'''


def _seed_view(run: Run) -> str:
    return next(line for line in run.stdout.splitlines() if line.startswith("SEEDVIEW"))


def _seed_project(project) -> Project:
    p = project("counter")
    (p.design_dir / "tb_seed.py").write_text(SEED_TB)
    return p


def test_fix_seed_makes_a_seedless_test_cacheable(project) -> None:
    p = _seed_project(project)
    kw = {"seed": None, "build_kw": {"cache_fix_seed": True}, "test_module": "tb_seed"}
    first = ok(p.run("icarus", **kw))
    assert first.phase_events("test") == ["miss", "stored"]
    second = ok(p.run("icarus", **kw))
    assert second.phase_events("test") == ["hit"]
    assert _seed_view(second) == _seed_view(first)  # replayed console


def test_fixed_seed_and_hashseed_are_reproducible(project) -> None:
    # Two real simulations (read mode stores nothing): same seed, same
    # PYTHONHASHSEED, so the same random and hash values.
    p = _seed_project(project)
    kw = {"seed": None, "build_kw": {"cache_fix_seed": True}, "test_module": "tb_seed", "mode": "read"}
    a, b = ok(p.run("icarus", **kw)), ok(p.run("icarus", **kw))
    assert a.phase_events("test") == ["miss"] and b.phase_events("test") == ["miss"]
    view = _seed_view(a)
    assert view == _seed_view(b)
    assert "seed=None" not in view and "hashseed=None" not in view


def test_user_values_win_over_fixing(project) -> None:
    p = _seed_project(project)
    kw = {"seed": 5, "build_kw": {"cache_fix_seed": True}, "test_module": "tb_seed", "mode": "read"}
    view = _seed_view(ok(p.run("icarus", **kw)))
    assert "seed=5 " in view and "hashseed=None" not in view  # hash seed still fixed
    view = _seed_view(ok(p.run("icarus", env={"PYTHONHASHSEED": "0"}, **kw)))
    assert "seed=5 " in view and "hashseed=0 " in view


def test_fix_seed_with_cache_off_is_accepted(project) -> None:
    p = _seed_project(project)
    run = ok(p.run("icarus", seed=None, mode="off", build_kw={"cache_fix_seed": True}, test_module="tb_seed"))
    assert "seed=" in _seed_view(run)  # cocotb's own random seed
