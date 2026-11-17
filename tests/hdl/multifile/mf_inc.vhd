library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- y = a + 1 (modulo 256).
entity mf_inc is
  port (a : in unsigned(7 downto 0); y : out unsigned(7 downto 0));
end entity;

architecture rtl of mf_inc is
begin
  y <= a + 1;
end architecture;
