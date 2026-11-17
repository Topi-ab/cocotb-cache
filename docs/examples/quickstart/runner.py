# An ordinary cocotb runner script: nothing in it is specific to cocotb-cache.
import logging
from pathlib import Path

from cocotb_tools.runner import get_runner

logging.basicConfig(level=logging.INFO)  # optional: shows "cocotb-cache: hit ..."

here = Path(__file__).resolve().parent
runner = get_runner("icarus")
runner.build(
    sources=[here / "counter.sv"],
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    timescale=("1ns", "1ps"),
)
runner.test(
    test_module="test_counter",
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    seed=12345,  # a fixed seed makes the test phase cacheable
)
