"""Unit tests for cocotb_cache.manifest (docs/format.md)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from cocotb_cache.hashing import canonical_hash, sha256_bytes
from cocotb_cache.manifest import (
    SCHEMA_RESULT,
    SCHEMA_SRC,
    InputFile,
    ManifestError,
    Outcome,
    OutputDir,
    OutputFile,
    ResultManifest,
    SrcManifest,
    Toolchain,
    check_relpath,
)

D1 = sha256_bytes(b"one")
D2 = sha256_bytes(b"two")
D3 = sha256_bytes(b"three")

TOOLCHAIN = Toolchain(python="3.12.3", platform="linux-x86_64-glibc2.39", cocotb_version="2.1.0", cocotb_dist=D3, simulator="Icarus 13.0", shim="0.1.0")


def src(**kw) -> SrcManifest:
    base = dict(
        phase="build",
        runner="Icarus",
        cmds=[["iverilog", "-o", "${ROOT}/b/sim.vvp", "${ROOT}/rtl/a.sv"]],
        cwd="${ROOT}/b",
        env={"COCOTB_TOPLEVEL": "a"},
        inputs=[InputFile("rtl/b.sv", D2, 2), InputFile("rtl/a.sv", D1, 1)],
        toolchain=TOOLCHAIN,
    )
    base.update(kw)
    return SrcManifest(**base)


def result(**kw) -> ResultManifest:
    base = dict(
        src=D1,
        phase="test",
        outputs=[
            OutputFile("test_dir", "results.xml", D2, 10),
            OutputFile("build_dir", "sim.vvp", D3, 99, executable=True),
        ],
        dirs=[OutputDir("build_dir", "work")],
        console=D1,
        outcome=Outcome(ok=False, exception="RuntimeError", message="Command failed with return code: 1"),
        info={"duration_s": 1.5},
    )
    base.update(kw)
    return ResultManifest(**base)


# --- SRC ------------------------------------------------------------------------


def test_src_roundtrip_json_and_bytes() -> None:
    m = src(phase="test", build=D3)
    assert SrcManifest.from_json(m.to_json()) == m
    assert SrcManifest.from_bytes(m.to_bytes()) == m
    assert m.to_json()["schema"] == SCHEMA_SRC


def test_src_key_is_hash_of_canonical_bytes() -> None:
    m = src()
    assert m.key() == sha256_bytes(m.to_bytes()) == canonical_hash(m.to_json())


def test_src_key_ignores_input_order() -> None:
    a = src(inputs=[InputFile("x", D1, 1), InputFile("y", D2, 2)])
    b = src(inputs=[InputFile("y", D2, 2), InputFile("x", D1, 1)])
    assert a.key() == b.key()
    assert [f.path for f in a.inputs] == ["x", "y"]


def test_src_key_ignores_container_types() -> None:
    assert src(cmds=[["a", "b"]]).key() == src(cmds=(("a", "b"),)).key()


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "test", "build": D1},
        {"runner": "Ghdl"},
        {"cmds": [["iverilog", "-DX=1"]]},
        {"cmds": [["iverilog"], ["vvp"]]},
        {"cwd": "${ROOT}/c"},
        {"env": {"COCOTB_TOPLEVEL": "b"}},
        {"env": {}},
        {"inputs": [InputFile("rtl/a.sv", D2, 1)]},
        {"inputs": [InputFile("rtl/a.sv", D1, 1, executable=True)]},
        {"toolchain": dataclasses.replace(TOOLCHAIN, simulator="Icarus 12.0")},
        {"toolchain": dataclasses.replace(TOOLCHAIN, platform="linux-aarch64-glibc2.39")},
        {"toolchain": dataclasses.replace(TOOLCHAIN, cocotb_dist=D1)},
        {"toolchain": dataclasses.replace(TOOLCHAIN, shim="0.2.0")},
        {"toolchain": dataclasses.replace(TOOLCHAIN, image="sha256:abc")},
        {"cache_inputs": ["csrc/*.cpp"]},
        {"cache_inputs": ["b", "a"]},  # differs from no patterns at all
        {"cmdfiles": [D1]},
        {"cache_env_whitelist": ["MY_TB_*"]},
        {"cmdfiles": [D1, D2]},
    ],
)
def test_src_key_changes_with_every_field(change: dict) -> None:
    assert src(**change).key() != src().key()


def test_test_phase_build_link_is_part_of_key() -> None:
    assert src(phase="test", build=D1).key() != src(phase="test", build=D2).key()


def test_toolchain_image_absent_not_null() -> None:
    assert "image" not in TOOLCHAIN.to_json()
    assert dataclasses.replace(TOOLCHAIN, image="img").to_json()["image"] == "img"


def test_src_blobs() -> None:
    assert src().blobs() == {D1, D2}


def test_src_golden_key() -> None:
    # Pins the SRC format; a change here changes every cache key. Bump the
    # schema version (docs/format.md) instead of editing this value.
    m = SrcManifest(
        phase="build",
        runner="Icarus",
        cmds=[["iverilog", "${ROOT}/a.sv"]],
        cwd="${ROOT}",
        env={"COCOTB_TOPLEVEL": "a"},
        inputs=[InputFile("a.sv", D1, 3)],
        toolchain=TOOLCHAIN,
    )
    assert m.to_bytes() == (
        '{"cache_env_whitelist":[],"cache_inputs":[],"cmdfiles":[],"cmds":[["iverilog","${ROOT}/a.sv"]],"cwd":"${ROOT}","env":{"COCOTB_TOPLEVEL":"a"},'
        '"inputs":[{"executable":false,"path":"a.sv","sha256":"' + D1 + '","size":3}],'
        '"phase":"build","runner":"Icarus","schema":"cocotb-cache/src/v1",'
        '"toolchain":{"cocotb":{"dist":"' + D3 + '","version":"2.1.0"},'
        '"platform":"linux-x86_64-glibc2.39","python":"3.12.3","shim":"0.1.0","simulator":"Icarus 13.0"}}'
    ).encode()


@pytest.mark.parametrize(
    "kw, match",
    [
        ({"phase": "elab"}, "phase"),
        ({"phase": "test"}, "build"),
        ({"build": D1}, "only a test-phase"),
        ({"phase": "test", "build": "nothex"}, "build"),
        ({"cmds": [[]]}, "empty command"),
        ({"cmds": [["a", 1]]}, "cmds"),
        ({"env": {"A": 1}}, "env"),
        ({"inputs": [InputFile("a", D1, 1), InputFile("a", D2, 2)]}, "duplicate"),
    ],
)
def test_src_validation(kw: dict, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        src(**kw)


@pytest.mark.parametrize("bad", ["/abs", "a/../b", "..", "a//b", "./a", "a/", "a\\b", "", "a\0b"])
def test_relpath_rejected(bad: str) -> None:
    with pytest.raises(ManifestError):
        check_relpath(bad, what="t")


@pytest.mark.parametrize("bad", [{"path": "x", "sha256": "z" * 64, "size": 1}, {"path": "x", "sha256": D1, "size": -1}])
def test_input_file_validation(bad: dict) -> None:
    with pytest.raises(ManifestError):
        InputFile(**bad)


def test_src_from_json_rejects_unknown_and_wrong_schema() -> None:
    obj = src().to_json()
    with pytest.raises(ManifestError, match="unknown"):
        SrcManifest.from_json({**obj, "extra": 1})
    with pytest.raises(ManifestError, match="schema"):
        SrcManifest.from_json({**obj, "schema": "cocotb-cache/src/v2"})
    with pytest.raises(ManifestError, match="missing"):
        SrcManifest.from_json({k: v for k, v in obj.items() if k != "cwd"})
    with pytest.raises(ManifestError, match="JSON"):
        SrcManifest.from_bytes(b"\xff not json")


# --- RESULT ---------------------------------------------------------------------


def test_result_roundtrip_and_canonical_order() -> None:
    m = result()
    assert ResultManifest.from_bytes(m.to_bytes()) == m
    assert [(f.base, f.path) for f in m.outputs] == [("build_dir", "sim.vvp"), ("test_dir", "results.xml")]
    j = m.to_json()
    assert j["schema"] == SCHEMA_RESULT
    assert json.loads(m.to_bytes()) == j


def test_result_blobs_include_console() -> None:
    assert result().blobs() == {D1, D2, D3}
    assert result(console=None).blobs() == {D2, D3}


def test_outcome_forms() -> None:
    assert Outcome(ok=True).to_json() == {"ok": True}
    with pytest.raises(ManifestError):
        Outcome(ok=True, exception="X", message="y")
    with pytest.raises(ManifestError):
        Outcome(ok=False)


def test_log_file_entry_has_empty_path() -> None:
    OutputFile("log_file", "", D1, 1)
    with pytest.raises(ManifestError):
        OutputFile("log_file", "x.log", D1, 1)
    with pytest.raises(ManifestError):
        OutputFile("test_dir", "", D1, 1)
    with pytest.raises(ManifestError):
        OutputDir("log_file", "x")


@pytest.mark.parametrize(
    "kw",
    [
        {"src": "nothex"},
        {"phase": "x"},
        {"console": "nothex"},
        {"outputs": [OutputFile("test_dir", "a", D1, 1), OutputFile("test_dir", "a", D2, 2)]},
        {"dirs": [OutputDir("test_dir", "a"), OutputDir("test_dir", "a")]},
        {"info": {"x": float("nan")}},
    ],
)
def test_result_validation(kw: dict) -> None:
    with pytest.raises((ManifestError, ValueError)):
        result(**kw)


def test_output_base_validated() -> None:
    with pytest.raises(ManifestError, match="base"):
        OutputFile("elsewhere", "a", D1, 1)


def test_cache_inputs_order_and_duplicates_do_not_change_key() -> None:
    a = src(cache_inputs=["x", "y"])
    b = src(cache_inputs=["y", "x", "y"])
    assert a.cache_inputs == b.cache_inputs == ("x", "y")
    assert a.key() == b.key()
    assert a.key() != src(cache_inputs=["x"]).key()
    assert SrcManifest.from_bytes(a.to_bytes()) == a


def test_cache_inputs_only_in_build_phase() -> None:
    assert "cache_inputs" not in src(phase="test", build=D1).to_json()
    with pytest.raises(ManifestError, match="cache_inputs"):
        src(phase="test", build=D1, cache_inputs=["x"])
    obj = src().to_json()
    del obj["cache_inputs"]
    with pytest.raises(ManifestError, match="cache_inputs"):
        SrcManifest.from_json(obj)


def test_cache_env_whitelist_canonical_and_build_only() -> None:
    a, b = src(cache_env_whitelist=["B", "A"]), src(cache_env_whitelist=["A", "B", "A"])
    assert a.cache_env_whitelist == ("A", "B") and a.key() == b.key()
    assert SrcManifest.from_bytes(a.to_bytes()) == a
    assert "cache_env_whitelist" not in src(phase="test", build=D1).to_json()
    with pytest.raises(ManifestError, match="cache_env_whitelist"):
        src(phase="test", build=D1, cache_env_whitelist=["X"])
