import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


@cocotb.test()
async def counts_as_configured(dut):
    # A data file and an environment variable: both declared in runner.py.
    cycles = int((Path(__file__).parent / "data" / "cycles.txt").read_text())
    mode = os.environ["TB_MODE"]
    cycles += random.randint(0, 3)  # random, but reproducible with cache_fix_seed

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, cycles)
    await RisingEdge(dut.clk)
    dut._log.info("mode=%s cycles=%d q=%d", mode, cycles, int(dut.q.value))
    assert int(dut.q.value) in (cycles, cycles + 1)
