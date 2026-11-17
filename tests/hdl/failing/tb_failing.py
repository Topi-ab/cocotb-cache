"""Testbench with one passing and one deliberately failing test.

Expected result: 2 tests, 1 failure (``fails_on_purpose``).
"""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def passes(dut):
    """y follows a."""
    dut.a.value = 0x3C
    await Timer(1, unit="ns")
    assert int(dut.y.value) == 0x3C


@cocotb.test()
async def fails_on_purpose(dut):
    """Deliberately wrong expected value: this test must fail."""
    dut.a.value = 0x3C
    await Timer(1, unit="ns")
    assert int(dut.y.value) == 0x3D, "deliberate failure"
