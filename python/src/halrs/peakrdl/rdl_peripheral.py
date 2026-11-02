"""Register-file peripheral for HALucinator/avatar2, driven by a register table.

``peakrdl halucinator`` copies this file into every model it generates (so the
output only needs avatar2) and adds one subclass per RDL peripheral. Behaviour
matches hal-rs's Rust model (crates/halrs-core): reset values, per-field access
(read-only, write-only, write-1-to-clear, read-to-clear) and the status-bit hints
from halrs.hints, so polling loops on RDY/BSY-style bits complete.

Customise a peripheral by subclassing its generated class in another module:

    class UART0(gen.UART0):
        def read_DR(self, value):          # called with the modelled value
            return ord(self.rx.pop(0)) if self.rx else 0
        def write_DR(self, value, old):    # called after the write is stored
            print(chr(value & 0xFF), end="")
"""

from avatar2.peripherals.avatar_peripheral import AvatarPeripheral

RW, RO, WO, W1C, RC = "read-write", "read-only", "write-only", "w1c", "read-clear"
POLL_SET, POLL_CLEAR = "poll-set", "poll-clear"


class RdlPeripheral(AvatarPeripheral):
    # offset -> (name, size in bytes, reset, fields); a field is
    # (name, lsb, width, access, hint) and hint is None, POLL_SET, POLL_CLEAR or
    # the int lsb of the field it mirrors.
    REGISTERS = {}
    IRQS = ()

    def __init__(self, name, address, size, **kwargs):
        AvatarPeripheral.__init__(self, name, address, size)
        self._names = {r[0]: off for off, r in self.REGISTERS.items()}
        self.reset()
        self.read_handler[0:size] = self.hw_read
        self.write_handler[0:size] = self.hw_write

    def reset(self):
        self.values = {off: r[2] for off, r in self.REGISTERS.items()}
        self.scratch = {}   # offsets no register describes: plain storage

    def __getitem__(self, reg):
        return self.values[self._names[reg]]

    def __setitem__(self, reg, value):
        """Set a register from the device side (no access rules, no hooks)."""
        self.values[self._names[reg]] = value

    def _find(self, offset):
        for off, r in self.REGISTERS.items():
            if off <= offset < off + r[1]:
                return off, r
        return None, None

    def hw_read(self, offset, size, pc=0):
        off, r = self._find(offset)
        if r is None:
            word = self.scratch.get(offset & ~3, 0)
            return (word >> (offset & 3) * 8) & ((1 << (size * 8)) - 1)
        name, _, _, fields = r
        value = self.values[off]
        out = value
        for _, lsb, width, access, hint in fields:
            mask = ((1 << width) - 1) << lsb
            if access == WO:
                out &= ~mask
            if hint == POLL_SET:
                out |= mask
            elif hint == POLL_CLEAR:
                out &= ~mask
            elif isinstance(hint, int):
                out = (out & ~mask) | (((value >> hint) << lsb) & mask)
            if access == RC:
                self.values[off] &= ~mask
        hook = getattr(self, "read_" + name, None)
        if hook is not None:
            out = hook(out)
        shift = (offset - off) * 8
        return (out >> shift) & ((1 << (size * 8)) - 1)

    def hw_write(self, offset, size, value, pc=0):
        off, r = self._find(offset)
        if r is None:
            shift, mask = (offset & 3) * 8, (1 << (size * 8)) - 1
            word = self.scratch.get(offset & ~3, 0)
            self.scratch[offset & ~3] = (word & ~(mask << shift)) | ((value & mask) << shift)
            return True
        name, _, _, fields = r
        shift = (offset - off) * 8
        lane = ((1 << (size * 8)) - 1) << shift
        value = (value << shift) & lane
        old = self.values[off]
        new = (old & ~lane) | value
        for _, lsb, width, access, _ in fields:
            mask = ((1 << width) - 1) << lsb & lane
            if access == RO:
                new = (new & ~mask) | (old & mask)
            elif access == W1C:
                new = (new & ~mask) | (old & mask & ~value)
        self.values[off] = new
        hook = getattr(self, "write_" + name, None)
        if hook is not None:
            hook(new, old)
        return True


class RdlBus(AvatarPeripheral):
    """One HALucinator memory window holding several peripherals.

    HALucinator (avatar2) wants memory regions in whole 4 KB pages, while e.g.
    STM32 peripherals are 1 KB apart, so the memory config maps page-aligned
    windows to a bus that routes each access to the peripheral it falls in.
    Offsets no peripheral covers are plain storage. ``bus["USART1"]`` is that
    peripheral's model. To swap in your own models, subclass with a different
    ``PERIPHERALS`` ({name: (class, base, size)}).
    """
    PERIPHERALS = {}

    def __init__(self, name, address, size, **kwargs):
        AvatarPeripheral.__init__(self, name, address, size)
        self.devices = {n: cls(n, base, sz) for n, (cls, base, sz) in self.PERIPHERALS.items()
                        if address <= base < address + size}
        self.scratch = {}
        self.read_handler[0:size] = self.hw_read
        self.write_handler[0:size] = self.hw_write

    def __getitem__(self, name):
        return self.devices[name]

    def _device(self, offset):
        addr = self.address + offset
        for d in self.devices.values():
            if d.address <= addr < d.address + d.size:
                return d, addr - d.address
        return None, None

    def hw_read(self, offset, size, pc=0):
        d, off = self._device(offset)
        if d is not None:
            return d.hw_read(off, size, pc)
        word = self.scratch.get(offset & ~3, 0)
        return (word >> (offset & 3) * 8) & ((1 << (size * 8)) - 1)

    def hw_write(self, offset, size, value, pc=0):
        d, off = self._device(offset)
        if d is not None:
            return d.hw_write(off, size, value, pc)
        shift, mask = (offset & 3) * 8, (1 << (size * 8)) - 1
        word = self.scratch.get(offset & ~3, 0)
        self.scratch[offset & ~3] = (word & ~(mask << shift)) | ((value & mask) << shift)
        return True
