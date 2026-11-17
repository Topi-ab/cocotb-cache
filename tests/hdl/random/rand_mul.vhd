library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- Combinational 8x8 -> 16 bit unsigned multiplier.
entity rand_mul is
  port (a : in unsigned(7 downto 0); b : in unsigned(7 downto 0); p : out unsigned(15 downto 0));
end entity;

architecture rtl of rand_mul is
begin
  p <= a * b;
end architecture;
