"""Python inputs of the test phase: the cocotb test modules and the project
files they import (docs/format.md).

The simulator's Python imports ``COCOTB_TEST_MODULES`` with the runner's
``PYTHONPATH`` (cocotb sets it from ``sys.path``) and the test directory as
working directory. Each module is resolved here the same way, and its source
is scanned with :mod:`ast` for imports, transitively, but only while the
modules stay inside the project root: everything outside (stdlib, cocotb,
installed packages) belongs to the toolchain identity.

Static analysis sees ``import`` statements anywhere in a module (also inside
functions), relative imports and ``from pkg import submodule``. It does not
see dynamic imports (``importlib.import_module(name)``) or data files a test
opens; those must be declared with ``build(..., cache_inputs=[...])``.

An unresolvable top-level test module is a problem (the cache is skipped). An
unresolvable ``import`` inside a module is ignored: it is usually optional
(``try: import numpy``) and would fail the same way in the simulator.
"""

from __future__ import annotations

import ast
import importlib.machinery
import site
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .collect import Collected, _toolchain_prefixes, _under, _venv_prefixes
from .hashing import sha256_file
from .manifest import InputFile
from .paths import relative_to_root


@dataclass(frozen=True)
class _Module:
    name: str
    origin: Path | None  # None for namespace packages
    search_locations: tuple[str, ...] | None  # set for packages


def _site_prefixes() -> tuple[Path, ...]:
    dirs = []
    try:
        dirs.extend(site.getsitepackages())
    except AttributeError:  # pragma: no cover - some embedded interpreters
        pass
    try:
        dirs.append(site.getusersitepackages())
    except AttributeError:  # pragma: no cover
        pass
    return tuple(Path(d).resolve() for d in dirs)


class _Resolver:
    def __init__(self, path: Sequence[str], root: Path) -> None:
        self.path = list(path)
        self.root = root
        self.outside_ok = (*_toolchain_prefixes(), *_venv_prefixes(), *_site_prefixes())
        self.venv = _venv_prefixes()
        self._cache: dict[str, _Module | None] = {}

    def find(self, fullname: str) -> _Module | None:
        """Resolve a dotted module name like the simulator's import system would."""
        if fullname in self._cache:
            return self._cache[fullname]
        parent_name, _, last = fullname.rpartition(".")
        if parent_name:
            parent = self.find(parent_name)
            if parent is None or parent.search_locations is None:
                self._cache[fullname] = None
                return None
            search = list(parent.search_locations)
        else:
            search = self.path
        spec = importlib.machinery.PathFinder.find_spec(last if not parent_name else fullname, search)
        if spec is None:
            result = None
        else:
            origin = Path(spec.origin).resolve() if spec.origin and spec.has_location else None
            locations = tuple(spec.submodule_search_locations) if spec.submodule_search_locations is not None else None
            result = _Module(fullname, origin, locations)
        self._cache[fullname] = result
        return result

    def location_kind(self, module: _Module) -> str:
        """'project', 'toolchain' or 'foreign' (outside the root, not a tool)."""
        places = [module.origin] if module.origin is not None else [Path(p).resolve() for p in module.search_locations or ()]
        if not places:
            return "toolchain"  # built-in or frozen
        for place in places:
            if relative_to_root(place, self.root) is not None and not _under(place, self.venv):
                return "project"
        if all(_under(p, self.outside_ok) for p in places):
            return "toolchain"
        return "foreign"


def _imports(source: str, module: _Module) -> Iterable[str]:
    """Absolute module names a module's source may import."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    is_package = module.search_locations is not None
    package = module.name if is_package else module.name.rpartition(".")[0]
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                if node.level - 1 > len(parts):
                    continue
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if not base:
                continue
            names.append(base)
            # "from pkg import name" may name a submodule.
            names.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def collect_python_inputs(
    test_modules: Iterable[str],
    pythonpath: Sequence[str],
    cwd: str | PathLike[str],
    *,
    root: Path,
) -> Collected:
    """Project files the simulator's Python imports for ``test_modules``."""
    root = Path(root).resolve()
    # The embedded interpreter searches the working directory first.
    search = [str(Path(cwd).resolve()), *[p for p in pythonpath if p]]
    resolver = _Resolver(search, root)
    files: dict[str, Path] = {}
    problems: list[str] = []
    seen: set[str] = set()
    queue: list[str] = []

    def visit(name: str, *, required: bool) -> None:
        # Importing a.b.c executes a/__init__, a/b/__init__ and a/b/c.
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in seen:
                continue
            module = resolver.find(prefix)
            if module is None:
                if required and i == len(parts):
                    problems.append(f"test module not found: {name}")
                return
            seen.add(prefix)
            kind = resolver.location_kind(module)
            if kind == "toolchain":
                return
            if kind == "foreign":
                problems.append(f"Python module outside the project root: {prefix} ({module.origin})")
                return
            if module.origin is not None:
                rel = relative_to_root(module.origin, root)
                if rel is not None:
                    files[rel] = module.origin
                    queue.append(prefix)

    for name in test_modules:
        name = name.strip()
        if name:
            visit(name, required=True)
    while queue:
        name = queue.pop()
        module = resolver.find(name)
        if module is None or module.origin is None or module.origin.suffix != ".py":
            continue
        try:
            source = module.origin.read_text(errors="replace")
        except OSError as e:
            problems.append(f"cannot read {module.origin}: {e}")
            continue
        for imported in _imports(source, module):
            visit(imported, required=False)

    inputs = []
    for rel, path in sorted(files.items()):
        try:
            digest, size = sha256_file(path)
        except OSError as e:
            problems.append(f"cannot read input {rel}: {e}")
            continue
        inputs.append(InputFile(rel, digest, size, executable=False))
    return Collected(tuple(inputs), tuple(problems))
