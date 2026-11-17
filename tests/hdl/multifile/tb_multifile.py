"""Testbench for the multifile design (top + two submodules)."""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def both_submodules(dut):
    """Both submodule outputs reach the top."""
    for a in (0x00, 0x01, 0x7F, 0xFF):
        dut.a.value = a
        await Timer(1, unit="ns")
        assert int(dut.y_inc.value) == (a + 1) & 0xFF
        assert int(dut.y_inv.value) == (~a) & 0xFF
