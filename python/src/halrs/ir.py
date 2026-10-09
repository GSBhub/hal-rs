"""The hal-rs IR: pydantic models for ``run.json``.

This is the source of truth; ``crates/halrs-core/src/config.rs`` mirrors it with
serde. ``tests/test_ir.py`` round-trips both to catch drift.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel as _BaseModel, ConfigDict, Field as PField


class BaseModel(_BaseModel):
    # Mirror serde(deny_unknown_fields) on the Rust side.
    model_config = ConfigDict(extra="forbid")


class Access(str, Enum):
    READ_WRITE = "read-write"
    READ_ONLY = "read-only"
    WRITE_ONLY = "write-only"
    W1C = "w1c"
    READ_CLEAR = "read-clear"


class Hint(str, Enum):
    POLL_SET = "poll-set"      # firmware waits for 1 (RDY, TXE, TC...)
    POLL_CLEAR = "poll-clear"  # firmware waits for 0 (BSY, BUSY...)


class Mirror(BaseModel):
    """Status field that reads back a control field of the same register."""

    mirror: int  # lsb of the source field


class Field(BaseModel):
    name: str
    lsb: int
    width: int
    access: Access = Access.READ_WRITE
    hint: Hint | Mirror | None = None


class Register(BaseModel):
    name: str
    offset: int
    size: int = 32
    reset: int = 0
    fields: list[Field] = PField(default_factory=list)


class ModelKind(str, Enum):
    RUST = "rust"
    PYTHON = "python"


class Peripheral(BaseModel):
    name: str
    base: int
    size: int
    irqs: list[int] = PField(default_factory=list)
    registers: list[Register] = PField(default_factory=list)
    model: ModelKind = ModelKind.RUST

    def overlaps(self, base: int, size: int) -> bool:
        return self.base < base + size and base < self.base + self.size


class Memory(BaseModel):
    name: str
    base: int
    size: int
    rom: bool = False
    file: Path | None = None


class HandlerReturn(BaseModel):
    kind: Literal["return"] = "return"
    value: int | None = None


class HandlerPython(BaseModel):
    kind: Literal["python"] = "python"
    cls: str = ""
    function: str = ""


Handler = Annotated[HandlerReturn | HandlerPython, PField(discriminator="kind")]


class InterceptMode(str, Enum):
    STUB = "stub"
    TRAMPOLINE = "trampoline"


class Intercept(BaseModel):
    symbol: str
    addr: int
    mode: InterceptMode = InterceptMode.STUB
    handler: Handler


class PythonConfig(BaseModel):
    home: Path | None = None
    site_packages: list[Path] = PField(default_factory=list)
    sys_path: list[Path] = PField(default_factory=list)
    configs: list[Path] = PField(default_factory=list)
    outdir: Path
    rx_port: int = 5555
    tx_port: int = 5556


class RunConfig(BaseModel):
    arch: str = "armv7m"
    cpu: str = "cortex-m4"
    num_irq: int = 96
    sysclk_hz: int = 168_000_000
    memory: list[Memory] = PField(default_factory=list)
    peripherals: list[Peripheral] = PField(default_factory=list)
    intercepts: list[Intercept] = PField(default_factory=list)
    python: PythonConfig | None = None
