"""Tier-3 naming: vector tables and call-graph context (LibMatch-style).

Both work on plain data so they're testable without Ghidra:

- :func:`vector_names` names Cortex-M exception handlers by vector slot and
  device IRQ handlers from SVD interrupt names (``USART1`` -> ``USART1_IRQHandler``).
- :func:`propagate` spreads names from anchors (FunctionID hits, vectors)
  through the call graph. Each reference function has one or more known
  ordered callee sequences; an anchored target function whose callee list has
  the same length and agrees at every already-named position names its other
  callees (downward), and an unnamed function whose named callees pin down a
  single reference function gets that name (upward). Proposals must be
  unanimous and never reuse a name, iterating to a fixpoint.

  Optimisation levels lay code out differently, so one function can have
  several callee orders across reference builds (HAL_UART_Init calls
  MspInit then SetConfig at -O0, the reverse at -O2), and unanimity then
  refuses to pick. ``preferred`` holds the sequences of the builds the
  firmware most likely came from; they're used whenever one of them fits.
"""

from __future__ import annotations

import struct
from collections import defaultdict

# ARMv7-M exception numbers -> CMSIS handler names.
CORE_VECTORS = {
    1: "Reset_Handler", 2: "NMI_Handler", 3: "HardFault_Handler", 4: "MemManage_Handler",
    5: "BusFault_Handler", 6: "UsageFault_Handler", 11: "SVC_Handler",
    12: "DebugMon_Handler", 14: "PendSV_Handler", 15: "SysTick_Handler",
}


def vector_names(image: bytes, base: int, irq_names: dict[int, str] | None = None,
                 num_irqs: int = 240) -> dict[int, str]:
    """addr -> handler name for vectors that point at a handler used by exactly
    one slot (shared Default_Handler stubs are left unnamed)."""
    slots: dict[int, list[str]] = defaultdict(list)
    for n in range(1, min(16 + num_irqs, len(image) // 4)):
        (v,) = struct.unpack_from("<I", image, 4 * n)
        addr = v & ~1
        if not (v & 1) or not (base <= addr < base + len(image)):
            continue
        if n < 16:
            name = CORE_VECTORS.get(n)
        else:
            irq = (irq_names or {}).get(n - 16)
            name = f"{irq}_IRQHandler" if irq else None
        slots[addr].append(name)
    return {a: ns[0] for a, ns in slots.items() if len(ns) == 1 and ns[0]}


def _same(a: str, b: str) -> bool:
    # CMSIS renamed NVIC_* helpers to __NVIC_* between versions.
    return a == b or a.lstrip("_") == b.lstrip("_")


def propagate(anchors: dict[int, str], target_calls: dict[int, list[int]],
              ref_calls: dict[str, list[list[str]]], max_rounds: int = 20,
              preferred: dict[str, list[list[str]]] | None = None) -> dict[int, str]:
    """Return new names (addr -> name) inferred from `anchors`.

    For each reference name, sequences from `preferred` are tried first and
    all of `ref_calls` only if none of the preferred ones fit."""
    preferred = preferred or {}
    known = dict(anchors)
    used = set(known.values())
    found: dict[int, str] = {}

    def consistent(seq_t: list[int], seq_r: list[str]) -> bool:
        return len(seq_t) == len(seq_r) and all(
            _same(known[t], r) for t, r in zip(seq_t, seq_r) if t in known)

    def fitting(seq_t: list[int], name: str) -> list[list[str]]:
        for table in (preferred, ref_calls):
            if refs := [r for r in table.get(name, []) if consistent(seq_t, r)]:
                return refs
        return []

    for _ in range(max_rounds):
        proposals: dict[int, set[str]] = defaultdict(set)

        # Downward: callees of named functions.
        for addr, name in list(known.items()):
            seq_t = target_calls.get(addr, [])
            refs = fitting(seq_t, name)
            for r in refs:
                for t, rn in zip(seq_t, r):
                    if t not in known:
                        proposals[t].add(rn)

        # Upward: unnamed functions whose named callees fit one reference.
        for addr, seq_t in target_calls.items():
            if addr in known or sum(t in known for t in seq_t) < 2:
                continue
            names = {n for n in ref_calls if fitting(seq_t, n)}
            if len(names) == 1:
                proposals[addr] |= names

        new = {a: next(iter(ns)) for a, ns in proposals.items()
               if len(ns) == 1 and next(iter(ns)) not in used}
        # A name proposed for two addresses this round is ambiguous: drop it.
        counts = defaultdict(int)
        for n in new.values():
            counts[n] += 1
        new = {a: n for a, n in new.items() if counts[n] == 1}
        if not new:
            break
        known.update(new)
        found.update(new)
        used.update(new.values())
    return found
