# The three extra build() parameters of cocotb-cache.
# Note: passing them makes cocotb-cache a requirement of this script.
from pathlib import Path

from cocotb_tools.runner import get_runner

here = Path(__file__).resolve().parent
runner = get_runner("icarus")
runner.build(
    sources=[here / "counter.sv"],
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    timescale=("1ns", "1ps"),
    # Files the simulation reads that are not on the simulator's command line.
    cache_inputs=[str(here / "data" / "*.txt")],
    # Environment variables the testbench reads (everything else is hidden).
    cache_env_whitelist=["TB_MODE"],
    # No seed given below: derive a reproducible one from the inputs.
    cache_fix_seed=True,
)
runner.test(
    test_module="test_params",
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
)
