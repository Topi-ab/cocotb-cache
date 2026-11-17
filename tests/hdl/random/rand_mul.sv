`timescale 1ns/1ps
// Combinational 8x8 -> 16 bit unsigned multiplier.
module rand_mul (input logic [7:0] a, input logic [7:0] b, output logic [15:0] p);
  assign p = {8'd0, a} * {8'd0, b};
endmodule
