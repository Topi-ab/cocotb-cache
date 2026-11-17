"""Description of the small HDL test designs under ``tests/hdl/``.

Each :class:`Design` has a VHDL and a SystemVerilog version with identical
port names, and one cocotb testbench module that works for both languages.
Integration tests use this module to build and test the designs with
``cocotb_tools.runner`` without knowing the file layout::

    from hdl.designs import DESIGNS, SIMULATORS

    design = DESIGNS["adder"]
    lang = SIMULATORS["nvc"]                   # "vhdl"
    runner = get_runner("nvc")
    runner.build(sources=design.sources(lang), includes=design.includes(lang),
                 hdl_toplevel=design.toplevel, parameters={"WIDTH": 12},
                 build_dir=..., **design.build_kwargs(lang))
    # design.directory must be on sys.path (PYTHONPATH of the simulator is
    # taken from sys.path), e.g. monkeypatch.syspath_prepend(design.directory)
    runner.test(test_module=design.test_module, hdl_toplevel=design.toplevel,
                parameters={"WIDTH": 12}, build_dir=...)

All paths are absolute. Source lists are in compilation order (packages and
submodules before their users).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Directory that contains one subdirectory per design.
HDL_DIR = Path(__file__).resolve().parent

#: Simulators used for the designs and the HDL language each one simulates.
SIMULATORS: dict[str, str] = {
    "ghdl": "vhdl",
    "nvc": "vhdl",
    "icarus": "verilog",
    "verilator": "verilog",
}

#: Timescale passed to ``build()`` for Verilog simulators (Verilator needs one).
VERILOG_TIMESCALE = ("1ns", "1ps")


@dataclass(frozen=True)
class Design:
    """One test design.

    Attributes:
        name: Short design name (also the subdirectory name).
        toplevel: HDL toplevel entity/module name (same in both languages).
        test_module: Python module with the cocotb tests (in :attr:`directory`).
        vhdl_files: VHDL sources relative to :attr:`directory`, in compile order.
        sv_files: SystemVerilog sources relative to :attr:`directory`.
        sv_include_dirs: Include directories for SV, relative to :attr:`directory`.
        parameters: Default build/test parameters (Verilog parameters /
            VHDL generics); empty if the design has none.
        num_tests: Number of cocotb tests in :attr:`test_module`.
        num_failed: Number of those tests that are expected to fail.
    """

    name: str
    toplevel: str
    test_module: str
    vhdl_files: tuple[str, ...]
    sv_files: tuple[str, ...]
    sv_include_dirs: tuple[str, ...] = ()
    parameters: dict[str, object] = field(default_factory=dict)
    num_tests: int = 1
    num_failed: int = 0

    @property
    def directory(self) -> Path:
        """Absolute directory of the design and its testbench module."""
        return HDL_DIR / self.name

    def sources(self, lang: str) -> list[Path]:
        """Absolute source paths for ``lang`` (``"vhdl"`` or ``"verilog"``)."""
        files = self.vhdl_files if _check_lang(lang) == "vhdl" else self.sv_files
        return [self.directory / f for f in files]

    def includes(self, lang: str) -> list[Path]:
        """Absolute include directories for ``lang`` (none for VHDL)."""
        if _check_lang(lang) == "vhdl":
            return []
        return [self.directory / d for d in self.sv_include_dirs]

    def build_kwargs(self, lang: str) -> dict[str, object]:
        """Extra language-specific keyword arguments for ``Runner.build()``."""
        if _check_lang(lang) == "vhdl":
            return {}
        return {"timescale": VERILOG_TIMESCALE}


def _check_lang(lang: str) -> str:
    if lang not in ("vhdl", "verilog"):
        raise ValueError(f"unknown HDL language {lang!r}")
    return lang


_DESIGN_LIST = [
    # Clocked 8-bit counter with synchronous reset (clk, rst, q).
    Design(
        name="counter",
        toplevel="counter",
        test_module="tb_counter",
        vhdl_files=("counter.vhd",),
        sv_files=("counter.sv",),
    ),
    # Combinational WIDTH-bit adder; the testbench reads the width from the DUT
    # and, if ADDER_WIDTH is set in its environment, checks it.
    Design(
        name="adder",
        toplevel="adder",
        test_module="tb_adder",
        vhdl_files=("adder.vhd",),
        sv_files=("adder.sv",),
        parameters={"WIDTH": 8},
    ),
    # Constant from an included header (SV, include dir "inc") or a package in
    # a separate file (VHDL): y = a ^ 0xA5.
    Design(
        name="include",
        toplevel="inc_top",
        test_module="tb_include",
        vhdl_files=("consts_pkg.vhd", "inc_top.vhd"),
        sv_files=("inc_top.sv",),
        sv_include_dirs=("inc",),
    ),
    # Top instantiating two submodules, each in its own file.
    Design(
        name="multifile",
        toplevel="mf_top",
        test_module="tb_multifile",
        vhdl_files=("mf_inc.vhd", "mf_inv.vhd", "mf_top.vhd"),
        sv_files=("mf_inc.sv", "mf_inv.sv", "mf_top.sv"),
    ),
    # Pass-through design; one test passes and one fails on purpose.
    Design(
        name="failing",
        toplevel="passthru",
        test_module="tb_failing",
        vhdl_files=("passthru.vhd",),
        sv_files=("passthru.sv",),
        num_tests=2,
        num_failed=1,
    ),
    # 8x8 multiplier driven with random stimulus; passes with any seed.
    Design(
        name="random",
        toplevel="rand_mul",
        test_module="tb_random",
        vhdl_files=("rand_mul.vhd",),
        sv_files=("rand_mul.sv",),
    ),
]

#: All designs by name.
DESIGNS: dict[str, Design] = {d.name: d for d in _DESIGN_LIST}
