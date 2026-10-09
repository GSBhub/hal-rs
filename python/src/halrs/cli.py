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
    r.add_argument("--trace-mmio", action="store_true")
    r.add_argument("--qemu-log", help="extra QEMU -d items, e.g. in_asm,int")
    r.add_argument("--gdb", action="store_true", help="wait for gdb on :1234")
    r.add_argument("--dry-run", action="store_true", help="print the QEMU command line")
    r.add_argument("qemu_args", nargs="*", help="extra QEMU arguments after --")
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
