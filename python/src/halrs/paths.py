"""Locations of the hal-rs checkout, its submodules and build outputs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HALUCINATOR_SRC = Path(os.environ.get("HALRS_HALUCINATOR", REPO / "extern/halucinator/src"))
BUILD = Path(os.environ.get("HALRS_BUILD", REPO / "build"))


def qemu_binary(arch: str = "arm") -> Path:
    return Path(os.environ.get("HALRS_QEMU", BUILD / "qemu-build" / f"qemu-system-{arch}"))


def plugin_library() -> Path:
    if "HALRS_PLUGIN" in os.environ:
        return Path(os.environ["HALRS_PLUGIN"])
    for profile in ("release", "debug"):
        for root in (BUILD / "cargo", REPO / "target"):
            p = root / profile / "libhalrs.so"
            if p.exists():
                return p
    return REPO / "target/release/libhalrs.so"


def use_halucinator() -> None:
    """Make the HALucinator submodule importable (it is used in place, not installed)."""
    from . import compat  # noqa: F401  (restores APIs HALucinator needs)

    p = str(HALUCINATOR_SRC)
    if p not in sys.path:
        sys.path.insert(0, p)
