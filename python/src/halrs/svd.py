"""CMSIS-SVD -> hal-rs IR peripherals (via the ``cmsis-svd`` parser)."""

from __future__ import annotations

from pathlib import Path

from cmsis_svd import SVDParser

from .hints import hint_for, mirror_hints
from .ir import Access, Field, Peripheral, Register


def _access(f, reg) -> Access:
    mwv = getattr(f, "modified_write_values", None) or getattr(reg, "modified_write_values", None)
    if mwv is not None and "ONE_TO_CLEAR" in str(mwv).upper():
        return Access.W1C
    ra = getattr(f, "read_action", None) or getattr(reg, "read_action", None)
    if ra is not None and str(ra).upper().endswith("CLEAR"):
        return Access.READ_CLEAR
    acc = str(f.access or reg.access or "").upper()
    if "READ_ONLY" in acc:
        return Access.READ_ONLY
    if "WRITE_ONLY" in acc or "WRITE_ONCE" == acc.rsplit(".", 1)[-1]:
        return Access.WRITE_ONLY
    return Access.READ_WRITE


def _registers(items, base_offset: int = 0) -> list[Register]:
    """Flatten registers, clusters and (already expanded) arrays."""
    out: list[Register] = []
    for r in items or []:
        nested = getattr(r, "registers", None)
        if nested is not None and not hasattr(r, "fields"):
            # Cluster (or an array container): offsets are relative to it.
            out += _registers(nested, base_offset + (r.address_offset or 0))
            continue
        if getattr(r, "fields", None) is None and getattr(r, "address_offset", None) is None:
            continue
        name = r.name
        fields = [
            Field(name=f.name, lsb=f.bit_offset, width=f.bit_width,
                  access=_access(f, r), hint=hint_for(name, f.name))
            for f in (r.fields or [])
        ]
        mirror_hints(fields)
        out.append(Register(
            name=name,
            offset=base_offset + (r.address_offset or 0),
            size=r.size or 32,
            reset=r.reset_value or 0,
            fields=fields,
        ))
    return out


def load_svd(path: Path) -> tuple[str | None, list[Peripheral]]:
    """Return ``(cpu_name, peripherals)`` from an SVD file."""
    dev = SVDParser.for_xml_file(str(path)).get_device()
    periphs = []
    for p in dev.peripherals:
        blocks = p.address_blocks or []
        size = max((b.offset + b.size for b in blocks), default=0x400)
        irqs = sorted({i.value for i in (p.interrupts or [])})
        periphs.append(Peripheral(name=p.name, base=p.base_address, size=size, irqs=irqs,
                                  registers=_registers(p.registers)))
    cpu = getattr(getattr(dev, "cpu", None), "name", None)
    return (str(cpu) if cpu else None), periphs


def irq_names(path: Path) -> dict[int, str]:
    """NVIC IRQ number -> interrupt name (for vector-table naming)."""
    dev = SVDParser.for_xml_file(str(path)).get_device()
    return {i.value: i.name for p in dev.peripherals for i in (p.interrupts or [])}
