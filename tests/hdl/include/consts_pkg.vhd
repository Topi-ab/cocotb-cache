library ieee;
use ieee.std_logic_1164.all;

-- Constant shared through a package in a separate file.
package consts_pkg is
  constant INC_MAGIC : std_logic_vector(7 downto 0) := x"A5";
end package;
