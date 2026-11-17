"""Integration: every test design on every simulator, as a plain runner script
and as a pytest test, through the cache.

Each case runs twice from scratch (fresh build_dir and test_dir): the first
run must miss and store both phases, the second must hit both and give the
same result (same summary, byte-identical results file, same exit status).
The random seed is fixed explicitly (see test_cache.py for runs without one).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cache_harness import Project
from hdl.designs import DESIGNS, SIMULATORS
from s3_local import S3Endpoint

pytestmark = [pytest.mark.sim, pytest.mark.s3]

# Per design: extra arguments for Project.run.
CASES: dict[str, dict] = {
    "counter": {},
    "adder": {"parameters": {"WIDTH": 4}, "extra_env": {"ADDER_WIDTH": "4"}},
    "include": {},
    "multifile": {},
    "failing": {},
    "random": {"seed": 987654},
}


@pytest.fixture
def project(tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str):
    def make(design: str) -> Project:
        return Project(tmp_path, design, local_s3, s3_prefix)

    return make


def _expected_returncode(design: str, style: str) -> int:
    # A failing cocotb test makes Runner.test() exit under pytest only; a plain
    # script just gets the results file back.
    return 1 if DESIGNS[design].num_failed and style == "pytest" else 0


@pytest.mark.parametrize("style", ["script", "pytest"])
@pytest.mark.parametrize("sim", list(SIMULATORS))
@pytest.mark.parametrize("design", list(CASES))
def test_design_miss_then_hit(project, design: str, sim: str, style: str) -> None:
    p = project(design)
    kw = {"seed": 1, **CASES[design]}
    expected_rc = _expected_returncode(design, style)
    expected_summary = (DESIGNS[design].num_tests, DESIGNS[design].num_failed)

    first = p.run(sim, style=style, **kw)
    assert first.returncode == expected_rc, first.stdout[-3000:] + first.stderr[-3000:]
    assert first.phase_events("build") == ["miss", "stored"]
    assert first.phase_events("test") == ["miss", "stored"]
    assert first.summary == expected_summary

    second = p.run(sim, style=style, **kw)
    assert second.returncode == expected_rc, second.stdout[-3000:] + second.stderr[-3000:]
    assert second.phase_events("build") == ["hit"]
    assert second.phase_events("test") == ["hit"]
    assert second.summary == expected_summary
    assert second.results_xml == first.results_xml


@pytest.mark.parametrize("sim", list(SIMULATORS))
def test_include_or_package_change_misses(project, sim: str) -> None:
    # A change in a file the sources depend on but do not name on the command
    # line (SV `include) or name separately (VHDL package) must miss.
    p = project("include")
    dependency = (
        p.design_dir / "consts_pkg.vhd" if SIMULATORS[sim] == "vhdl" else p.design_dir / "inc" / "consts.svh"
    )
    first = p.run(sim)
    assert first.returncode == 0 and first.phase_events("build") == ["miss", "stored"]
    assert p.run(sim).phase_events("build") == ["hit"]
    comment = "-- changed" if SIMULATORS[sim] == "vhdl" else "// changed"
    dependency.write_text(dependency.read_text() + f"\n{comment}\n")
    changed = p.run(sim)
    assert changed.returncode == 0, changed.stdout[-3000:] + changed.stderr[-3000:]
    assert changed.phase_events("build") == ["miss", "stored"]
    assert changed.phase_events("test") == ["miss", "stored"]
