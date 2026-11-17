"""Testbench for the include (SV) / package (VHDL) design."""

import cocotb
from cocotb.triggers import Timer

INC_MAGIC = 0xA5


@cocotb.test()
async def uses_constant(dut):
    """y == a ^ INC_MAGIC, so the constant from the header/package is in effect."""
    for a in (0x00, 0xFF, 0x5A, 0x12):
        dut.a.value = a
        await Timer(1, unit="ns")
        assert int(dut.y.value) == a ^ INC_MAGIC, (a, int(dut.y.value))
