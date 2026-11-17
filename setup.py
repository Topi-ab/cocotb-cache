"""Ship ``cocotb_cache.pth`` at the top level of site-packages.

Everything else is declared in pyproject.toml. The .pth file must sit in the
root of the wheel (not inside the package) so that Python's ``site`` module
runs its import line at every interpreter start; that is what activates the
shim without any change to user code (docs/user-guide.md: "How it works").
"""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

PTH = "cocotb_cache.pth"
PTH_SRC = Path(__file__).parent / "src" / PTH


class build_py_with_pth(build_py):
    """build_py that also places cocotb_cache.pth at the root of the wheel.

    Regular wheel: the root of the wheel is ``build_lib``.

    Editable install (PEP 660): setuptools' ``editable_wheel`` keeps modules in
    the project and ignores ``build_lib``, but points ``install.install_lib``
    at the unpacked wheel root and writes that directory into the editable
    wheel as-is, so the .pth is placed there. tests/test_package.py checks
    both install kinds, so a setuptools change that breaks this is caught.
    """

    def _pth_target(self) -> Path:
        if getattr(self, "editable_mode", False):
            install_lib = self.distribution.get_command_obj("install").install_lib
            return Path(install_lib) / PTH
        return Path(self.build_lib) / PTH

    def run(self) -> None:
        super().run()
        target = self._pth_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.copy_file(str(PTH_SRC), str(target), preserve_mode=False)

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        if not getattr(self, "editable_mode", False):
            outputs.append(str(Path(self.build_lib) / PTH))
        return outputs


setup(cmdclass={"build_py": build_py_with_pth})
