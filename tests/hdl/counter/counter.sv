`timescale 1ns/1ps
// 8-bit counter with synchronous reset.
module counter (input logic clk, input logic rst, output logic [7:0] q);
  always_ff @(posedge clk) begin
    if (rst) q <= 8'd0;
    else q <= q + 8'd1;
  end
endmodule
