`timescale 1ns/1ps
// y = a + 1 (modulo 256).
module mf_inc (input logic [7:0] a, output logic [7:0] y);
  assign y = a + 8'd1;
endmodule
