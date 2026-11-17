"""Testbench driving random stimulus.

Uses Python's ``random`` module, which cocotb seeds from ``COCOTB_RANDOM_SEED``
(or a time-based seed), and checks against a reference model, so it passes
with any seed.
"""

import random

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def random_products(dut):
    """p == a * b for random a, b."""
    for _ in range(50):
        a = random.randrange(256)
        b = random.randrange(256)
        dut.a.value = a
        dut.b.value = b
        await Timer(1, unit="ns")
        assert int(dut.p.value) == a * b, (a, b, int(dut.p.value))
