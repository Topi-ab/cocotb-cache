"""Testbench for the parameterized adder (VHDL and SystemVerilog).

The width in use is read from the DUT (the length of port ``a``). If the
environment variable ``ADDER_WIDTH`` is set, the testbench also checks that
the DUT was really built/elaborated with that width.
"""

import os

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def adds(dut):
    """sum == a + b, including the carry, for corner cases of the actual width."""
    width = len(dut.a)
    expected_width = os.environ.get("ADDER_WIDTH")
    if expected_width is not None:
        assert width == int(expected_width), (width, expected_width)
    assert len(dut.sum) == width + 1
    top = (1 << width) - 1
    cases = [(0, 0), (1, 2), (top, 1), (top, top), (top >> 1, (top >> 1) + 1)]
    for a, b in cases:
        dut.a.value = a
        dut.b.value = b
        await Timer(1, unit="ns")
        assert int(dut.sum.value) == a + b, (a, b, int(dut.sum.value))
