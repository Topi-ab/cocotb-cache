"""Contract between the shim and cocotb's runner.

The shim relies on one property of ``cocotb_tools.runner``: every ``Runner.build()`` and ``Runner.test()`` call reaches
``Runner._execute()`` exactly once, with the final command lines, and the
supported simulator classes inherit all three methods unchanged. These tests
pin that property for the installed cocotb, statically and by running every
test design on every simulator. A new cocotb version is supported only after
this file passes against it (shim.SUPPORTED_COCOTB).
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import cocotb_tools.runner as cocotb_runner
from cocotb_cache import shim
from hdl.designs import DESIGNS, SIMULATORS

TESTS_DIR = Path(__file__).resolve().parent


# --- static: the source of the installed cocotb ------------------------------------


def _execute_calls(func) -> int:
    """Number of ``self._execute(...)`` calls in *func*'s source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_execute"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def _original(func):
    # The test process itself runs without the cache, but unwrap defensively.
    return inspect.unwrap(func)


@pytest.mark.parametrize("method", ["build", "test"])
def test_each_phase_calls_execute_once_in_source(method: str) -> None:
    assert _execute_calls(_original(getattr(cocotb_runner.Runner, method))) == 1


def test_execute_signature_matches_the_guard() -> None:
    params = list(inspect.signature(_original(cocotb_runner.Runner._execute)).parameters)
    assert params == ["self", "cmds", "cwd"]


def test_installed_cocotb_is_supported() -> None:
    assert shim._check_supported(cocotb_runner) is None


@pytest.mark.parametrize("name", sorted(shim.SUPPORTED_RUNNERS))
def test_supported_runners_inherit_the_seam(name: str) -> None:
    cls = getattr(cocotb_runner, name)
    assert issubclass(cls, cocotb_runner.Runner)
    for method in ("build", "test", "_execute"):
        assert method not in vars(cls), f"{name} overrides {method}()"


def test_simulator_names_map_to_supported_runners() -> None:
    for sim in SIMULATORS:
        assert type(cocotb_runner.get_runner(sim)).__name__ in shim.SUPPORTED_RUNNERS


def test_no_other_runner_method_calls_execute() -> None:
    # If another public method started to run commands, the cache would not
    # see a phase for it; the shim treats phase-less calls as pass-through,
    # but the contract should be revisited.
    callers = [
        name
        for name, func in vars(cocotb_runner.Runner).items()
        if callable(func) and name not in ("build", "test", "_execute") and _execute_calls(_original(func))
    ]
    assert callers == []


# --- dynamic: every design on every simulator, through the real hook ------------

SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from cocotb_tools.runner import get_runner
    from hdl.designs import DESIGNS, SIMULATORS

    name, sim, tmp = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    d, lang = DESIGNS[name], SIMULATORS[sim]
    build_dir = tmp / "build"
    test_dir = build_dir if sim == "ghdl" else tmp / "test"
    r = get_runner(sim)
    kw = dict(sources=d.sources(lang), includes=d.includes(lang), hdl_toplevel=d.toplevel,
              parameters=d.parameters, build_dir=build_dir, **d.build_kwargs(lang))
    r.build(always=True, **kw)
    r.build(always=False, **kw)  # incremental: may have nothing to do
    r.test(test_module=d.test_module, hdl_toplevel=d.toplevel, parameters=d.parameters,
           build_dir=build_dir, test_dir=test_dir)
    """
)


@pytest.mark.sim
@pytest.mark.parametrize("sim", list(SIMULATORS))
@pytest.mark.parametrize("name", list(DESIGNS))
def test_one_execute_per_phase_call(name: str, sim: str, tmp_path: Path) -> None:
    script = tmp_path / "runner_script.py"
    script.write_text(SCRIPT)
    trace = tmp_path / "trace.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COCOTB_CACHE"))}
    env.update(
        COCOTB_CACHE="rw",
        COCOTB_CACHE_TRACE=str(trace),
        PYTHONPATH=os.pathsep.join([str(TESTS_DIR), str(DESIGNS[name].directory)]),
    )
    res = subprocess.run(
        [sys.executable, str(script), name, sim, str(tmp_path)], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
    records = [r for r in map(json.loads, trace.read_text().splitlines()) if "runner" in r]
    expected_runner = type(cocotb_runner.get_runner(sim)).__name__
    assert [(r["runner"], r["phase"], r["active"]) for r in records] == [
        (expected_runner, "build", True),
        (expected_runner, "build", True),
        (expected_runner, "test", True),
    ]
    assert records[0]["ncmds"] >= 1 and records[2]["ncmds"] >= 1
