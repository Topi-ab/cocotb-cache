#!/bin/sh
set -e
echo "cocotb:    $(python3 -c 'import cocotb; print(cocotb.__version__, cocotb.__file__)')"
echo "boto3:     $(python3 -c 'import boto3; print(boto3.__version__)' 2>/dev/null || echo 'not installed')"
echo "python:    $(python3 --version)"
echo "ghdl:      $(ghdl --version | head -1)"
echo "nvc:       $(nvc --version | head -1)"
echo "verilator: $(verilator --version)"
echo "versitygw: $(versitygw --version 2>&1 | head -1)"
echo "iverilog:  $(iverilog -V 2>&1 | head -1)"
