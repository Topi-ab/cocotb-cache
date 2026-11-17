"""Toolchain identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cocotb_cache import identity
from cocotb_cache.identity import IdentityError, dist_content_hash, first_paragraph


# --- version text ---------------------------------------------------------------


def test_first_paragraph() -> None:
    text = "\n\nGHDL 6.0.0\n Compiled with GNAT\n llvm 18\n\nCopyright (C) 2003\nlicense...\n"
    assert first_paragraph(text) == "GHDL 6.0.0\n Compiled with GNAT\n llvm 18"
    assert first_paragraph("") == ""


def test_missing_tool_is_an_identity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    identity._tool_version.cache_clear()
    monkeypatch.setitem(identity.SIMULATOR_COMMANDS, "Fake", (("definitely-not-a-simulator-xyz", "--version"),))
    with pytest.raises(IdentityError, match="cannot run"):
        identity.simulator_identity("Fake")


def test_failing_tool_is_an_identity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    identity._tool_version.cache_clear()
    monkeypatch.setitem(identity.SIMULATOR_COMMANDS, "Fake", (("false",),))
    with pytest.raises(IdentityError, match="no version"):
        identity.simulator_identity("Fake")


def test_unknown_runner() -> None:
    with pytest.raises(IdentityError, match="no identity commands"):
        identity.simulator_identity("Questa")


@pytest.mark.parametrize(
    "runner, expected",
    [
        ("Icarus", ["Icarus Verilog version 13.0", "Icarus Verilog runtime version 13.0"]),
        ("Verilator", ["Verilator 5.054", "g++", "GNU Make"]),
        ("Ghdl", ["GHDL 6.0.0", "llvm", "gcc"]),
        ("Nvc", ["nvc 1.23.1"]),
    ],
)
def test_real_simulator_identities(runner: str, expected: list[str]) -> None:
    text = identity.simulator_identity(runner)
    for fragment in expected:
        assert fragment in text
    assert identity.simulator_identity(runner) == text  # stable


# --- distribution content hash -----------------------------------------------------


class FakeFile:
    def __init__(self, base: Path, rel: str) -> None:
        self._base, self._rel = base, Path(rel)
        self.parts = self._rel.parts
        self.suffix = self._rel.suffix

    def locate(self) -> Path:
        return self._base / self._rel

    def as_posix(self) -> str:
        return self._rel.as_posix()


class FakeDist:
    def __init__(self, base: Path, rels: list[str], direct_url: str | None = None) -> None:
        self.files = [FakeFile(base, r) for r in rels]
        self._direct_url = direct_url
        self.metadata = {"Name": "fake"}

    def read_text(self, name: str) -> str | None:
        return self._direct_url if name == "direct_url.json" else None


def make_install(base: Path, content: dict[str, bytes]) -> list[str]:
    for rel, data in content.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return list(content)


CONTENT = {
    "fake/__init__.py": b"x = 1\n",
    "fake/libs/libgpi.so": b"\x7fELF...",
    "fake-1.0.dist-info/METADATA": b"Name: fake\n",
    "fake-1.0.dist-info/RECORD": b"per-install hashes",
    "fake-1.0.dist-info/INSTALLER": b"pip",
    "fake/__pycache__/__init__.cpython-312.pyc": b"bytecode",
    "../../bin/fake-config": b"#!/some/venv/bin/python",
}


def test_same_wheel_same_hash_anywhere(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "site", tmp_path / "b" / "site"
    ra = make_install(a, CONTENT)
    other = dict(CONTENT)
    other["fake-1.0.dist-info/INSTALLER"] = b"uv"  # installer-specific
    other["fake-1.0.dist-info/RECORD"] = b"different"
    other["fake/__pycache__/__init__.cpython-312.pyc"] = b"other bytecode"
    other["../../bin/fake-config"] = b"#!/another/venv/bin/python"
    rb = make_install(b, other)
    assert dist_content_hash(FakeDist(a, ra)) == dist_content_hash(FakeDist(b, rb))


def test_modified_install_changes_hash(tmp_path: Path) -> None:
    base = tmp_path / "site"
    rels = make_install(base, CONTENT)
    before = dist_content_hash(FakeDist(base, rels))
    (base / "fake" / "__init__.py").write_bytes(b"x = 2\n")
    assert dist_content_hash(FakeDist(base, rels)) != before


def test_empty_distribution_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(IdentityError):
        dist_content_hash(FakeDist(tmp_path, []))


def test_editable_detection() -> None:
    assert identity._is_editable(FakeDist(Path("."), [], json.dumps({"url": "file:///x", "dir_info": {"editable": True}})))
    assert not identity._is_editable(FakeDist(Path("."), [], json.dumps({"url": "file:///x", "dir_info": {}})))
    assert not identity._is_editable(FakeDist(Path("."), [], None))
    assert not identity._is_editable(FakeDist(Path("."), [], "not json"))


def test_editable_install_hashes_source_tree() -> None:
    # This project itself is installed editable in the dev venv.
    from importlib import metadata

    dist = metadata.distribution("cocotb-cache")
    assert identity._is_editable(dist)
    entries = identity._editable_entries(dist)
    assert any(path == "cocotb_cache/identity.py" for path, _ in entries)


# --- the whole toolchain -----------------------------------------------------------


def test_toolchain_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata

    import cocotb_cache

    monkeypatch.setenv("COCOTB_CACHE_IMAGE", "cocotb-sims:abc")
    tc = identity.toolchain("Icarus")
    assert tc.python == f"cpython-{sys.version.split()[0]}"
    assert tc.platform.startswith("linux-x86_64-glibc")
    assert tc.cocotb_version == metadata.version("cocotb")
    assert len(tc.cocotb_dist) == 64
    assert tc.shim == cocotb_cache.__version__
    assert tc.image == "cocotb-sims:abc"
    assert "Icarus Verilog" in tc.simulator


def test_image_absent_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COCOTB_CACHE_IMAGE", raising=False)
    assert identity.toolchain("Nvc").image is None


def test_container_sets_content_based_image_id() -> None:
    # COCOTB_CACHE_* is cleared for tests (conftest.no_real_s3); read the source.
    image = "cocotb-sims:" + Path("/etc/cocotb-cache-image-id").read_text().strip()
    assert image.startswith("cocotb-sims:") and len(image.split(":", 1)[1]) == 64


def test_identity_is_stable_across_processes_and_differs_per_simulator() -> None:
    code = (
        "import json; from cocotb_cache.identity import toolchain\n"
        "print(json.dumps({r: toolchain(r).to_json() for r in ('Icarus', 'Ghdl')}))"
    )
    runs = [
        json.loads(subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout)
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
    assert runs[0]["Icarus"] != runs[0]["Ghdl"]
    assert runs[0]["Icarus"]["cocotb"] == runs[0]["Ghdl"]["cocotb"]
