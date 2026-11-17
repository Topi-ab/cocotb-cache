"""Python test-module inputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cocotb_cache.pyinputs import collect_python_inputs

HDL = Path(__file__).resolve().parent / "hdl"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = (tmp_path / "proj").resolve()
    r.mkdir()
    return r


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def collect(root: Path, modules, path=(), cwd: Path | None = None):
    return collect_python_inputs(modules, [str(p) for p in path] + sys.path, cwd or root / "build", root=root)


def paths(result) -> list[str]:
    return [f.path for f in result.inputs]


@pytest.fixture(autouse=True)
def build_dir(root: Path) -> None:
    (root / "build").mkdir()


def test_test_module_and_its_local_helpers(root: Path) -> None:
    tb = root / "tb"
    write(tb / "test_top.py", "import cocotb\nimport helpers\nfrom models import alu\n")
    write(tb / "helpers.py", "import os, json\n")
    write(tb / "models" / "__init__.py", "")
    write(tb / "models" / "alu.py", "from . import common\nfrom .regs import REG\n")
    write(tb / "models" / "common.py", "")
    write(tb / "models" / "regs.py", "REG = 1\n")
    write(tb / "unused.py", "")
    r = collect(root, ["test_top"], [tb])
    assert r.ok, r.problems
    assert paths(r) == [
        "tb/helpers.py",
        "tb/models/__init__.py",
        "tb/models/alu.py",
        "tb/models/common.py",
        "tb/models/regs.py",
        "tb/test_top.py",
    ]


def test_several_test_modules_and_dotted_names(root: Path) -> None:
    tb = root / "tb"
    write(tb / "suite" / "__init__.py")
    write(tb / "suite" / "test_a.py", "")
    write(tb / "test_b.py", "")
    r = collect(root, ["suite.test_a", " test_b "], [tb])
    assert paths(r) == ["tb/suite/__init__.py", "tb/suite/test_a.py", "tb/test_b.py"]


def test_import_forms(root: Path) -> None:
    tb = root / "tb"
    write(
        tb / "test_x.py",
        "import pkg.deep.mod as m\n"
        "from pkg import sub\n"
        "def later():\n    import lazy\n"
        "try:\n    import optional_dependency_that_does_not_exist\nexcept ImportError:\n    pass\n",
    )
    write(tb / "pkg" / "__init__.py")
    write(tb / "pkg" / "deep" / "__init__.py")
    write(tb / "pkg" / "deep" / "mod.py")
    write(tb / "pkg" / "sub.py")
    write(tb / "lazy.py")
    r = collect(root, ["test_x"], [tb])
    assert r.ok, r.problems
    assert paths(r) == [
        "tb/lazy.py",
        "tb/pkg/__init__.py",
        "tb/pkg/deep/__init__.py",
        "tb/pkg/deep/mod.py",
        "tb/pkg/sub.py",
        "tb/test_x.py",
    ]


def test_cwd_is_searched_first(root: Path) -> None:
    # The simulator runs in test_dir; a module there wins over PYTHONPATH.
    write(root / "build" / "helpers.py", "")
    write(root / "tb" / "helpers.py", "")
    write(root / "tb" / "test_t.py", "import helpers\n")
    r = collect(root, ["test_t"], [root / "tb"])
    assert paths(r) == ["build/helpers.py", "tb/test_t.py"]


def test_path_order_decides_shadowing(root: Path) -> None:
    write(root / "a" / "test_s.py", "")
    write(root / "b" / "test_s.py", "")
    assert paths(collect(root, ["test_s"], [root / "a", root / "b"])) == ["a/test_s.py"]
    assert paths(collect(root, ["test_s"], [root / "b", root / "a"])) == ["b/test_s.py"]


def test_namespace_package(root: Path) -> None:
    write(root / "tb" / "ns" / "test_n.py", "")  # no __init__.py
    assert paths(collect(root, ["ns.test_n"], [root / "tb"])) == ["tb/ns/test_n.py"]


def test_cycles_terminate(root: Path) -> None:
    write(root / "tb" / "a.py", "import b\n")
    write(root / "tb" / "b.py", "import a\n")
    assert paths(collect(root, ["a"], [root / "tb"])) == ["tb/a.py", "tb/b.py"]


def test_stdlib_and_installed_packages_are_toolchain(root: Path) -> None:
    write(root / "tb" / "test_t.py", "import json, cocotb, cocotb.triggers, boto3\n")
    r = collect(root, ["test_t"], [root / "tb"])
    assert r.ok and paths(r) == ["tb/test_t.py"]


def test_missing_test_module_is_a_problem(root: Path) -> None:
    r = collect(root, ["test_nowhere"], [root / "tb"])
    assert not r.ok and "test_nowhere" in r.problems[0]


def test_foreign_module_outside_root_is_a_problem(root: Path, tmp_path: Path) -> None:
    write(tmp_path / "elsewhere" / "shared_models.py")
    write(root / "tb" / "test_t.py", "import shared_models\n")
    r = collect(root, ["test_t"], [root / "tb", tmp_path / "elsewhere"])
    assert not r.ok and "outside the project root" in r.problems[0]


def test_syntax_error_does_not_crash(root: Path) -> None:
    write(root / "tb" / "test_t.py", "def broken(:\n")
    r = collect(root, ["test_t"], [root / "tb"])
    assert r.ok and paths(r) == ["tb/test_t.py"]  # cocotb will report the error itself


def test_hashes(root: Path) -> None:
    write(root / "tb" / "test_t.py", "x = 1\n")
    (entry,) = collect(root, ["test_t"], [root / "tb"]).inputs
    import hashlib

    assert entry.sha256 == hashlib.sha256(b"x = 1\n").hexdigest() and entry.size == 6


# --- static result vs. what the simulator's Python really imports ----------------------

RECORDER = '''
import sys, json, os
import cocotb
from cocotb.triggers import Timer
import helpers
from models import alu

@cocotb.test()
async def records_imports(dut):
    await Timer(1, unit="ns")
    root = os.environ["IMPORT_RECORD_ROOT"]
    loaded = sorted(
        os.path.relpath(m.__file__, root)
        for m in list(sys.modules.values())
        if getattr(m, "__file__", None) and os.path.realpath(m.__file__).startswith(root + os.sep)
        and "/.venv/" not in m.__file__
    )
    with open(os.environ["IMPORT_RECORD_FILE"], "w") as f:
        json.dump(loaded, f)
'''


@pytest.mark.sim
def test_static_inputs_cover_runtime_imports(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cocotb_tools.runner import get_runner

    tb = root / "tb"
    write(tb / "test_rec.py", RECORDER)
    write(tb / "helpers.py", "import models.regs\n")
    write(tb / "models" / "__init__.py", "")
    write(tb / "models" / "alu.py", "from . import common\n")
    write(tb / "models" / "common.py", "")
    write(tb / "models" / "regs.py", "")
    record = tmp_path / "record.json"
    monkeypatch.setenv("IMPORT_RECORD_ROOT", str(root))
    monkeypatch.setenv("IMPORT_RECORD_FILE", str(record))
    monkeypatch.syspath_prepend(str(tb))
    build = root / "build"
    r = get_runner("icarus")
    # The testbench reads these, so they must be declared (the simulator only
    # sees declared variables).
    r.build(sources=[HDL / "counter" / "counter.sv"], hdl_toplevel="counter", build_dir=build, timescale=("1ns", "1ps"),
            cache_env_whitelist=["IMPORT_RECORD_*"])
    r.test(test_module="test_rec", hdl_toplevel="counter", build_dir=build, results_xml=str(tmp_path / "r.xml"))
    runtime = set(json.loads(record.read_text()))
    static = collect_python_inputs(
        r.env["COCOTB_TEST_MODULES"].split(","), r.env["PYTHONPATH"].split(":"), build, root=root
    )
    assert static.ok, static.problems
    assert runtime  # the recorder saw project modules
    assert runtime <= set(paths(static)), runtime - set(paths(static))
