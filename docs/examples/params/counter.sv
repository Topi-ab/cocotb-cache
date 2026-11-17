`timescale 1ns/1ps
module counter (input logic clk, input logic rst, output logic [7:0] q);
  always_ff @(posedge clk) if (rst) q <= 0; else q <= q + 1;
endmodule
