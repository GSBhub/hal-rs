"""HALucinator config -> ``run.json`` for libhalrs.so.

Uses HALucinator's own ``HalucinatorConfig`` (YAML loading, symbol resolution,
validation), so any config that runs under stock HALucinator runs here.
"""

from __future__ import annotations

import json
import site
import sys
from pathlib import Path
from typing import Any

from .paths import HALUCINATOR_SRC, use_halucinator


def load_halucinator_config(configs: list[Path]):
    use_halucinator()
    from halucinator import hal_config

    cfg = hal_config.HalucinatorConfig()
    for c in configs:
        cfg.add_yaml(str(c))
    if not cfg.prepare_and_validate():
        raise SystemExit("halrs: HALucinator config failed validation (see log above)")
    return cfg


def build_run_config(configs: list[Path], outdir: Path, *, rx_port: int = 5555,
                     tx_port: int = 5556, python: bool = True, regmaps: list[Path] = (),
                     skip_intercepts: frozenset[str] = frozenset(),
                     handler_dirs: list[Path] = ()) -> dict[str, Any]:
    configs = [c.resolve() for c in configs]
    handler_dirs = [str(Path(d).resolve()) for d in handler_dirs]
    sys.path[:0] = [d for d in handler_dirs if d not in sys.path]  # config validation imports them
    cfg = load_halucinator_config(configs)

    run: dict[str, Any] = {
        "arch": "armv7m",
        "cpu": _qemu_cpu(cfg.machine.cpu_model),
        "memory": [],
        "peripherals": [],
        "intercepts": [],
    }
    for mem in cfg.memories.values():
        if mem.emulate:
            run["peripherals"].append({
                "name": mem.name, "base": mem.base_addr, "size": mem.size,
                "model": "python" if python else "rust",
            })
            continue
        entry = {"name": mem.name, "base": mem.base_addr, "size": mem.size,
                 "rom": "w" not in mem.permissions}
        if mem.file:
            # HalMemConfig already made `file` relative to its config file.
            entry["file"] = str(Path(mem.file).resolve())
        run["memory"].append(entry)

    taken = run["memory"]
    for p in (p for rm in regmaps for p in load_regmap(rm)):
        if any(p.overlaps(m["base"], m["size"]) for m in taken):
            continue
        run["peripherals"].append(p.model_dump(mode="json", exclude_defaults=True))

    for ic in cfg.intercepts:
        if ic.bp_addr is None or ic.function in skip_intercepts:
            continue
        handler = ({"kind": "python", "cls": ic.cls, "function": ic.function} if python
                   else {"kind": "return", "value": 0})
        # HALucinator handlers may return (False, None) to run the original
        # function, which needs a trampoline; constant returns never do.
        run["intercepts"].append({"symbol": ic.symbol or ic.function,
                                  "addr": ic.bp_addr & ~1, "handler": handler,
                                  "mode": "trampoline" if python else "stub"})

    if python:
        run["python"] = {
            "home": sys.base_prefix,
            "site_packages": site.getsitepackages(),
            "sys_path": [str(HALUCINATOR_SRC), *handler_dirs],
            "configs": [str(c) for c in configs],
            "outdir": str(outdir.resolve()),
            "rx_port": rx_port,
            "tx_port": tx_port,
        }
    return run


def load_regmap(path: Path):
    """Peripheral register maps from CMSIS-SVD (.svd/.xml) or SystemRDL (.rdl)."""
    if path.suffix.lower() == ".rdl":
        from .rdl import load_rdl

        return load_rdl(path)
    from .svd import load_svd

    return load_svd(path)[1]


def _qemu_cpu(cpu_model: str) -> str:
    # HALucinator defaults to cortex-m3; QEMU's names match for the M profile.
    return cpu_model if cpu_model.startswith("cortex-m") else "cortex-m4"


def write_run_config(run: dict[str, Any], path: Path) -> Path:
    """Validate against the IR (the contract with libhalrs) and write it."""
    from .ir import RunConfig

    cfg = RunConfig.model_validate(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.model_dump_json(indent=2, exclude_none=True))
    return path
