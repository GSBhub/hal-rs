"""Status-bit heuristics: which fields firmware polls, and for what value.

Assigned statically from field names (the P2IM / uEmu idea, done up front);
the Rust model's poll-loop detector covers whatever these miss. Deliberately
conservative: forcing a bit like RXNE would make firmware read garbage data.
"""

from __future__ import annotations

import re

from .ir import Access, Field, Hint, Mirror

_SET = re.compile(r"^(.*RDY|.*READY|TXE|TC|TXIS|TCF|EOC|JEOC|DONE|.*_DONE|PLLLOCK|LOCKED)$")
_CLEAR = re.compile(r"^(BSY|BUSY|.*_BUSY|CMDACT)$")
# Only fields in registers that look like status/control registers get hints.
_MIRROR = re.compile(r"^(.+)S(\d*)$")
_STATUS_REG = re.compile(r"(SR|ISR|STAT|STATUS|FLAG|CSR|^CR\d?$|^CFGR$)", re.I)


def hint_for(register: str, field: str) -> Hint | None:
    if not _STATUS_REG.search(register):
        return None
    f = field.upper()
    if _SET.match(f):
        return Hint.POLL_SET
    if _CLEAR.match(f):
        return Hint.POLL_CLEAR
    return None


def mirror_hints(fields: list[Field]) -> None:
    """Mark read-only ``<X>S`` fields that report control field ``<X>``.

    e.g. RCC_CFGR.SWS mirrors SW (or SWS1/SWS0 mirror SW1/SW0 when the SVD
    splits them into bits), so ``HAL_RCC_ClockConfig``'s wait for the clock
    switch to take effect completes.
    """
    by_name = {f.name.upper(): f for f in fields}
    for f in fields:
        n = f.name.upper()
        # "Ready follows enable": PLLRDY <- PLLON, HSERDY <- HSEON, ...
        on = by_name.get(n[:-3] + "ON") if n.endswith("RDY") else None
        if on is not None and on.width == f.width == 1:
            f.hint = Mirror(mirror=on.lsb)
            continue
        m = _MIRROR.match(n)
        src = by_name.get(m.group(1) + m.group(2)) if m else None
        if (src is not None and f.hint is None and f.access == Access.READ_ONLY
                and src.width == f.width and src is not f):
            f.hint = Mirror(mirror=src.lsb)
