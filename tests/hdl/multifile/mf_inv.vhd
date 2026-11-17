library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- y = not a.
entity mf_inv is
  port (a : in unsigned(7 downto 0); y : out unsigned(7 downto 0));
end entity;

architecture rtl of mf_inv is
begin
  y <= not a;
end architecture;
