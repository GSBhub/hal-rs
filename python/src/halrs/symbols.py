"""Tier-1 matching: function symbols from an unstripped ELF -> addrs.yaml.

Same output as HALucinator's ``hal_make_addr`` (every function symbol, Thumb bit
cleared, keyed by address with the last symbol at an address winning), but via
pyelftools instead of angr.
"""

from __future__ import annotations

from pathlib import Path

from elftools.elf.elffile import ELFFile


def elf_functions(path: Path) -> dict[int, str]:
    with open(path, "rb") as f:
        elf = ELFFile(f)
        symtab = elf.get_section_by_name(".symtab")
        if symtab is None:
            return {}
        by_name = {s.name: s["st_value"] & ~1 for s in symtab.iter_symbols()
                   if s["st_info"]["type"] == "STT_FUNC" and s.name}
    return {addr: name for name, addr in by_name.items()}
