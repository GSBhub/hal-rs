"""``halrs`` command line.

    halrs run -c config.yaml -c addrs.yaml -c memory.yaml [-n NAME]

takes the same ``-c`` configs as ``halucinator`` and runs the firmware on the
qmake-built QEMU with libhalrs.so.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import paths
from .frontend import build_run_config, write_run_config


def cmd_run(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir or os.path.join("tmp", args.name))
    run = build_run_config([Path(c) for c in args.config], outdir,
                           rx_port=args.rx_port, tx_port=args.tx_port,
                           python=not args.no_python,
                           regmaps=[Path(r) for r in args.regmap],
                           skip_intercepts=frozenset(args.skip_intercept),
                           handler_dirs=[Path(d) for d in args.handlers])
    run_json = write_run_config(run, outdir / "run.json")

    plugin = paths.plugin_library()
    qemu = paths.qemu_binary()
    plugin_args = f"{plugin},config={run_json},outdir={outdir / 'images'}"
    if args.trace_mmio:
        plugin_args += ",trace-mmio=on"
    if args.trace:
        plugin_args += f",trace={Path(args.trace).resolve()}"
    argv = [str(qemu), "-plugin", plugin_args, "-M", f"halrs-m,plugin={plugin}",
            "-nographic", "-monitor", "none", "-serial", "none",
            "-d", "plugin" + (f",{args.qemu_log}" if args.qemu_log else "")]
    if args.gdb:
        argv += ["-s", "-S"]
    argv += args.qemu_args
    if args.dry_run:
        print(" ".join(argv))
        return 0
    os.execv(argv[0], argv)


DEFAULT_FIDB = paths.BUILD / "fid/stm32f4-hal.fidb"


def _firmware_image(path: Path, base: int | None) -> tuple[Path, int]:
    """Raw image + load address. ELF inputs are flattened from their first
    executable PT_LOAD segment (stripped ELFs still carry program headers)."""
    with open(path, "rb") as f:
        if f.read(4) != b"\x7fELF":
            return path, base if base is not None else 0x0800_0000
    from elftools.elf.elffile import ELFFile

    with open(path, "rb") as f:
        elf = ELFFile(f)
        seg = next(s for s in elf.iter_segments()
                   if s["p_type"] == "PT_LOAD" and s["p_flags"] & 1)
        out = path.with_suffix(".halrs.bin")
        out.write_bytes(seg.data())
        return out, base if base is not None else seg["p_paddr"]


def cmd_match(args: argparse.Namespace) -> int:
    fw = Path(args.firmware)
    out = Path(args.out or fw.with_name(fw.stem + "_addrs.yaml"))
    from .fid import write_addrs_yaml

    if not args.force_fid:
        from .symbols import elf_functions

        try:
            names = elf_functions(fw)
        except Exception:
            names = {}
        if names:
            write_addrs_yaml(names, out)
            print(f"halrs: {len(names)} functions from ELF symbols -> {out}", file=sys.stderr)
            return 0

    from collections import Counter

    from . import fid

    image, base = _firmware_image(fw, int(args.base, 0) if args.base else None)
    irqs = None
    if args.svd:
        from .svd import irq_names

        irqs = irq_names(Path(args.svd))
    fidb = Path(args.fidb).resolve()
    if not fidb.exists():
        raise SystemExit(f"halrs: {fidb} not found; run `halrs fidb build` first")
    r = fid.match(image, fidb, paths.BUILD / "fid/ghidra", base=base, irq_names=irqs,
                  threshold=args.threshold, use_callgraph=not args.no_callgraph)
    write_addrs_yaml(r.names, out, base=base)
    tiers = ", ".join(f"{k} {v}" for k, v in Counter(r.tier.values()).items())
    print(f"halrs: named {len(r.names)} of {r.functions} functions ({tiers}); "
          f"{len(r.ambiguous)} ambiguous -> {out}", file=sys.stderr)
    if r.variants:
        print(f"halrs: closest reference builds: {', '.join(r.variants)}", file=sys.stderr)
    for a, cands in sorted(r.ambiguous.items()):
        print(f"  {a:#010x} ambiguous: {', '.join(cands)}", file=sys.stderr)
    return 0


def cmd_fidb_build(args: argparse.Namespace) -> int:
    from . import fid
    from .refbuild import Variant, build

    tcs = paths.toolchains()
    missing = [c for c in args.cc if c not in tcs]
    if missing:
        raise SystemExit(f"halrs: toolchains not found: {missing} (have {list(tcs)}); "
                         "see HALRS_TOOLCHAINS")
    built = []
    for h in args.hal:
        for c in args.cc:
            for o in args.opt:
                b = build(Variant(h, c, o), tcs[c], paths.BUILD / "fid-ref")
                print(f"halrs: {b.variant.name}: {len(b.objects)} objects"
                      + (f", failed: {b.failed}" if b.failed else ""), file=sys.stderr)
                built.append(b)
    out = Path(args.out).resolve()
    for s in fid.build_fidb(built, out, paths.BUILD / "fid/ghidra"):
        print(f"halrs: library {s.variant}: {s.added}/{s.attempted} functions", file=sys.stderr)
    print(f"halrs: wrote {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="halrs")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run firmware from HALucinator configs")
    r.add_argument("-c", "--config", action="append", required=True,
                   help="HALucinator YAML (repeatable, same as halucinator -c)")
    r.add_argument("-n", "--name", default="halrs")
    r.add_argument("--outdir")
    r.add_argument("-r", "--rx_port", type=int, default=5555)
    r.add_argument("-t", "--tx_port", type=int, default=5556)
    r.add_argument("--no-python", action="store_true",
                   help="replace every handler with 'return 0' (pure Rust, for timing)")
    r.add_argument("--svd", "--rdl", dest="regmap", action="append", default=[], metavar="FILE",
                   help="CMSIS-SVD (.svd) or SystemRDL (.rdl) register map: model its "
                        "peripherals in Rust (repeatable)")
    r.add_argument("--skip-intercept", action="append", default=[], metavar="FUNC",
                   help="don't hook FUNC even if the config intercepts it (repeatable)")
    r.add_argument("--handlers", action="append", default=[], metavar="DIR",
                   help="directory with your own bp_handler modules (repeatable)")
    r.add_argument("--trace-mmio", action="store_true", help="log MMIO accesses to the QEMU log")
    r.add_argument("--trace", metavar="FILE",
                   help="structured JSONL trace: MMIO, intercepted calls, IRQs")
    r.add_argument("--qemu-log", help="extra QEMU -d items, e.g. in_asm,int")
    r.add_argument("--gdb", action="store_true", help="wait for gdb on :1234")
    r.add_argument("--dry-run", action="store_true", help="print the QEMU command line")
    r.add_argument("qemu_args", nargs="*", help="extra QEMU arguments after --")
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("match", help="recover function names -> HALucinator addrs.yaml")
    m.add_argument("firmware", help="ELF (symbols used if present) or raw .bin")
    m.add_argument("-o", "--out", help="output addrs.yaml (default: <firmware>_addrs.yaml)")
    m.add_argument("--base", help="load address for raw images (default 0x08000000)")
    m.add_argument("--fidb", default=str(DEFAULT_FIDB))
    m.add_argument("--svd", help="SVD for naming IRQ handlers from the vector table")
    m.add_argument("--threshold", type=float, default=14.6, help="FunctionID score threshold")
    m.add_argument("--force-fid", action="store_true", help="ignore ELF symbols")
    m.add_argument("--no-callgraph", action="store_true")
    m.set_defaults(func=cmd_match)

    f = sub.add_parser("fidb", help="Ghidra FunctionID databases")
    fsub = f.add_subparsers(dest="fidb_cmd", required=True)
    fb = fsub.add_parser("build", help="build HAL reference objects and a .fidb from them")
    fb.add_argument("--hal", action="append", help="HAL driver tag (repeatable)")
    fb.add_argument("--cc", action="append", help="toolchain name, e.g. gcc49 (repeatable)")
    fb.add_argument("--opt", action="append", help="optimisation, e.g. O0 (repeatable)")
    fb.add_argument("-o", "--out", default=str(DEFAULT_FIDB))
    fb.set_defaults(func=cmd_fidb_build)

    args = p.parse_args(argv)
    if getattr(args, "fidb_cmd", None) == "build":
        args.hal = args.hal or ["v1.7.6", "v1.8.5"]
        args.cc = args.cc or sorted(paths.toolchains())
        args.opt = args.opt or ["O0", "Os", "O2"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
