"""Lethe - Autonomous executive assistant with local memory and LLM."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("lethe")
except PackageNotFoundError:  # editable install before metadata is built
    __version__ = "0.0.0+unknown"
