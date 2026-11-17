library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- Top that instantiates the two submodules from their own files.
entity mf_top is
  port (
    a     : in  unsigned(7 downto 0);
    y_inc : out unsigned(7 downto 0);
    y_inv : out unsigned(7 downto 0)
  );
end entity;

architecture rtl of mf_top is
begin
  u_inc : entity work.mf_inc port map (a => a, y => y_inc);
  u_inv : entity work.mf_inv port map (a => a, y => y_inv);
end architecture;
