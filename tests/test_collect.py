"""Input collection from simulator command lines."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cocotb_cache.collect import collect_inputs
from cocotb_cache.hashing import sha256_bytes


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = (tmp_path / "proj").resolve()
    r.mkdir()
    return r


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def collect(cmds, root: Path, *, cwd: Path | None = None, phase: str = "build", build_dir: Path | None = None, **kw):
    return collect_inputs(cmds, cwd or root, phase=phase, root=root, build_dir=build_dir or root / "sim_build", **kw)


def paths(result) -> list[str]:
    return [f.path for f in result.inputs]


# --- command-line arguments ------------------------------------------------------


def test_plain_source_arguments(root: Path) -> None:
    a = write(root / "rtl" / "a.vhd", "entity a is end;\n")
    write(root / "rtl" / "b.vhd")
    r = collect([["ghdl", "-i", "--work=top", str(a), str(root / "rtl" / "b.vhd")]], root)
    assert r.ok and paths(r) == ["rtl/a.vhd", "rtl/b.vhd"]
    entry = r.inputs[0]
    assert entry.sha256 == sha256_bytes(b"entity a is end;\n") and entry.size == 17


def test_relative_paths_resolve_against_cwd(root: Path) -> None:
    write(root / "rtl" / "a.v")
    (root / "sim_build").mkdir()
    r = collect([["iverilog", "../rtl/a.v"]], root, cwd=root / "sim_build")
    assert paths(r) == ["rtl/a.v"]


def test_toolchain_paths_outside_root_are_ignored(root: Path) -> None:
    r = collect([["/usr/bin/env", "--load=/usr/lib", "-Wl,-rpath,/usr/lib"]], root)
    assert r.ok and paths(r) == []


def test_foreign_file_outside_root_is_a_problem(root: Path, tmp_path: Path) -> None:
    foreign = write(tmp_path / "elsewhere" / "ip.v")
    r = collect([["iverilog", str(foreign)]], root)
    assert not r.ok and "outside the project root" in r.problems[0]


def test_attached_include_dir_forms(root: Path) -> None:
    write(root / "inc1" / "a.svh")
    write(root / "inc2" / "b.svh")
    write(root / "inc3" / "c.svh")
    write(root / "inc4" / "d.svh")
    cmd = ["iverilog", f"-I{root}/inc1", "-I", str(root / "inc2"), f"+incdir+{root}/inc3+{root}/inc4"]
    assert paths(collect([cmd], root)) == ["inc1/a.svh", "inc2/b.svh", "inc3/c.svh", "inc4/d.svh"]


def test_option_values_and_comma_lists(root: Path) -> None:
    write(root / "lib" / "x.so")
    write(root / "data" / "m.hex")
    cmd = ["verilator", f"-LDFLAGS", f"-Wl,-rpath,{root}/lib", f"--mem={root}/data/m.hex", f"-y{root}/lib"]
    assert paths(collect([cmd], root)) == ["data/m.hex", "lib/x.so"]


def test_directories_are_expanded_without_noise(root: Path) -> None:
    write(root / "ip" / "a.v")
    write(root / "ip" / "sub" / "b.v")
    write(root / "ip" / "__pycache__" / "junk.pyc")
    write(root / "ip" / ".git" / "HEAD")
    assert paths(collect([["iverilog", f"-y{root}/ip"]], root)) == ["ip/a.v", "ip/sub/b.v"]


def test_venv_inside_root_is_toolchain(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    venv = root / ".venv"
    lib = write(venv / "lib" / "libcocotbvpi.so")
    monkeypatch.setattr(sys, "prefix", str(venv))
    r = collect([["vvp", "-m", str(lib)], ["make", "-C", str(venv / "lib")]], root)
    assert r.ok and paths(r) == []


def test_project_root_itself_is_never_expanded(root: Path) -> None:
    write(root / "big" / "file")
    r = collect([["make", "-C", str(root)]], root)
    assert r.ok and paths(r) == []


def test_executable_bit_recorded(root: Path) -> None:
    tool = write(root / "tools" / "gen.sh", "#!/bin/sh\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    plain = write(root / "tools" / "data.txt")
    r = collect([[str(tool), str(plain)]], root)
    assert {f.path: f.executable for f in r.inputs} == {"tools/data.txt": False, "tools/gen.sh": True}


# --- command files -----------------------------------------------------------------


def test_command_file_is_hashed_and_read(root: Path) -> None:
    write(root / "rtl" / "a.sv", "module a; endmodule\n")
    write(root / "inc" / "h.svh")
    cmdf = write(root / "files.f", f"// comment\n{root}/rtl/a.sv  # trailing\n+incdir+{root}/inc\n")
    r = collect([["iverilog", "-f", str(cmdf)]], root)
    assert r.ok and paths(r) == ["files.f", "inc/h.svh", "rtl/a.sv"]
    assert r.cmdfiles == (sha256_bytes(cmdf.read_bytes()),)


def test_nested_and_relative_command_files(root: Path) -> None:
    write(root / "ip" / "core.sv", "module core; endmodule\n")
    write(root / "ip" / "ip.f", "core.sv\n")  # relative to the file with -F
    top = write(root / "top.f", f"-F {root}/ip/ip.f\n")
    r = collect([["verilator", "-f", str(top)]], root)
    assert r.ok and paths(r) == ["ip/core.sv", "ip/ip.f", "top.f"]
    assert len(r.cmdfiles) == 2


def test_command_file_cycle_terminates(root: Path) -> None:
    a = root / "a.f"
    write(a, f"-f {root}/b.f\n")
    write(root / "b.f", f"-f {a}\n")
    r = collect([["iverilog", "-f", str(a)]], root)
    assert r.ok and paths(r) == ["a.f", "b.f"]


def test_missing_command_file_is_a_problem(root: Path) -> None:
    r = collect([["iverilog", "-f", str(root / "nope.f")]], root)
    assert not r.ok


def test_make_dash_f_is_not_a_command_file(root: Path) -> None:
    # Verilator's second command: the makefile does not exist yet on a fresh build.
    b = root / "sim_build"
    r = collect([["make", "-C", str(b), "-f", "Vtop.mk"]], root, build_dir=b)
    assert r.ok and paths(r) == []


def test_command_file_outside_root_hashed_by_content(root: Path, tmp_path: Path) -> None:
    b = (tmp_path / "outside_build").resolve()
    cmdf = write(b / "cmds.f", "+timescale+1ns/1ps\n")
    r = collect([["iverilog", "-f", str(cmdf)]], root, build_dir=b)
    assert r.ok and paths(r) == []
    assert r.cmdfiles == (sha256_bytes(b"+timescale+1ns/1ps\n"),)


# --- build_dir rule -----------------------------------------------------------------


def test_build_dir_outputs_are_not_inputs(root: Path) -> None:
    b = root / "sim_build"
    write(b / "sim.vvp", "stale output of a previous build")
    write(b / "obj" / "x.o")
    cmdf = write(b / "cmds.f", "+timescale+1ns/1ps\n")
    cmd = ["iverilog", "-o", str(b / "sim.vvp"), "-f", str(cmdf)]
    r = collect([cmd, ["verilator", "-Mdir", str(b)]], root, build_dir=b)
    # The generated command file counts by content only (no location-dependent path).
    assert r.ok and paths(r) == []
    assert r.cmdfiles == (sha256_bytes(b"+timescale+1ns/1ps\n"),)


def test_test_phase_leaves_build_dir_out(root: Path) -> None:
    b = root / "sim_build"
    cmdf = write(b / "cmds.f", "+timescale+1ns/1ps\n")
    write(b / "sim.vvp")
    r = collect([["vvp", str(b / "sim.vvp"), "-f", str(cmdf)]], root, phase="test", build_dir=b)
    assert r.ok and paths(r) == []


# --- SystemVerilog includes via pyslang ---------------------------------------------


def test_include_next_to_source_without_dash_i(root: Path) -> None:
    write(root / "rtl" / "local.svh", "`define X 1\n")
    top = write(root / "rtl" / "top.sv", '`include "local.svh"\nmodule top; endmodule\n')
    assert paths(collect([["iverilog", str(top)]], root)) == ["rtl/local.svh", "rtl/top.sv"]


def test_nested_includes_through_include_dir(root: Path) -> None:
    write(root / "inc" / "a.svh", '`include "b.svh"\n')
    write(root / "inc" / "b.svh", "`define B 1\n")
    write(root / "inc" / "unused.svh", "")
    top = write(root / "rtl" / "top.sv", '`include "a.svh"\nmodule top; endmodule\n')
    r = collect([["iverilog", f"-I{root}/inc", str(top)]], root)
    # The include dir is hashed as a whole; pyslang adds nothing outside it here.
    assert paths(r) == ["inc/a.svh", "inc/b.svh", "inc/unused.svh", "rtl/top.sv"]


@pytest.mark.parametrize(
    "define_arg, expected",
    [(None, "rtl/plain.svh"), ("-DFAST", "rtl/fast.svh"), ("+define+FAST=1", "rtl/fast.svh")],
)
def test_ifdef_selects_include(root: Path, define_arg: str | None, expected: str) -> None:
    write(root / "rtl" / "fast.svh")
    write(root / "rtl" / "plain.svh")
    top = write(
        root / "rtl" / "top.sv",
        '`ifdef FAST\n`include "fast.svh"\n`else\n`include "plain.svh"\n`endif\nmodule top; endmodule\n',
    )
    cmd = ["iverilog", *([define_arg] if define_arg else []), str(top)]
    assert paths(collect([cmd], root)) == [expected, "rtl/top.sv"]


def test_macro_from_earlier_file_affects_later_include(root: Path) -> None:
    # One compilation unit, like iverilog/verilator: a `define in a.sv
    # changes which file b.sv includes.
    write(root / "rtl" / "fast.svh")
    write(root / "rtl" / "plain.svh")
    a = write(root / "rtl" / "a.sv", "`define FAST\nmodule a; endmodule\n")
    b = write(root / "rtl" / "b.sv", '`ifdef FAST\n`include "fast.svh"\n`else\n`include "plain.svh"\n`endif\n')
    assert "rtl/fast.svh" in paths(collect([["iverilog", str(a), str(b)]], root))


def test_missing_include_is_a_problem(root: Path) -> None:
    top = write(root / "rtl" / "top.sv", '`include "nowhere.svh"\nmodule top; endmodule\n')
    r = collect([["iverilog", str(top)]], root)
    assert not r.ok and "include" in r.problems[0]


def test_vhdl_is_not_preprocessed(root: Path) -> None:
    v = write(root / "a.vhd", '-- `include "nothing.svh"\nentity a is end;\n')
    assert collect([["nvc", "-a", str(v)]], root).ok


# --- cache_inputs ----------------------------------------------------------------------


def test_cache_inputs_globs_files_and_dirs(root: Path) -> None:
    write(root / "csrc" / "a.cpp")
    write(root / "csrc" / "sub" / "b.cpp")
    write(root / "csrc" / "sub" / "b.h")
    write(root / "mem" / "m.hex")
    write(root / "data" / "d1.txt")
    r = collect([["nvc"]], root, cache_inputs=["csrc/**/*.cpp", "mem/m.hex", "data", f"{root}/csrc/sub/b.h"])
    assert r.ok and paths(r) == ["csrc/a.cpp", "csrc/sub/b.cpp", "csrc/sub/b.h", "data/d1.txt", "mem/m.hex"]


def test_cache_inputs_empty_glob_is_fine_missing_file_is_not(root: Path) -> None:
    assert collect([["nvc"]], root, cache_inputs=["nothing/*.cpp"]).ok
    r = collect([["nvc"]], root, cache_inputs=["csrc/typo.cpp"])
    assert not r.ok and "does not exist" in r.problems[0]


def test_cache_inputs_outside_root_is_a_problem(root: Path, tmp_path: Path) -> None:
    write(tmp_path / "other.cpp")
    assert not collect([["nvc"]], root, cache_inputs=[str(tmp_path / "other.cpp")]).ok


def test_cache_inputs_only_in_build_phase(root: Path) -> None:
    write(root / "csrc" / "a.cpp")
    r = collect([["nvc"]], root, phase="test", cache_inputs=["csrc/*.cpp"])
    assert paths(r) == []


def test_result_is_deterministic(root: Path) -> None:
    for name in ("z.v", "a.v", "m.v"):
        write(root / name)
    files = [str(root / n) for n in ("z.v", "a.v", "m.v")]
    forward = collect([["iverilog", *files]], root)
    backward = collect([["iverilog", *reversed(files)]], root)
    assert forward.inputs == backward.inputs
    assert paths(forward) == ["a.v", "m.v", "z.v"]


def test_symlinked_source_inside_root(root: Path) -> None:
    real = write(root / "rtl" / "real.v")
    os.symlink(real, root / "link.v")
    assert paths(collect([["iverilog", str(root / "link.v")]], root)) == ["rtl/real.v"]


# --- real command lines from cocotb's runners -----------------------------------------

PROJECT = Path(__file__).resolve().parents[1]


def _capture_cmds(monkeypatch: pytest.MonkeyPatch, design_name: str, sim: str, tmp_path: Path) -> dict:
    import cocotb_tools.runner as cr

    from hdl.designs import DESIGNS, SIMULATORS

    captured: dict[str, tuple] = {}
    original = cr.Runner._execute

    def capture(self, cmds, cwd):
        captured[self._phase_for_test] = ([list(map(str, c)) for c in cmds], str(cwd))
        return original(self, cmds, cwd)

    monkeypatch.setattr(cr.Runner, "_execute", capture)
    monkeypatch.syspath_prepend(str(DESIGNS[design_name].directory))
    d, lang = DESIGNS[design_name], SIMULATORS[sim]
    build_dir = tmp_path / "build"
    r = cr.get_runner(sim)
    r._phase_for_test = "build"
    r.build(sources=d.sources(lang), includes=d.includes(lang), hdl_toplevel=d.toplevel,
            build_dir=build_dir, always=True, **d.build_kwargs(lang))
    r._phase_for_test = "test"
    r.test(test_module=d.test_module, hdl_toplevel=d.toplevel, build_dir=build_dir,
           test_dir=build_dir if sim == "ghdl" else tmp_path / "test", results_xml=str(tmp_path / "r.xml"))
    return {"captured": captured, "build_dir": build_dir}


EXPECTED = {
    ("include", "vhdl"): ["tests/hdl/include/consts_pkg.vhd", "tests/hdl/include/inc_top.vhd"],
    ("include", "verilog"): ["tests/hdl/include/inc/consts.svh", "tests/hdl/include/inc_top.sv"],
    ("multifile", "vhdl"): ["tests/hdl/multifile/mf_inc.vhd", "tests/hdl/multifile/mf_inv.vhd", "tests/hdl/multifile/mf_top.vhd"],
    ("multifile", "verilog"): ["tests/hdl/multifile/mf_inc.sv", "tests/hdl/multifile/mf_inv.sv", "tests/hdl/multifile/mf_top.sv"],
}


@pytest.mark.sim
@pytest.mark.parametrize("sim", ["icarus", "verilator", "ghdl", "nvc"])
@pytest.mark.parametrize("design_name", ["include", "multifile"])
def test_real_runner_command_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, design_name: str, sim: str) -> None:
    from hdl.designs import SIMULATORS

    run = _capture_cmds(monkeypatch, design_name, sim, tmp_path)
    (bcmds, bcwd), (tcmds, tcwd) = run["captured"]["build"], run["captured"]["test"]
    root = PROJECT
    build = collect_inputs(bcmds, bcwd, phase="build", root=root, build_dir=run["build_dir"])
    test = collect_inputs(tcmds, tcwd, phase="test", root=root, build_dir=run["build_dir"])
    assert build.ok, build.problems
    assert test.ok, test.problems
    assert [f.path for f in build.inputs] == EXPECTED[(design_name, SIMULATORS[sim])]
    assert test.inputs == ()  # test phase: build_dir via the build key; Python modules: pyinputs
    # Icarus generates cmds.f (timescale) in build_dir, outside the project root here.
    assert (len(build.cmdfiles) == 1) == (sim == "icarus")
