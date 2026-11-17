"""Project root and paths relative to it (docs/format.md).

The project root anchors every stored path, so a cache key does not depend on
where the project lives on disk. It is, in order:

1. ``COCOTB_CACHE_ROOT``, if set;
2. the top of the git work tree containing the current directory;
3. the current directory.
"""

from __future__ import annotations

import os
from os import PathLike
from pathlib import Path


def project_root(cwd: str | PathLike[str] | None = None) -> Path:
    """The project root for a run started in *cwd* (default: current directory)."""
    override = os.environ.get("COCOTB_CACHE_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    start = Path(cwd if cwd is not None else os.getcwd()).resolve()
    git_top = find_git_root(start)
    return git_top if git_top is not None else start


def find_git_root(start: Path) -> Path | None:
    """The closest directory at or above *start* with a ``.git`` entry.

    ``.git`` may be a directory (normal repository) or a file (worktree,
    submodule). No git executable is needed.
    """
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def relative_to_root(path: str | PathLike[str], root: Path) -> str | None:
    """*path* as a "/"-separated path relative to *root*, or ``None`` if outside.

    The path is resolved first (symlinks, ``..``), so a link that points out
    of the project is treated as outside. The root itself maps to ``None``:
    it is never an input on its own.
    """
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    if rel == Path("."):
        return None
    return rel.as_posix()


class Normalizer:
    """Replace machine-specific absolute path prefixes with placeholders.

    ``${BUILD_DIR}`` and ``${TEST_DIR}`` (the runner's directories, which may
    be per-run temporary directories), ``${VENV}`` (the Python environment)
    and ``${ROOT}`` (the project root). The most specific prefix wins, so a
    build directory inside the project becomes ``${BUILD_DIR}``, not
    ``${ROOT}/sim_build``.

    A prefix is replaced only at path boundaries: it must start the text or
    follow a separator (``= : ; , +`` or whitespace) or a one-letter option
    (``-I/work/inc``), and be followed by ``/``, a separator or the end. So
    ``/work`` does not match inside ``/home/x/work`` or ``/workspace``.
    """

    def __init__(
        self,
        root: Path,
        venvs: "tuple[Path, ...]" = (),
        build_dir: Path | None = None,
        test_dir: Path | None = None,
    ) -> None:
        import re

        pairs: list[tuple[str, str]] = []
        for placeholder, path in (("${BUILD_DIR}", build_dir), ("${TEST_DIR}", test_dir)):
            if path is not None:
                pairs.append((str(Path(path).resolve()), placeholder))
        pairs.extend((str(Path(v).resolve()), "${VENV}") for v in venvs)
        pairs.append((str(Path(root).resolve()), "${ROOT}"))
        # Longest prefix first; drop duplicates (e.g. test_dir == build_dir).
        seen: set[str] = set()
        self._pairs = []
        for prefix, placeholder in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
            if prefix not in seen and prefix != "/":
                seen.add(prefix)
                self._pairs.append((prefix, placeholder))
        lead = r"(?:(?<=^)|(?<=[=:;,+\s\"'])|(?<=^-[A-Za-z]))"
        trail = r"(?=$|[/=:;,+\s\"'])"
        self._regex = re.compile(
            "|".join(f"{lead}({re.escape(prefix)}){trail}" for prefix, _ in self._pairs)
        )
        self._lookup = dict(self._pairs)

    def text(self, value: str) -> str:
        """*value* with every known absolute prefix replaced by its placeholder."""
        if not self._pairs:
            return value
        return self._regex.sub(lambda m: self._lookup[m.group(m.lastindex)], value)
