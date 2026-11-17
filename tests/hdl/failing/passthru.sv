`timescale 1ns/1ps
// y = a.
module passthru (input logic [7:0] a, output logic [7:0] y);
  assign y = a;
endmodule
