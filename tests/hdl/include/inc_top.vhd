library ieee;
use ieee.std_logic_1164.all;
use work.consts_pkg.all;

-- y = a XOR the constant from the package.
entity inc_top is
  port (a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0));
end entity;

architecture rtl of inc_top is
begin
  y <= a xor INC_MAGIC;
end architecture;
