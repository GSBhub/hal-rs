"""avatar2-target shim so HALucinator handlers run unmodified inside QEMU.

HALucinator's ``bp_handlers`` and ``peripheral_models`` talk to an avatar2
``ARMv7mQemuTarget`` (``qemu.regs.r0``, ``qemu.read_memory(...)``,
``qemu.get_arg(i)``, ``qemu.execute_return(v)``, ``qemu.irq_set(n)``...).
``ShimTarget`` implements that surface on top of ``halrs_native`` -- direct
calls into the libhalrs.so plugin, no GDB/QMP round trip.

The calling-convention helpers (``get_arg``, ``set_arg``, ``get_ret_addr``...)
are taken straight from HALucinator's ``ARMQemuTarget`` so its semantics are
reused, not reimplemented.
"""

from __future__ import annotations

import logging
import struct
from types import SimpleNamespace
from typing import Any

from halucinator.qemu_targets.arm_qemu import ARMQemuTarget

log = logging.getLogger(__name__)

_FMT = {1: "B", 2: "H", 4: "I", 8: "Q"}


class _Regs:
    """``target.regs.<name>`` attribute access, as on avatar2 targets."""

    def __init__(self, target: ShimTarget) -> None:
        object.__setattr__(self, "_t", target)

    def __getattr__(self, name: str) -> int:
        return self._t.read_register(name)

    def __setattr__(self, name: str, value: int) -> None:
        self._t.write_register(name, value)


class FakeAvatar:
    """The slice of ``avatar2.Avatar`` that HALucinator handlers reach for."""

    def __init__(self, config: Any, output_directory: str) -> None:
        self.config = config
        self.output_directory = output_directory
        self.arch = SimpleNamespace(name="ARMV7M")
        self.callables: dict[str, Any] = {}
        self.memory_ranges = None


class ShimTarget:
    # Calling-convention helpers reused verbatim from HALucinator.
    get_arg = ARMQemuTarget.get_arg
    set_arg = ARMQemuTarget.set_arg
    get_ret_addr = ARMQemuTarget.get_ret_addr
    set_ret_addr = ARMQemuTarget.set_ret_addr
    get_symbol_name = ARMQemuTarget.get_symbol_name

    def __init__(self, native: Any, avatar: FakeAvatar) -> None:
        self._n = native
        self.avatar = avatar
        self.name = "halrs"
        self.regs = _Regs(self)
        self._bps: dict[int, int] = {}  # bp number -> address
        self._next_bp = 1
        # Set by the runtime around each dispatch.
        self.current_pc: int | None = None
        self.returned = False

    # -- registers ---------------------------------------------------------
    def read_register(self, name: str) -> int:
        name = name.lower()
        if name == "pc" and self.current_pc is not None:
            # QEMU may not have synced PC mid-TB; we know where we are.
            return self.current_pc | 1
        return self._n.read_reg(name)

    def write_register(self, name: str, value: int) -> None:
        self._n.write_reg(name.lower(), value & 0xFFFFFFFF)

    # -- memory (avatar2 semantics) ---------------------------------------
    def read_memory(self, address: int, size: int, num_words: int = 1, raw: bool = False):
        data = self._n.read_mem(address, size * num_words)
        if raw:
            return data
        words = list(struct.unpack("<%d%s" % (num_words, _FMT[size]), data))
        return words[0] if num_words == 1 else words

    def write_memory(self, address: int, size: int, value, num_words: int = 1,
                     raw: bool = False) -> bool:
        if raw:
            data = value if isinstance(value, (bytes, bytearray)) else bytes(value)
            data = bytes(data[: size * num_words])
        elif num_words == 1:
            data = struct.pack("<" + _FMT[size], value & ((1 << (8 * size)) - 1))
        else:
            data = struct.pack("<%d%s" % (num_words, _FMT[size]), *value)
        return self._n.write_mem(address, data)

    # -- control flow ------------------------------------------------------
    def execute_return(self, ret_value) -> None:
        # The patched entry is a return stub: just set r0, the stub returns.
        if ret_value is not None:
            self.write_register("r0", ret_value)
        self.returned = True

    def cont(self, *args, **kwargs) -> None:
        pass  # execution resumes when the plugin callback returns

    def set_breakpoint(self, address: int, temporary: bool = False, **kwargs) -> int:
        bp = self._next_bp
        self._next_bp += 1
        self._bps[bp] = address & ~1
        return bp

    def set_watchpoint(self, address: int, write: bool = True, read: bool = False) -> int:
        log.warning("halrs: watchpoints are not supported yet (0x%08x)", address)
        return self.set_breakpoint(address)

    def remove_breakpoint(self, bp: int) -> None:
        self._bps.pop(bp, None)

    def breakpoints(self) -> dict[int, int]:
        return dict(self._bps)

    # -- interrupts --------------------------------------------------------
    def irq_set(self, irq_num: int = 1, cpu: int = 0) -> None:
        self._n.set_irq(irq_num, 1)

    def irq_clear(self, irq_num: int = 1, cpu: int = 0) -> None:
        self._n.set_irq(irq_num, 0)

    def irq_pulse(self, irq_num: int = 1, cpu: int = 0) -> None:
        self._n.set_irq(irq_num, 1)
        self._n.set_irq(irq_num, 0)

    def trigger_interrupt(self, interrupt_number: int, cpu_number: int = 0) -> None:
        self.irq_pulse(interrupt_number, cpu_number)

    def set_vector_table_base(self, base: int, cpu_number: int = 0) -> None:
        self.write_memory(0xE000ED08, 4, base)  # SCB->VTOR

    def enable_interrupt(self, interrupt_number: int, cpu_number: int = 0) -> None:
        reg = 0xE000E100 + 4 * (interrupt_number // 32)  # NVIC_ISERn
        self.write_memory(reg, 4, 1 << (interrupt_number % 32))
