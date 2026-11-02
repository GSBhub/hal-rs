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


OPT = Path(os.environ.get("HALRS_OPT", Path.home() / ".local/opt"))
STM32 = REPO / "extern/stm32"


def _newest(pattern: str) -> Path | None:
    hits = sorted(OPT.glob(pattern))
    return hits[-1] if hits else None


def ghidra_install() -> Path:
    """GHIDRA_INSTALL_DIR, else the newest ~/.local/opt/ghidra_*."""
    p = os.environ.get("GHIDRA_INSTALL_DIR") or _newest("ghidra_*_PUBLIC")
    if not p:
        raise SystemExit("halrs: Ghidra not found; set GHIDRA_INSTALL_DIR")
    return Path(p)


def java_home() -> Path | None:
    p = os.environ.get("JAVA_HOME") or _newest("jdk-21*")
    return Path(p) if p else None


def toolchains() -> dict[str, Path]:
    """arm-none-eabi-gcc binaries by short name (gcc49, gcc15, ...).

    HALRS_TOOLCHAINS="name=/path/to/arm-none-eabi-gcc:..." overrides discovery.
    """
    if env := os.environ.get("HALRS_TOOLCHAINS"):
        return {k: Path(v) for k, v in (item.split("=", 1) for item in env.split(":"))}
    import subprocess

    found = {}
    for gcc in sorted(OPT.glob("*/bin/arm-none-eabi-gcc")):
        ver = subprocess.run([gcc, "-dumpversion"], capture_output=True, text=True).stdout
        major, minor = (ver.strip().split(".") + ["0"])[:2]
        # Before GCC 5 the minor version was part of the release name (4.9).
        found["gcc" + (major + minor if int(major) < 5 else major)] = gcc
    return found
