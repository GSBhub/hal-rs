"""Build reference HAL objects (with symbols) for Ghidra FunctionID.

Each variant is (HAL tag, compiler, optimisation level). Sources come from the
``extern/stm32/stm32f4xx-hal-driver`` submodule via ``git archive``, so any tag
in its history can be built without touching the checkout.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .paths import STM32

HAL_REPO = STM32 / "stm32f4xx-hal-driver"
# Match the example firmware: Cortex-M4, Thumb, soft-float (see its .ARM.attributes).
CFLAGS = ["-mcpu=cortex-m4", "-mthumb", "-ffunction-sections", "-fdata-sections",
          "-std=gnu99", "-w"]


@dataclass(frozen=True)
class Variant:
    hal: str        # git tag, e.g. "v1.8.5"
    cc: str         # toolchain name, e.g. "gcc49"
    opt: str        # "O0", "Os", "O2"
    device: str = "STM32F429xx"

    @property
    def name(self) -> str:
        return f"{self.hal}-{self.cc}-{self.opt}"


@dataclass
class Built:
    variant: Variant
    objects: list[Path]
    failed: list[str]


def _hal_sources(tag: str, dest: Path) -> Path:
    """Extract HAL `tag` into dest (cached)."""
    root = dest / f"hal-{tag}"
    if not (root / "Src").is_dir():
        data = subprocess.run(["git", "-C", str(HAL_REPO), "archive", tag],
                              check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(root, filter="data")
    return root


def build(variant: Variant, gcc: Path, out: Path, jobs: int = 8) -> Built:
    hal = _hal_sources(variant.hal, out / "src")
    vdir = out / variant.name
    objdir = vdir / "obj"
    objdir.mkdir(parents=True, exist_ok=True)

    conf = vdir / "conf"
    conf.mkdir(exist_ok=True)
    shutil.copy(hal / "Inc/stm32f4xx_hal_conf_template.h", conf / "stm32f4xx_hal_conf.h")
    incs = [conf, hal / "Inc", hal / "Inc/Legacy", STM32 / "cmsis-device-f4/Include",
            STM32 / "CMSIS_6/CMSIS/Core/Include"]
    base = [str(gcc), *CFLAGS, f"-{variant.opt}", f"-D{variant.device}", "-DUSE_HAL_DRIVER",
            *(f"-I{i}" for i in incs)]

    sources = sorted(p for p in (hal / "Src").glob("*.c") if "template" not in p.name)

    def compile_one(src: Path) -> tuple[Path, bool]:
        obj = objdir / (src.stem + ".o")
        if obj.exists() and obj.stat().st_mtime >= src.stat().st_mtime:
            return obj, True
        r = subprocess.run([*base, "-c", str(src), "-o", str(obj)], capture_output=True)
        return obj, r.returncode == 0

    with ThreadPoolExecutor(jobs) as ex:
        results = list(ex.map(compile_one, sources))
    objects = [o for o, ok in results if ok]
    failed = [o.stem for o, ok in results if not ok]
    return Built(variant, objects, failed)
