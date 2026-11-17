`timescale 1ns/1ps
// Top that instantiates the two submodules from their own files.
module mf_top (input logic [7:0] a, output logic [7:0] y_inc, output logic [7:0] y_inv);
  mf_inc u_inc (.a(a), .y(y_inc));
  mf_inv u_inv (.a(a), .y(y_inv));
endmodule
