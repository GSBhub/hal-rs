"""Embedded runtime: runs *inside* QEMU, in libhalrs.so's Python interpreter.

It replays HALucinator's own ``emulate_binary`` setup against the avatar2 shim:
the same ``HalucinatorConfig`` loader, ``intercepts.register_bp_handler``,
``peripheral_models.generic`` emulators and zmq ``peripheral_server`` -- so
external devices (``hal_dev_uart`` ...) work unchanged. The plugin then calls
:func:`dispatch` on every intercept hit and :func:`mmio_read`/:func:`mmio_write`
for Python-emulated peripheral windows.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from types import SimpleNamespace

import halrs_native as native  # provided by libhalrs.so

from . import compat  # noqa: F401  (must precede halucinator imports)
from halucinator import hal_config, hal_stats
from halucinator.bp_handlers import intercepts
from halucinator.peripheral_models import generic as peripheral_emulators
from halucinator.peripheral_models import peripheral_server

from .shim import FakeAvatar, ShimTarget

log = logging.getLogger("halrs")


def emulator(name: str):
    """A memory's ``emulate:`` class: a name in HALucinator's
    ``peripheral_models.generic`` (as stock HALucinator resolves it) or a dotted
    ``module.Class`` path, e.g. models from ``peakrdl halucinator``."""
    if "." not in name:
        return getattr(peripheral_emulators, name)
    module, _, cls = name.rpartition(".")
    return getattr(importlib.import_module(module), cls)


class Runtime:
    def __init__(self, configs: list[str], outdir: str, rx_port: int, tx_port: int) -> None:
        os.makedirs(outdir, exist_ok=True)
        hal_stats.set_filename(os.path.join(outdir, "stats.yaml"))

        self.config = hal_config.HalucinatorConfig()
        for c in configs:
            self.config.add_yaml(c)
        self.config.prepare_and_validate()

        self.avatar = FakeAvatar(self.config, outdir)
        self.target = ShimTarget(native, self.avatar)
        self.avatar.config = self.config

        # Python-emulated peripheral windows, keyed by memory name.
        self.peripherals = {}
        for mem in self.config.memories.values():
            if mem.emulate:
                cls = emulator(mem.emulate)
                self.peripherals[mem.name] = cls(mem.name, mem.base_addr, mem.size)

        for ic in self.config.intercepts:
            if ic.bp_addr is not None:
                intercepts.register_bp_handler(self.target, ic)
        self.addr2bp = {addr: bp for bp, addr in self.target.breakpoints().items()}

        peripheral_server.start(rx_port, tx_port, self.target)
        threading.Thread(target=peripheral_server.run_server, name="halrs-periph-server",
                         daemon=True).start()
        native.log("runtime ready: %d intercepts, %d python peripherals"
                   % (len(self.addr2bp), len(self.peripherals)))

    def dispatch(self, addr: int) -> bool:
        """Run the HALucinator handler for the intercept at ``addr``.

        Returns whether the handler intercepted (returned) the function.
        """
        t = self.target
        t.current_pc = addr
        t.returned = False
        try:
            msg = SimpleNamespace(breakpoint_number=self.addr2bp[addr], origin=t)
            intercepts.interceptor(self.avatar, msg)
        finally:
            t.current_pc = None
        return t.returned

    def mmio_read(self, name: str, offset: int, size: int) -> int:
        p = self.peripherals[name]
        return p.read_memory(p.address + offset, size) or 0

    def mmio_write(self, name: str, offset: int, size: int, value: int) -> None:
        p = self.peripherals[name]
        p.write_memory(p.address + offset, size, value)


_rt: Runtime | None = None


def setup(configs: list[str], outdir: str, rx_port: int = 5555, tx_port: int = 5556) -> None:
    global _rt
    logging.basicConfig(level=os.environ.get("HALRS_LOG", "WARNING"))
    _rt = Runtime(configs, outdir, rx_port, tx_port)


def dispatch(addr: int) -> bool:
    return _rt.dispatch(addr)


def mmio_read(name: str, offset: int, size: int) -> int:
    return _rt.mmio_read(name, offset, size)


def mmio_write(name: str, offset: int, size: int, value: int) -> None:
    _rt.mmio_write(name, offset, size, value)
