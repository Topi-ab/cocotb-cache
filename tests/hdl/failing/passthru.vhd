library ieee;
use ieee.std_logic_1164.all;

-- y = a.
entity passthru is
  port (a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0));
end entity;

architecture rtl of passthru is
begin
  y <= a;
end architecture;
