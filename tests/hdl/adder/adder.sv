`timescale 1ns/1ps
// Combinational WIDTH-bit adder; the sum keeps the carry (WIDTH+1 bits).
module adder #(parameter int WIDTH = 8) (
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  output logic [WIDTH:0]   sum
);
  assign sum = {1'b0, a} + {1'b0, b};
endmodule
