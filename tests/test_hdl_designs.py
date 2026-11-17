"""Build and test every design in tests/hdl/ on every matching simulator.

This checks the test designs themselves, so that later integration
tests of the cache can rely on them.
"""

from __future__ import annotations

import pytest
from cocotb_tools.runner import get_results, get_runner

from hdl.designs import DESIGNS, SIMULATORS, Design

pytestmark = pytest.mark.sim

# (design name, case id, build/test parameters, extra test env, seed)
_CASES = [
    ("counter", "default", {}, {}, None),
    ("adder", "w4", {"WIDTH": 4}, {"ADDER_WIDTH": "4"}, None),
    ("adder", "w16", {"WIDTH": 16}, {"ADDER_WIDTH": "16"}, None),
    ("include", "default", {}, {}, None),
    ("multifile", "default", {}, {}, None),
    ("failing", "default", {}, {}, None),
    ("random", "seed1", {}, {}, 1),
    ("random", "seed987654", {}, {}, 987654),
]

_PARAMS = [
    pytest.param(sim, name, params, env, seed, id=f"{name}-{case}-{sim}")
    for name, case, params, env, seed in _CASES
    for sim in SIMULATORS
]


def run_design(
    design: Design,
    sim: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    parameters: dict[str, object] | None = None,
    extra_env: dict[str, str] | None = None,
    seed: int | None = None,
):
    """Build and test ``design`` with ``sim``; return (num_tests, num_failed).

    A failing cocotb test makes ``Runner.test()`` call ``sys.exit`` under
    pytest; the SystemExit is caught here and the results file is read anyway.
    """
    lang = SIMULATORS[sim]
    build_dir = tmp_path / "build"
    # GHDL (LLVM backend) runs the elaborated executable from the current
    # directory, so its tests have to run in build_dir.
    test_dir = build_dir if sim == "ghdl" else tmp_path / "test"
    results = tmp_path / "results.xml"
    params = dict(design.parameters if parameters is None else parameters)

    # The simulator's PYTHONPATH is taken from sys.path; monkeypatch restores
    # sys.path after the test, so nothing leaks into other tests.
    monkeypatch.syspath_prepend(str(design.directory))

    runner = get_runner(sim)
    runner.build(
        sources=design.sources(lang),
        includes=design.includes(lang),
        hdl_toplevel=design.toplevel,
        parameters=params,
        build_dir=build_dir,
        always=True,
        **design.build_kwargs(lang),
    )
    try:
        runner.test(
            test_module=design.test_module,
            hdl_toplevel=design.toplevel,
            parameters=params,
            build_dir=build_dir,
            test_dir=test_dir,
            results_xml=str(results),
            extra_env=extra_env or {},
            seed=seed,
        )
    except SystemExit as e:
        if design.num_failed == 0:
            raise AssertionError(f"tests failed unexpectedly (exit {e.code})") from e
    else:
        assert design.num_failed == 0, "expected failing tests, but test() returned"
    return get_results(results)


@pytest.mark.parametrize(("sim", "name", "params", "env", "seed"), _PARAMS)
def test_design(sim, name, params, env, seed, tmp_path, monkeypatch):
    design = DESIGNS[name]
    result = run_design(design, sim, tmp_path, monkeypatch, params or None, env, seed)
    assert result == (design.num_tests, design.num_failed)
