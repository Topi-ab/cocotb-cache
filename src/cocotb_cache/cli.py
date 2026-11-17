"""``cocotb-cache`` command line: inspect, stat and gc.

Configuration is the same as the cache's: ``COCOTB_CACHE_S3_BUCKET``,
``COCOTB_CACHE_S3_PREFIX`` and the standard ``AWS_*`` variables (or
``--bucket`` / ``--prefix``).

* ``inspect KEY``: the SRC and RESULT manifests of a cache key; a unique
  prefix of the key is enough (at least 4 hex characters).
* ``stat``: number and size of stored objects per kind.
* ``gc``: mark and sweep. Every blob referenced by a manifest is kept; other
  blobs, and SRC manifests without a RESULT, older than ``--min-age`` hours
  are garbage. Without ``--delete`` it only reports what it would delete.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__

MIN_KEY_PREFIX = 4


def _store(args: argparse.Namespace) -> Any:
    from .store import Store, make_client

    bucket = args.bucket or os.environ.get("COCOTB_CACHE_S3_BUCKET", "")
    if not bucket:
        raise SystemExit("cocotb-cache: no bucket (set COCOTB_CACHE_S3_BUCKET or use --bucket)")
    prefix = args.prefix if args.prefix is not None else os.environ.get("COCOTB_CACHE_S3_PREFIX", "")
    return Store(make_client(), bucket, prefix)


def _size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# --- inspect ----------------------------------------------------------------------------


def _resolve_key(store: Any, start: str) -> str:
    start = start.lower()
    if len(start) < MIN_KEY_PREFIX or any(c not in "0123456789abcdef" for c in start):
        raise SystemExit(f"cocotb-cache: give at least {MIN_KEY_PREFIX} hex characters of a key")
    matches = sorted({o.digest for kind in ("src", "result") for o in store.list_objects(kind, start)})
    if not matches:
        raise SystemExit(f"cocotb-cache: no manifest with a key starting {start}")
    if len(matches) > 1:
        raise SystemExit(f"cocotb-cache: {start} is ambiguous: " + ", ".join(m[:12] for m in matches[:10]))
    return matches[0]


def cmd_inspect(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    key = _resolve_key(store, args.key)
    src = store.get_src(key)
    result = store.get_result(key)
    if args.json:
        json.dump({"key": key, "src": src.to_json() if src else None, "result": result.to_json() if result else None},
                  out, indent=2, sort_keys=True)
        out.write("\n")
        return 0
    print(f"key      {key}", file=out)
    if src is None:
        print("src      (not stored)", file=out)
    else:
        tc = src.toolchain
        print(f"phase    {src.phase}", file=out)
        print(f"runner   {src.runner}", file=out)
        if src.build:
            print(f"build    {src.build}", file=out)
        print(f"inputs   {len(src.inputs)} files, {_size(sum(f.size for f in src.inputs))}", file=out)
        for f in src.inputs[: args.limit]:
            print(f"           {f.path}  {f.sha256[:12]}  {_size(f.size)}", file=out)
        if len(src.inputs) > args.limit:
            print(f"           ... {len(src.inputs) - args.limit} more", file=out)
        print(f"commands {len(src.cmds)}", file=out)
        for cmd in src.cmds:
            print(f"           {' '.join(cmd)}", file=out)
        print(f"env      {len(src.env)} variables", file=out)
        print(f"cocotb   {tc.cocotb_version} ({tc.cocotb_dist[:12]})  python {tc.python}  {tc.platform}", file=out)
        print("tools    " + tc.simulator.split("\n", 1)[0] + ("  ..." if "\n" in tc.simulator else ""), file=out)
        if tc.image:
            print(f"image    {tc.image}", file=out)
    if result is None:
        print("result   (none: never completed, or still being stored)", file=out)
        return 0
    outcome = "ok" if result.outcome.ok else f"failed: {result.outcome.exception}: {result.outcome.message}"
    print(f"outcome  {outcome}", file=out)
    print(f"outputs  {len(result.outputs)} files, {_size(sum(f.size for f in result.outputs))}", file=out)
    for f in result.outputs[: args.limit]:
        exe = "  x" if f.executable else ""
        print(f"           {f.base}:{f.path}  {_size(f.size)}{exe}", file=out)
    if len(result.outputs) > args.limit:
        print(f"           ... {len(result.outputs) - args.limit} more", file=out)
    print(f"console  {'yes' if result.console else 'no'}", file=out)
    for k, v in sorted(result.info.items()):
        print(f"info     {k} = {v}", file=out)
    return 0


# --- stat -------------------------------------------------------------------------------


def cmd_stat(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    total_n = total_bytes = 0
    print(f"store    s3://{store.bucket}/{store.prefix}v1/", file=out)
    for kind in store.KINDS:
        n = size = 0
        for obj in store.list_objects(kind):
            n += 1
            size += obj.size
        total_n += n
        total_bytes += size
        print(f"{kind:8s} {n:8d} objects  {_size(size):>11s}", file=out)
    print(f"{'total':8s} {total_n:8d} objects  {_size(total_bytes):>11s}", file=out)
    return 0


# --- gc ---------------------------------------------------------------------------------


def find_garbage(store: Any, min_age: timedelta, now: datetime | None = None) -> tuple[list[Any], dict[str, int]]:
    """Mark and sweep. Returns (objects to delete, counters)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - min_age
    referenced: set[str] = set()
    results = {o.digest for o in store.list_objects("result")}
    counters = {"results": len(results), "src": 0, "blobs": 0, "unreadable": 0}
    for digest in results:
        r = store.get_result(digest)
        if r is not None:
            referenced |= r.blobs()
    garbage = []
    for obj in store.list_objects("src"):
        counters["src"] += 1
        if obj.digest in results:
            s = store.get_src(obj.digest)
            if s is not None:
                referenced |= s.blobs()
        elif obj.last_modified < cutoff:
            garbage.append(obj)  # an SRC without a RESULT: the run never completed
    for obj in store.list_objects("blobs"):
        counters["blobs"] += 1
        if obj.digest not in referenced and obj.last_modified < cutoff:
            garbage.append(obj)
    return garbage, counters


def cmd_gc(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    garbage, counters = find_garbage(store, timedelta(hours=args.min_age))
    size = sum(o.size for o in garbage)
    print(
        f"scanned  {counters['results']} results, {counters['src']} src, {counters['blobs']} blobs", file=out
    )
    print(f"garbage  {len(garbage)} objects, {_size(size)} (older than {args.min_age:g} h, unreferenced)", file=out)
    for obj in garbage[: args.limit]:
        print(f"           {obj.kind}/{obj.digest[:16]}  {_size(obj.size)}  {obj.last_modified:%Y-%m-%d %H:%M}", file=out)
    if len(garbage) > args.limit:
        print(f"           ... {len(garbage) - args.limit} more", file=out)
    if not args.delete:
        if garbage:
            print("dry run  nothing deleted (use --delete)", file=out)
        return 0
    for obj in garbage:
        store.delete_key(obj.key)
    print(f"deleted  {len(garbage)} objects", file=out)
    return 0


# --- main -------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cocotb-cache", description="Inspect and maintain the cocotb-cache store.")
    parser.add_argument("--version", action="version", version=f"cocotb-cache {__version__}")
    parser.add_argument("--bucket", help="S3 bucket (default: $COCOTB_CACHE_S3_BUCKET)")
    parser.add_argument("--prefix", help="key prefix (default: $COCOTB_CACHE_S3_PREFIX)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="show the manifests of a cache key")
    p.add_argument("key", help="cache key or a unique prefix of it")
    p.add_argument("--json", action="store_true", help="print the raw manifests")
    p.add_argument("--limit", type=int, default=20, help="max files listed per section (default 20)")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("stat", help="number and size of stored objects")
    p.set_defaults(func=cmd_stat)

    p = sub.add_parser("gc", help="remove unreferenced blobs and incomplete runs")
    p.add_argument("--min-age", type=float, default=24.0, help="only objects older than this, in hours (default 24)")
    p.add_argument("--delete", action="store_true", help="really delete (default: only report)")
    p.add_argument("--limit", type=int, default=20, help="max objects listed (default 20)")
    p.set_defaults(func=cmd_gc)
    return parser


def _describe(e: Exception) -> str:
    """A one-line explanation of an S3 or connection error."""
    try:
        from botocore.exceptions import ClientError, NoCredentialsError
    except Exception:  # pragma: no cover
        return f"{type(e).__name__}: {e}"
    if isinstance(e, NoCredentialsError):
        return "no S3 credentials (set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE)"
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        hint = ""
        if status == 403:
            hint = " (access denied: check the credentials and that they allow s3:ListBucket and s3:GetObject)"
        elif err.get("Code") == "NoSuchBucket":
            hint = " (check COCOTB_CACHE_S3_BUCKET)"
        return f"S3 error {status} {err.get('Code', '')}: {err.get('Message', '')}{hint}"
    return f"{type(e).__name__}: {e} (check AWS_ENDPOINT_URL and the network)"


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, out or sys.stdout)
    except SystemExit:
        raise
    except Exception as e:
        print(f"cocotb-cache: {_describe(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
