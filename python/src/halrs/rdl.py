"""SystemRDL -> hal-rs IR peripherals (via ``systemrdl-compiler``).

Each child ``addrmap``/``regfile`` of the top addrmap becomes a peripheral
window; a top addrmap that holds registers directly is one peripheral. SystemRDL
has no interrupt numbers, so an optional user property ``halrs_irq`` (declared
by this module) on a peripheral supplies its NVIC line.
"""

from __future__ import annotations

from pathlib import Path

from systemrdl import RDLCompiler
from systemrdl.component import Addrmap
from systemrdl.node import AddrmapNode, FieldNode, RegfileNode, RegNode
from systemrdl.rdltypes import OnReadType, OnWriteType
from systemrdl.udp import UDPDefinition

from .hints import hint_for, mirror_hints
from .ir import Access, Field, Peripheral, Register


class _HalrsIrq(UDPDefinition):
    name = "halrs_irq"
    valid_components = {Addrmap}
    valid_type = int


def _access(f: FieldNode) -> Access:
    if f.get_property("onread") == OnReadType.rclr:
        return Access.READ_CLEAR
    if f.get_property("onwrite") == OnWriteType.woclr:
        return Access.W1C
    r, w = f.is_sw_readable, f.is_sw_writable
    if r and not w:
        return Access.READ_ONLY
    if w and not r:
        return Access.WRITE_ONLY
    return Access.READ_WRITE


def _register(reg: RegNode, base: int) -> Register:
    fields = [
        Field(name=f.inst_name, lsb=f.low, width=f.width, access=_access(f),
              hint=hint_for(reg.inst_name, f.inst_name))
        for f in reg.fields()
    ]
    mirror_hints(fields)
    reset = 0
    for f in reg.fields():
        r = f.get_property("reset")
        if isinstance(r, int):
            reset |= (r & ((1 << f.width) - 1)) << f.low
    return Register(name=reg.inst_name, offset=reg.absolute_address - base,
                    size=reg.get_property("regwidth"), reset=reset, fields=fields)


def _peripheral(node, name: str) -> Peripheral:
    base = node.absolute_address
    regs = [_register(r, base) for r in node.descendants(unroll=True)
            if isinstance(r, RegNode)]
    irq = node.get_property("halrs_irq", default=None) if isinstance(node, AddrmapNode) else None
    return Peripheral(name=name, base=base, size=node.total_size,
                      irqs=[irq] if irq is not None else [], registers=regs)


def load_rdl(path: Path, top: str | None = None) -> list[Peripheral]:
    rdlc = RDLCompiler()
    rdlc.register_udp(_HalrsIrq, soft=False)
    rdlc.compile_file(str(path))
    root = rdlc.elaborate(top_def_name=top).top

    blocks = [c for c in root.children(unroll=True) if isinstance(c, (AddrmapNode, RegfileNode))]
    if not blocks:
        return [_peripheral(root, root.inst_name)]
    return [_peripheral(b, b.inst_name) for b in blocks]
