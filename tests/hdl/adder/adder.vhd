library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- Combinational WIDTH-bit adder; the sum keeps the carry (WIDTH+1 bits).
entity adder is
  generic (WIDTH : positive := 8);
  port (
    a   : in  unsigned(WIDTH - 1 downto 0);
    b   : in  unsigned(WIDTH - 1 downto 0);
    sum : out unsigned(WIDTH downto 0)
  );
end entity;

architecture rtl of adder is
begin
  sum <= resize(a, WIDTH + 1) + resize(b, WIDTH + 1);
end architecture;
