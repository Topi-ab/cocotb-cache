`timescale 1ns/1ps
`include "consts.svh"
// y = a XOR the constant from the included header.
module inc_top (input logic [7:0] a, output logic [7:0] y);
  assign y = a ^ `INC_MAGIC;
endmodule
