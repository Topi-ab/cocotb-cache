"""SRC and RESULT manifests (docs/format.md).

A manifest is an immutable value with a strict JSON form. ``to_json()`` gives
the canonical object (lists in canonical order), ``to_bytes()`` its canonical
encoding, and ``key()`` the SHA-256 of those bytes. ``from_json()`` accepts
only well-formed manifests of the current schema and rejects unknown fields,
so a manifest written by a newer, incompatible version is never misread.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .hashing import canonical_hash, canonical_json, is_digest

SCHEMA_SRC = "cocotb-cache/src/v1"
SCHEMA_RESULT = "cocotb-cache/result/v1"

PHASES = ("build", "test")
OUTPUT_BASES = ("build_dir", "test_dir", "log_file", "results_file")
# Bases that name a single file: the entry path is empty.
FILE_BASES = ("log_file", "results_file")


class ManifestError(ValueError):
    """A manifest is malformed or violates the schema."""


# --- validation helpers ------------------------------------------------------


def check_relpath(path: object, *, what: str, allow_empty: bool = False) -> str:
    """Validate a stored relative path: "/"-separated, no "..", not absolute."""
    if not isinstance(path, str):
        raise ManifestError(f"{what}: path must be a string, got {type(path).__name__}")
    if path == "":
        if allow_empty:
            return path
        raise ManifestError(f"{what}: empty path")
    if path.startswith("/") or "\\" in path or "\0" in path:
        raise ManifestError(f"{what}: invalid path {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ManifestError(f"{what}: path {path!r} must not contain empty, '.' or '..' parts")
    return path


def _check_digest(value: object, what: str) -> str:
    if not is_digest(value):
        raise ManifestError(f"{what}: not a SHA-256 hex digest: {value!r}")
    return value  # type: ignore[return-value]


def _check_size(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"{what}: size must be a non-negative int, got {value!r}")
    return value


def _check_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{what}: must be a bool, got {value!r}")
    return value


def _check_str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{what}: must be a string, got {value!r}")
    return value


def _check_keys(obj: object, what: str, required: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ManifestError(f"{what}: must be an object")
    keys = set(obj)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ManifestError(f"{what}: missing fields {sorted(missing)}")
    if unknown:
        raise ManifestError(f"{what}: unknown fields {sorted(unknown)}")
    return obj


def _unique(items: Iterable[tuple[str, ...]], what: str) -> None:
    seen: set[tuple[str, ...]] = set()
    for item in items:
        if item in seen:
            raise ManifestError(f"{what}: duplicate entry {item}")
        seen.add(item)


# --- building blocks ----------------------------------------------------------


@dataclass(frozen=True)
class InputFile:
    """A file the phase reads, relative to the project root."""

    path: str
    sha256: str
    size: int
    executable: bool = False

    def __post_init__(self) -> None:
        check_relpath(self.path, what="inputs")
        _check_digest(self.sha256, f"inputs[{self.path}].sha256")
        _check_size(self.size, f"inputs[{self.path}].size")
        _check_bool(self.executable, f"inputs[{self.path}].executable")

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "executable": self.executable}

    @classmethod
    def from_json(cls, obj: object) -> InputFile:
        o = _check_keys(obj, "inputs[]", {"path", "sha256", "size", "executable"})
        return cls(o["path"], o["sha256"], o["size"], o["executable"])


def _check_base(base: object, what: str) -> str:
    if base not in OUTPUT_BASES:
        raise ManifestError(f"{what}: base must be one of {OUTPUT_BASES}, got {base!r}")
    return base  # type: ignore[return-value]


@dataclass(frozen=True)
class OutputFile:
    """A file the phase produced, relative to its base directory.

    For a single-file base (``log_file``, ``results_file``) the path is
    empty: the entry is that file.
    """

    base: str
    path: str
    sha256: str
    size: int
    executable: bool = False

    def __post_init__(self) -> None:
        _check_base(self.base, "outputs")
        if self.base in FILE_BASES:
            if self.path != "":
                raise ManifestError(f"outputs: a {self.base} entry must have an empty path")
        else:
            check_relpath(self.path, what="outputs")
        _check_digest(self.sha256, f"outputs[{self.base}:{self.path}].sha256")
        _check_size(self.size, f"outputs[{self.base}:{self.path}].size")
        _check_bool(self.executable, f"outputs[{self.base}:{self.path}].executable")

    def to_json(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "executable": self.executable,
        }

    @classmethod
    def from_json(cls, obj: object) -> OutputFile:
        o = _check_keys(obj, "outputs[]", {"base", "path", "sha256", "size", "executable"})
        return cls(o["base"], o["path"], o["sha256"], o["size"], o["executable"])


@dataclass(frozen=True)
class OutputDir:
    """An empty directory the phase produced (recreated on restore)."""

    base: str
    path: str

    def __post_init__(self) -> None:
        _check_base(self.base, "dirs")
        if self.base in FILE_BASES:
            raise ManifestError(f"dirs: base {self.base!r} is not a directory")
        check_relpath(self.path, what="dirs")

    def to_json(self) -> dict[str, Any]:
        return {"base": self.base, "path": self.path}

    @classmethod
    def from_json(cls, obj: object) -> OutputDir:
        o = _check_keys(obj, "dirs[]", {"base", "path"})
        return cls(o["base"], o["path"])


@dataclass(frozen=True)
class Toolchain:
    """Identity of everything outside the project root that affects a phase."""

    python: str
    platform: str
    cocotb_version: str
    cocotb_dist: str
    simulator: str
    shim: str
    image: str | None = None

    def __post_init__(self) -> None:
        for name in ("python", "platform", "cocotb_version", "simulator", "shim"):
            _check_str(getattr(self, name), f"toolchain.{name}")
        _check_digest(self.cocotb_dist, "toolchain.cocotb.dist")
        if self.image is not None:
            _check_str(self.image, "toolchain.image")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "python": self.python,
            "platform": self.platform,
            "cocotb": {"version": self.cocotb_version, "dist": self.cocotb_dist},
            "simulator": self.simulator,
            "shim": self.shim,
        }
        # Absent (not null) outside our container, so a user's key does not
        # depend on whether the field exists in their version of the schema.
        if self.image is not None:
            out["image"] = self.image
        return out

    @classmethod
    def from_json(cls, obj: object) -> Toolchain:
        o = _check_keys(obj, "toolchain", {"python", "platform", "cocotb", "simulator", "shim"}, {"image"})
        c = _check_keys(o["cocotb"], "toolchain.cocotb", {"version", "dist"})
        return cls(o["python"], o["platform"], c["version"], c["dist"], o["simulator"], o["shim"], o.get("image"))


@dataclass(frozen=True)
class Outcome:
    """How the phase's ``_execute`` ended."""

    ok: bool
    exception: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        _check_bool(self.ok, "outcome.ok")
        if self.ok:
            if self.exception is not None or self.message is not None:
                raise ManifestError("outcome: a successful outcome has no exception")
        else:
            _check_str(self.exception, "outcome.exception")
            _check_str(self.message, "outcome.message")

    def to_json(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True}
        return {"ok": False, "exception": self.exception, "message": self.message}

    @classmethod
    def from_json(cls, obj: object) -> Outcome:
        o = _check_keys(obj, "outcome", {"ok"}, {"exception", "message"})
        return cls(o["ok"], o.get("exception"), o.get("message"))


# --- manifests ----------------------------------------------------------------


def _check_phase(phase: object) -> str:
    if phase not in PHASES:
        raise ManifestError(f"phase must be one of {PHASES}, got {phase!r}")
    return phase  # type: ignore[return-value]


@dataclass(frozen=True)
class SrcManifest:
    """Everything that determines a phase's result; its hash is the cache key."""

    phase: str
    runner: str
    cmds: tuple[tuple[str, ...], ...]
    cwd: str
    env: Mapping[str, str]
    inputs: tuple[InputFile, ...]
    toolchain: Toolchain
    build: str | None = None
    cache_inputs: tuple[str, ...] = ()
    cmdfiles: tuple[str, ...] = ()
    cache_env_whitelist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_phase(self.phase)
        _check_str(self.runner, "runner")
        _check_str(self.cwd, "cwd")
        # Normalise containers so equal manifests compare and hash equal.
        object.__setattr__(self, "cmds", tuple(tuple(c) for c in self.cmds))
        for i, cmd in enumerate(self.cmds):
            if not cmd:
                raise ManifestError(f"cmds[{i}]: empty command")
            for arg in cmd:
                _check_str(arg, f"cmds[{i}][]")
        env = dict(self.env)
        for k, v in env.items():
            _check_str(k, "env key")
            _check_str(v, f"env[{k}]")
        object.__setattr__(self, "env", env)
        inputs = tuple(sorted(self.inputs, key=lambda f: f.path))
        _unique(((f.path,) for f in inputs), "inputs")
        object.__setattr__(self, "inputs", inputs)
        if self.phase == "test":
            _check_digest(self.build, "build")
        elif self.build is not None:
            raise ManifestError("build: only a test-phase manifest refers to a build")
        # Order and duplicates of the user's patterns do not change which files
        # match, so they must not change the key: sorted and de-duplicated.
        for pattern in self.cache_inputs:
            _check_str(pattern, "cache_inputs[]")
        object.__setattr__(self, "cache_inputs", tuple(sorted(set(self.cache_inputs))))
        if self.phase == "test" and self.cache_inputs:
            raise ManifestError("cache_inputs: only a build-phase manifest has user inputs")
        # Like cache_inputs: order and duplicates do not change the meaning.
        for pattern in self.cache_env_whitelist:
            _check_str(pattern, "cache_env_whitelist[]")
        object.__setattr__(self, "cache_env_whitelist", tuple(sorted(set(self.cache_env_whitelist))))
        if self.phase == "test" and self.cache_env_whitelist:
            raise ManifestError("cache_env_whitelist: only a build-phase manifest has it")
        # Command files hashed by content, in processing order (order matters).
        object.__setattr__(self, "cmdfiles", tuple(self.cmdfiles))
        for digest in self.cmdfiles:
            _check_digest(digest, "cmdfiles[]")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": SCHEMA_SRC,
            "phase": self.phase,
            "runner": self.runner,
            "cmds": [list(c) for c in self.cmds],
            "cwd": self.cwd,
            "env": dict(self.env),
            "inputs": [f.to_json() for f in self.inputs],
            "cmdfiles": list(self.cmdfiles),
            "toolchain": self.toolchain.to_json(),
        }
        if self.build is not None:
            out["build"] = self.build
        if self.phase == "build":
            out["cache_inputs"] = list(self.cache_inputs)
            out["cache_env_whitelist"] = list(self.cache_env_whitelist)
        return out

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_json())

    def key(self) -> str:
        """The cache key: SHA-256 of the canonical JSON encoding."""
        return canonical_hash(self.to_json())

    def blobs(self) -> set[str]:
        """Blob digests this manifest refers to (stored only optionally)."""
        return {f.sha256 for f in self.inputs}

    @classmethod
    def from_json(cls, obj: object) -> SrcManifest:
        o = _check_keys(
            obj,
            "src manifest",
            {"schema", "phase", "runner", "cmds", "cwd", "env", "inputs", "cmdfiles", "toolchain"},
            {"build", "cache_inputs", "cache_env_whitelist"},
        )
        if o["schema"] != SCHEMA_SRC:
            raise ManifestError(f"schema: expected {SCHEMA_SRC!r}, got {o['schema']!r}")
        if not isinstance(o["cmds"], list) or not all(isinstance(c, list) for c in o["cmds"]):
            raise ManifestError("cmds: must be a list of lists")
        if not isinstance(o["env"], Mapping):
            raise ManifestError("env: must be an object")
        if not isinstance(o["inputs"], list):
            raise ManifestError("inputs: must be a list")
        for field_name in ("cache_inputs", "cache_env_whitelist"):
            if o["phase"] == "build" and field_name not in o:
                raise ManifestError(f"{field_name}: missing in a build-phase manifest")
            if not isinstance(o.get(field_name, []), list):
                raise ManifestError(f"{field_name}: must be a list")
        if not isinstance(o.get("cache_inputs", []), list):
            raise ManifestError("cache_inputs: must be a list")
        if not isinstance(o["cmdfiles"], list):
            raise ManifestError("cmdfiles: must be a list")
        return cls(
            phase=o["phase"],
            runner=o["runner"],
            cmds=tuple(tuple(c) for c in o["cmds"]),
            cwd=o["cwd"],
            env=o["env"],
            inputs=tuple(InputFile.from_json(f) for f in o["inputs"]),
            toolchain=Toolchain.from_json(o["toolchain"]),
            build=o.get("build"),
            cache_inputs=tuple(o.get("cache_inputs", ())),
            cache_env_whitelist=tuple(o.get("cache_env_whitelist", ())),
            cmdfiles=tuple(o["cmdfiles"]),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> SrcManifest:
        return cls.from_json(_load(data))


@dataclass(frozen=True)
class ResultManifest:
    """What a phase produced; stored under the key of its SRC manifest."""

    src: str
    phase: str
    outputs: tuple[OutputFile, ...]
    outcome: Outcome
    dirs: tuple[OutputDir, ...] = ()
    console: str | None = None
    info: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_digest(self.src, "src")
        _check_phase(self.phase)
        outputs = tuple(sorted(self.outputs, key=lambda f: (f.base, f.path)))
        _unique(((f.base, f.path) for f in outputs), "outputs")
        object.__setattr__(self, "outputs", outputs)
        dirs = tuple(sorted(self.dirs, key=lambda d: (d.base, d.path)))
        _unique(((d.base, d.path) for d in dirs), "dirs")
        object.__setattr__(self, "dirs", dirs)
        if self.console is not None:
            _check_digest(self.console, "console")
        if not isinstance(self.info, Mapping):
            raise ManifestError("info: must be an object")
        info = dict(self.info)
        canonical_json(info)  # info must be plain JSON, too
        object.__setattr__(self, "info", info)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_RESULT,
            "src": self.src,
            "phase": self.phase,
            "outputs": [f.to_json() for f in self.outputs],
            "dirs": [d.to_json() for d in self.dirs],
            "console": self.console,
            "outcome": self.outcome.to_json(),
            "info": dict(self.info),
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_json())

    def blobs(self) -> set[str]:
        """Blob digests a restore needs: every output and the console."""
        out = {f.sha256 for f in self.outputs}
        if self.console is not None:
            out.add(self.console)
        return out

    @classmethod
    def from_json(cls, obj: object) -> ResultManifest:
        o = _check_keys(
            obj, "result manifest", {"schema", "src", "phase", "outputs", "dirs", "console", "outcome", "info"}
        )
        if o["schema"] != SCHEMA_RESULT:
            raise ManifestError(f"schema: expected {SCHEMA_RESULT!r}, got {o['schema']!r}")
        if not isinstance(o["outputs"], list) or not isinstance(o["dirs"], list):
            raise ManifestError("outputs and dirs must be lists")
        return cls(
            src=o["src"],
            phase=o["phase"],
            outputs=tuple(OutputFile.from_json(f) for f in o["outputs"]),
            dirs=tuple(OutputDir.from_json(d) for d in o["dirs"]),
            console=o["console"],
            outcome=Outcome.from_json(o["outcome"]),
            info=o["info"],
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ResultManifest:
        return cls.from_json(_load(data))


def _load(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError(f"not valid UTF-8 JSON: {e}") from e
