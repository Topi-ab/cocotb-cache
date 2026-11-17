"""Input files of a phase, from the simulator command lines (docs/format.md).

The runner hands ``_execute`` the final command lines. Every argument that
names a file or directory under the project root is an input:

* plain arguments and ``--opt=value`` / comma-separated values;
* attached option prefixes: ``-I<dir>``, ``+incdir+<dir>[+<dir>...]``,
  ``-y<dir>``, ``-v<file>`` (and ``-D<macro>`` / ``+define+`` for macros);
* command files (``iverilog -f/-c``, ``verilator -f/-F``), hashed and read
  recursively; other tools' ``-f`` (e.g. ``make -f``) is not a command file;
* directories are expanded to all files below them.

``build_dir`` holds results, not inputs. In the build phase only command files
under it (such as the runner-generated ``cmds.f``) are hashed; anything else
there, like ``-o build_dir/sim.vvp`` from a previous build, is an output and
ignored, and its directories are never expanded. In the test phase it is left
out entirely, because the build's key covers it.

SystemVerilog sources are preprocessed with pyslang, as one compilation unit
in command-line order, with the include directories and macros from the
command lines. Every `include file it actually opens becomes an input, also
ones found next to the source without ``-I``. A missing include is a problem:
the key would be incomplete, so the cache must be skipped.

User-declared ``cache_inputs`` (``build(..., cache_inputs=[...])``) are added
in the build phase: paths or globs relative to the project root.

Outside the project root, only system and tool locations (the venv, ``/usr``,
``/opt``, ...) are accepted; the toolchain identity covers them. Any other
existing file outside the root (e.g. a source in ``/tmp``) is a problem: it
cannot be stored relative to the root, so the cache is skipped rather than
risking a wrong hit. Command files are the exception: they are hashed by
content wherever they are (``Collected.cmdfiles``), because the runner may
generate one in a ``build_dir`` outside the project (Icarus' ``cmds.f``
carries the timescale).
"""

from __future__ import annotations

import glob as _glob
import os
import shlex
import stat
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

from .hashing import sha256_file
from .manifest import InputFile
from .paths import relative_to_root

SV_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh", ".vlog", ".verilog"})
# Per tool: options whose *next* argument is a command file ("-F": paths in it
# are relative to the file). Other tools' -f (make -f Makefile) is not one.
CMDFILE_OPTIONS = {
    "iverilog": frozenset({"-f", "-c"}),
    "verilator": frozenset({"-f", "-F"}),
    "verilator_bin": frozenset({"-f", "-F"}),
}
# Directory names never expanded (VCS metadata, caches, virtual envs).
SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"})
MAX_CMDFILE_DEPTH = 16
GLOB_CHARS = frozenset("*?[")


def _venv_prefixes() -> tuple[Path, ...]:
    """The Python environment: toolchain, even when it lies inside the project."""
    return tuple(sorted({Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}))


def _toolchain_prefixes() -> tuple[Path, ...]:
    """Locations outside the project root that belong to the toolchain."""
    prefixes = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    for p in ("/usr", "/opt", "/lib", "/lib32", "/lib64", "/bin", "/sbin", "/etc", "/nix", "/proc", "/dev", "/sys"):
        prefixes.add(Path(p))
    return tuple(sorted(prefixes))


def _under(path: Path, prefixes: Iterable[Path]) -> bool:
    return any(path == p or p in path.parents for p in prefixes)


@dataclass(frozen=True)
class Collected:
    """Result of collecting the inputs of one phase."""

    inputs: tuple[InputFile, ...]
    problems: tuple[str, ...] = ()
    # SHA-256 of every command file, in processing order (wherever it lives).
    cmdfiles: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """``False`` if the inputs may be incomplete: the cache must be skipped."""
        return not self.problems


@dataclass
class _Collector:
    root: Path
    build_dir: Path | None
    phase: str
    files: dict[str, Path] = field(default_factory=dict)
    sv_sources: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cmdfile_digests: list[str] = field(default_factory=list)
    _seen_cmdfiles: set[Path] = field(default_factory=set)
    _toolchain: tuple[Path, ...] = field(default_factory=_toolchain_prefixes)
    _venv: tuple[Path, ...] = field(default_factory=_venv_prefixes)

    # --- classification ---------------------------------------------------------

    def _in_build_dir(self, path: Path) -> bool:
        return self.build_dir is not None and (path == self.build_dir or self.build_dir in path.parents)

    def add_file(self, path: Path, *, cmdfile: bool = False) -> None:
        path = path.resolve()
        # Nothing under build_dir is an input file: its outputs are results, and
        # a command file there (e.g. Icarus' generated cmds.f) is covered by its
        # content hash in Collected.cmdfiles, without a location-dependent path.
        if self._in_build_dir(path):
            return
        if not cmdfile and _under(path, self._venv):
            return  # cocotb libraries etc.: covered by the toolchain identity
        rel = relative_to_root(path, self.root)
        if rel is None:
            if not cmdfile and path != self.root:
                self._outside_root(path)
            return
        if rel not in self.files:
            self.files[rel] = path
        if path.suffix.lower() in SV_SUFFIXES and path not in self.sv_sources:
            self.sv_sources.append(path)

    def _outside_root(self, path: Path) -> None:
        if self._in_build_dir(path) or _under(path, self._toolchain):
            return
        self.problems.append(f"input outside the project root: {path}")

    def add_dir(self, path: Path) -> None:
        path = path.resolve()
        if self._in_build_dir(path) or path == self.root or _under(path, self._venv):
            return
        if relative_to_root(path, self.root) is None:
            self._outside_root(path)
            return
        for dirpath, dirnames, filenames in os.walk(path):
            d = Path(dirpath)
            dirnames[:] = sorted(
                n
                for n in dirnames
                if n not in SKIP_DIR_NAMES and not self._in_build_dir(d / n) and not _under((d / n).resolve(), self._venv)
            )
            for name in sorted(filenames):
                f = d / name
                if f.is_file():
                    self._add_walked_file(f)

    def _add_walked_file(self, f: Path) -> None:
        # Files inside expanded directories are inputs but not compiled
        # sources: only explicitly named sources are preprocessed.
        resolved = f.resolve()
        rel = relative_to_root(resolved, self.root)
        if rel is not None and not self._in_build_dir(resolved) and not _under(resolved, self._venv):
            self.files.setdefault(rel, resolved)

    # --- command lines ----------------------------------------------------------

    def add_path_candidate(self, value: str, cwd: Path) -> None:
        if not value or "\0" in value:
            return
        p = Path(value)
        if not p.is_absolute():
            p = cwd / p
        try:
            if p.is_file():
                self.add_file(p)
            elif p.is_dir():
                self.add_dir(p)
        except OSError:
            return

    def add_incdir(self, value: str, cwd: Path) -> None:
        p = Path(value) if Path(value).is_absolute() else cwd / value
        if p.is_dir():
            if p.resolve() not in self.incdirs:
                self.incdirs.append(p.resolve())
            self.add_dir(p)

    def process_args(self, args: Sequence[str], cwd: Path, tool: str, depth: int = 0) -> None:
        cmdfile_opts = CMDFILE_OPTIONS.get(tool, frozenset())
        i = 0
        while i < len(args):
            arg = str(args[i])
            if arg in cmdfile_opts and i + 1 < len(args):
                self.process_cmdfile(str(args[i + 1]), cwd, tool, relative_to_file=(arg == "-F"), depth=depth)
                i += 2
                continue
            if arg == "-I" and i + 1 < len(args):
                self.add_incdir(str(args[i + 1]), cwd)
                i += 2
                continue
            self.process_token(arg, cwd)
            i += 1

    def process_token(self, arg: str, cwd: Path) -> None:
        if arg.startswith("+incdir+"):
            for d in arg[len("+incdir+") :].split("+"):
                if d:
                    self.add_incdir(d, cwd)
            return
        if arg.startswith("+define+"):
            self.defines.extend(d for d in arg[len("+define+") :].split("+") if d)
            return
        if arg.startswith("+"):
            return  # other plusargs (+libext+, +timescale+, ...) carry no paths
        if len(arg) > 2 and arg.startswith("-I"):
            self.add_incdir(arg[2:], cwd)
            return
        if len(arg) > 2 and arg.startswith("-D"):
            self.defines.append(arg[2:])
            return
        if len(arg) > 2 and arg[:2] in ("-y", "-v"):
            self.add_path_candidate(arg[2:], cwd)
            return
        if arg.startswith("-") and "=" in arg:
            for part in arg.split("=", 1)[1].split(","):
                self.add_path_candidate(part, cwd)
            return
        if arg.startswith("-") and "," in arg:
            for part in arg.split(",")[1:]:
                self.add_path_candidate(part, cwd)
            return
        self.add_path_candidate(arg, cwd)

    def process_cmdfile(self, value: str, cwd: Path, tool: str, *, relative_to_file: bool, depth: int) -> None:
        path = (Path(value) if Path(value).is_absolute() else cwd / value).resolve()
        if not path.is_file():
            self.problems.append(f"command file not found: {value}")
            return
        self.add_file(path, cmdfile=True)
        try:
            self.cmdfile_digests.append(sha256_file(path)[0])
        except OSError as e:
            self.problems.append(f"cannot read command file {value}: {e}")
            return
        if path in self._seen_cmdfiles:
            return
        if depth >= MAX_CMDFILE_DEPTH:
            self.problems.append(f"command files nested deeper than {MAX_CMDFILE_DEPTH}: {value}")
            return
        self._seen_cmdfiles.add(path)
        try:
            text = path.read_text(errors="replace")
        except OSError as e:
            self.problems.append(f"cannot read command file {value}: {e}")
            return
        tokens = _cmdfile_tokens(text)
        self.process_args(tokens, path.parent if relative_to_file else cwd, tool, depth + 1)

    # --- SystemVerilog includes -------------------------------------------------

    def scan_sv_includes(self) -> None:
        if not self.sv_sources:
            return
        try:
            import pyslang
            from pyslang.parsing import PreprocessorOptions
            from pyslang.syntax import SyntaxTree
        except Exception as e:  # pragma: no cover - dependency is required
            self.problems.append(f"pyslang unavailable ({e}); SV includes unknown")
            return
        sm = pyslang.SourceManager()
        options = PreprocessorOptions()
        options.additionalIncludePaths = [str(d) for d in self.incdirs]
        options.predefines = list(self.defines)
        try:
            tree = SyntaxTree.fromFiles([str(p) for p in self.sv_sources], sm, pyslang.Bag([options]))
        except Exception as e:
            self.problems.append(f"pyslang could not preprocess the SV sources: {e}")
            return
        for diag in tree.diagnostics:
            if diag.isError() and "CouldNotOpenIncludeFile" in str(diag.code):
                self.problems.append(f"an `include could not be resolved ({diag.code})")
        for inc in tree.getIncludeDirectives():
            try:
                full = sm.getFullPath(inc.buffer.id)
            except Exception:
                continue
            if full and str(full):
                self.add_file(Path(full))

    # --- user-declared inputs ---------------------------------------------------

    def add_cache_inputs(self, patterns: Iterable[str]) -> None:
        for pattern in patterns:
            p = Path(pattern)
            if p.is_absolute():
                if relative_to_root(p, self.root) is None and p.resolve() != self.root:
                    self.problems.append(f"cache_inputs entry outside the project root: {pattern}")
                    continue
                full = str(p)
            else:
                full = str(self.root / p)
            if GLOB_CHARS & set(pattern):
                for match in sorted(_glob.glob(full, recursive=True)):
                    m = Path(match)
                    if m.is_file():
                        self._add_walked_file(m)
                    elif m.is_dir():
                        self.add_dir(m)
                continue  # an empty glob is not an error
            target = Path(full)
            if target.is_file():
                self._add_walked_file(target)
            elif target.is_dir():
                self.add_dir(target)
            else:
                self.problems.append(f"cache_inputs entry does not exist: {pattern}")

    # --- result -----------------------------------------------------------------

    def result(self) -> Collected:
        inputs = []
        for rel, path in sorted(self.files.items()):
            try:
                digest, size = sha256_file(path)
                mode = path.stat().st_mode
            except OSError as e:
                self.problems.append(f"cannot read input {rel}: {e}")
                continue
            inputs.append(InputFile(rel, digest, size, executable=bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))))
        return Collected(tuple(inputs), tuple(self.problems), tuple(self.cmdfile_digests))


def _cmdfile_tokens(text: str) -> list[str]:
    """Tokens of a Verilog-style command file: ``//`` and ``#`` comments removed."""
    lines = []
    for line in text.splitlines():
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx]
        lines.append(line)
    try:
        return shlex.split("\n".join(lines))
    except ValueError:
        return " ".join(lines).split()


def collect_inputs(
    cmds: Sequence[Sequence[str | PathLike[str]]],
    cwd: str | PathLike[str],
    *,
    phase: str,
    root: Path,
    build_dir: str | PathLike[str] | None,
    cache_inputs: Iterable[str] = (),
) -> Collected:
    """Collect and hash the input files of one ``_execute`` call."""
    c = _Collector(
        root=Path(root).resolve(),
        build_dir=Path(build_dir).resolve() if build_dir is not None else None,
        phase=phase,
    )
    work = Path(cwd).resolve()
    for cmd in cmds:
        args = [str(a) for a in cmd]
        if args:
            c.process_args(args, work, tool=Path(args[0]).name)
    c.scan_sv_includes()
    if phase == "build":
        c.add_cache_inputs(cache_inputs)
    return c.result()
