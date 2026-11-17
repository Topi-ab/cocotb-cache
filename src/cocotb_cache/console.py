"""Console capture and replay (docs/format.md).

On a miss the simulator's output must reach the user as usual and be stored.
:func:`tee` redirects file descriptors 1 and 2 through pipes for the duration
of the block: every byte read is written on to the original descriptor at
once and recorded. Child processes (the simulator) inherit the redirected
descriptors, so their output is captured too.

The record is one stream of ``(fd, bytes)`` chunks in the order they were
read, so stdout and stderr keep their relative order as far as two pipes
allow. It is stored as JSON Lines ``{"fd": 1|2, "b64": "..."}``; on a hit
:func:`replay` writes the chunks to the same descriptors in the same order.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

CHUNK = 65536


class Recording:
    """Chunks captured by :func:`tee`."""

    def __init__(self) -> None:
        self.chunks: list[tuple[int, bytes]] = []
        self._lock = threading.Lock()

    def add(self, fd: int, data: bytes) -> None:
        with self._lock:
            self.chunks.append((fd, data))

    def to_bytes(self) -> bytes:
        """The JSON Lines encoding stored as the console blob."""
        return b"".join(
            json.dumps({"fd": fd, "b64": base64.b64encode(data).decode("ascii")}, separators=(",", ":")).encode() + b"\n"
            for fd, data in self.chunks
        )

    def is_empty(self) -> bool:
        return not self.chunks


def parse(blob: bytes) -> list[tuple[int, bytes]]:
    """Chunks from a console blob (inverse of :meth:`Recording.to_bytes`)."""
    out = []
    for line in blob.splitlines():
        if line.strip():
            rec = json.loads(line)
            fd = int(rec["fd"])
            if fd not in (1, 2):
                raise ValueError(f"bad fd {fd} in console record")
            out.append((fd, base64.b64decode(rec["b64"])))
    return out


def _flush_python_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _pump(read_fd: int, target_fd: int, fd_id: int, rec: Recording) -> None:
    while True:
        try:
            data = os.read(read_fd, CHUNK)
        except OSError:
            break
        if not data:
            break
        rec.add(fd_id, data)
        view = memoryview(data)
        while view:
            try:
                written = os.write(target_fd, view)
            except OSError:
                break
            view = view[written:]
    os.close(read_fd)


@contextmanager
def tee() -> Iterator[Recording]:
    """Capture fds 1 and 2 while passing everything through to them."""
    rec = Recording()
    _flush_python_streams()
    saved = {fd: os.dup(fd) for fd in (1, 2)}
    threads = []
    write_ends = []
    try:
        for fd in (1, 2):
            r, w = os.pipe()
            os.dup2(w, fd)
            write_ends.append(w)
            t = threading.Thread(target=_pump, args=(r, saved[fd], fd, rec), daemon=True)
            t.start()
            threads.append(t)
        yield rec
    finally:
        _flush_python_streams()
        for fd in (1, 2):
            os.dup2(saved[fd], fd)  # also drops fd's reference to the pipe
        for w in write_ends:
            os.close(w)
        for t in threads:
            t.join(timeout=30)
        for fd in saved.values():
            os.close(fd)


def replay(chunks: list[tuple[int, bytes]]) -> None:
    """Write recorded chunks to fds 1 and 2 in their original order."""
    _flush_python_streams()
    for fd, data in chunks:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
