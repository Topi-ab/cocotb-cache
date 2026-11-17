library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- 8-bit counter with synchronous reset.
entity counter is
  port (clk : in std_logic; rst : in std_logic; q : out unsigned(7 downto 0));
end entity;

architecture rtl of counter is
  signal c : unsigned(7 downto 0) := (others => '0');
begin
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then c <= (others => '0'); else c <= c + 1; end if;
    end if;
  end process;
  q <= c;
end architecture;
