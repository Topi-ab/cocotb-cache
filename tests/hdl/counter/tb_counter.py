"""Testbench for the counter design (VHDL and SystemVerilog)."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge


@cocotb.test()
async def counts_after_reset(dut):
    """The counter starts at 0 after reset and increments once per clock."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    assert int(dut.q.value) == 0
    dut.rst.value = 0
    for expected in range(1, 6):
        await FallingEdge(dut.clk)
        assert int(dut.q.value) == expected
