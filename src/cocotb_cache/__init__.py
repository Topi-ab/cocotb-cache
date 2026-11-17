"""Transparent S3-backed build and test cache for the cocotb Python runner.

The package hooks into ``cocotb_tools.runner`` at import time; user code does
not change. See docs/user-guide.md and docs/format.md in the repository.
"""

# Part of the cache key: changing it invalidates every cached result.
__version__ = "0.1.0"

__all__ = ["__version__"]
