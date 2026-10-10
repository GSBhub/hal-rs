# hal-rs

HALucinator-style firmware rehosting on a stock QEMU built out-of-tree by
[qmake](extern/qmake), with HAL handlers running **in-process**: Rust for the hot
paths, an embedded CPython for HALucinator's own handlers, and peripheral models
generated from CMSIS-SVD / SystemRDL.

HALucinator and avatar2 are **submodules, used unmodified**. `halrs run` takes the
same `-c` YAML configs as `halucinator`, runs the same `bp_handlers` and
`peripheral_models`, and talks to the same external devices (`hal_dev_uart`, ...)
over the same zmq peripheral server. Only the QEMU side changes:

- no avatar-qemu fork: QEMU 10.2 plus ~400 lines of C glue (machine + generic MMIO device) built through qmake;
- no GDB/QMP round trip per hook: an intercept is a TCG plugin callback;
- firmware peripherals without a HAL handler can be modelled from an SVD/RDL file.

## Layout

```
qemu/                 C glue linked into QEMU (via qmake): halrs-m machine + halrs-periph device
crates/halrs-core     run.json IR (serde), register-file models, hints, firmware patching
crates/halrs-plugin   libhalrs.so: QEMU TCG plugin; PyO3 interpreter hosting HALucinator handlers
python/src/halrs      front end (HALucinator config + SVD/RDL -> run.json), avatar2 shim, CLI
extern/               qmake, halucinator, avatar2 (v1.3.1, the version HALucinator pins)
tests/                pytest e2e + IR contract tests; fixtures
bench/                hook-rate benchmark
```

How a run fits together:

1. `halrs run` loads the HALucinator configs with HALucinator's own
   `HalucinatorConfig`, adds SVD/RDL register maps, and writes `run.json`
   (validated against `python/src/halrs/ir.py`).
2. QEMU loads `libhalrs.so`, which writes patched copies of the firmware and
   hooks only the patched instructions. Each intercepted function's entry
   branches to a small trampoline (`cmp r12,#0; beq orig; bx lr`, then the
   relocated original entry). The handler's decision goes in r12: return, or
   run the original function. Constant-return intercepts use a bare `bx lr`
   stub instead.
3. The `halrs-m` machine asks the plugin for its layout (`halrs_board()`), maps
   memories and one `halrs-periph` MMIO window per peripheral, and forwards MMIO
   to the plugin.
4. On an intercept, the plugin either returns a constant (Rust) or calls
   `halrs.runtime.dispatch`, which runs HALucinator's `interceptor` against an
   avatar2-compatible shim (`regs.r0`, `read_memory`, `get_arg`, `irq_set`, ...).

## Build

Needs QEMU's build deps (meson, ninja, glib, pixman), CMake >= 3.30, Rust, and
[uv](https://docs.astral.sh/uv/).

```sh
git submodule update --init --recursive        # (extern/qmake/extern/qemu: --depth 1 is fine)
(cd python && uv sync)                         # venv with avatar2 + HALucinator's deps
cmake -S . -B build -G Ninja && cmake --build build --target qemu
PYO3_PYTHON=$PWD/python/.venv/bin/python cargo build --release -p halrs-plugin
```

The plugin embeds the venv's (uv-managed) CPython 3.12; `PYO3_PYTHON` pins which
one it links. Rebuilding the plugin never relinks QEMU; QEMU only needs
rebuilding when `qemu/` changes.

## Run

```sh
cd extern/halucinator
EX=test/STM32/example
../../python/.venv/bin/halrs run -c $EX/Uart_Hyperterminal_IT_O0_config.yaml \
    -c $EX/Uart_Hyperterminal_IT_O0_addrs.yaml -c $EX/Uart_Hyperterminal_IT_O0_memory.yaml
# in another terminal, HALucinator's own UART device:
PYTHONPATH=src ../../python/.venv/bin/python -m halucinator.external_devices.uart -i 1073811456
```

Useful options: `--svd FILE` / `--rdl FILE` (model peripherals in Rust),
`--skip-intercept FUNC` (let real HAL code run against those models),
`--handlers DIR` (your own `bp_handler` modules), `--no-python` (every handler
becomes `return 0`), `--trace-mmio`, `--gdb`, `--dry-run`.

Example: run the firmware's real clock/power init against STM32F429 SVD models
instead of HALucinator's `ReturnZero` intercepts:

```sh
halrs run -c ... --svd ../../tests/fixtures/svd/STM32F429.svd \
  --skip-intercept HAL_Init --skip-intercept HAL_RCC_OscConfig \
  --skip-intercept HAL_RCC_ClockConfig --skip-intercept HAL_PWREx_EnableOverDrive
```

## Peripheral models

SVD (via `cmsis-svd`) and SystemRDL (via `systemrdl-compiler`) both lower to the
same IR. The Rust model keeps reset values and access semantics (RO, WO, W1C,
read-clear). Undescribed offsets behave like RAM. Static hints cover the usual
status-polling patterns:

| hint | rule | example |
|---|---|---|
| `poll-set` | `*RDY`, `TXE`, `TC`, `EOC`, ... in a status/control register | `USART_SR.TXE` |
| `poll-clear` | `BSY`, `BUSY`, ... | `FLASH_SR.BSY` |
| `mirror` | `<X>RDY` follows `<X>ON`; read-only `<X>S` reports `<X>` | `RCC_CR.PLLRDY`, `RCC_CFGR.SWS` |

Anything the hints miss gets caught by a poll-loop detector. It fires after the
same register reads back the same value 64 times with no write in between, and
it logs each time it steps in.

## Function matching (stripped firmware)

`halrs match` recovers function names and writes HALucinator's `addrs.yaml`,
so a stripped image can run with a stock HALucinator config. It works in
tiers:

1. **ELF symbols.** If the input has them, they're used directly. The output is
   identical to HALucinator's `hal_make_addr`, without its angr dependency.
2. **Vector table.** The CMSIS core handlers (Reset, HardFault, SysTick, ...)
   are named by slot. Device IRQ handlers are named from the SVD's interrupt
   names when you pass `--svd`.
3. **Ghidra FunctionID**, against reference builds of the STM32F4 HAL. A name
   is only applied when every match above the score threshold agrees.

   FunctionID alone misses any function that makes a *backward* call in Thumb
   code. Ghidra's instruction mask for `bl`/`b.w` keeps the offset's sign bit,
   so the hash depends on call direction. Reference objects always call
   forward, into Ghidra's EXTERNAL block, but linked firmware often calls
   backward. To cover this, `fidb build` also writes a `.norm.json` index of a
   FunctionID-style hash with that bit cleared. Functions with a unique match
   there are named too.
4. **Call-graph context**, LibMatch-style. Names spread outward from the
   tier-2 and tier-3 anchors:
   - *Downward:* a named function's callees, when its call list lines up with
     a reference function's.
   - *Upward:* an unnamed function whose named callees pin down exactly one
     reference function.

   A proposal is applied only if it's unanimous and doesn't reuse a name.
   Different optimization levels can order the same function's calls
   differently. Call orders from the reference builds that FunctionID agreed
   with most often are tried first; `halrs match` prints which builds those
   were.

```sh
# one-time setup: Ghidra + JDK 21 (GHIDRA_INSTALL_DIR / JAVA_HOME, or under
# ~/.local/opt), arm-none-eabi-gcc toolchains (~/.local/opt/*/bin, or HALRS_TOOLCHAINS)
(cd python && uv sync --extra match)
git submodule update --init extern/stm32/*
halrs fidb build                       # HAL v1.7.6+v1.8.5 x each toolchain x -O0/-Os/-O2
halrs match firmware.bin --svd chip.svd -o firmware_addrs.yaml
halrs run -c config.yaml -c firmware_addrs.yaml -c memory.yaml
```

Reference sources are the `extern/stm32/` submodules: STM's
`stm32f4xx-hal-driver`, `cmsis-device-f4`, and CMSIS_6. Any HAL tag in the
driver's history can be built with `--hal`.

## Tests

```sh
cargo test -p halrs-core
(cd python && uv run pytest ../tests)
bench/hook_rate.sh
```

The e2e tests boot HALucinator's STM32 UART example and type into it through
`hal_dev_uart`. They run it twice: once with HALucinator's own config, and once
with clock/power init running against SVD models instead of intercepts. They
also check three more cases:

- a pass-through handler (`ArgumentLogger`) whose real function then runs,
- an IRQ raised from a Python handler,
- Ctrl-C while a handler is blocked.

Measured on this machine (`bench/hook_rate.sh`), hits on a spinning
`HAL_UART_GetState`, guest loop included:

| handler | rate | per hook |
|---|---|---|
| Rust (`return 0`) | ~11.4 M/s | ~88 ns |
| Python (HALucinator `ReturnZero` via its `interceptor`) | ~80 k/s | ~12.6 µs |

## Status

| | |
|---|---|
| halrs-m machine, halrs-periph, plugin ABI | done |
| Stub intercepts (`bx lr`), Rust return handlers | done |
| Pass-through handlers (`return False, None`) via trampolines | done (falls back to a stub, with a log line, if the entry isn't relocatable) |
| Embedded HALucinator handlers, avatar2 shim, zmq peripheral server | done |
| HALucinator `emulate:` peripherals (Python `AvatarPeripheral`) | done |
| SVD / SystemRDL -> Rust models, hints, poll detector | done |
| IRQs from handlers (NVIC), Ctrl-C while blocked | done |
| `AvatarPeripheral`-based bp_handlers (`get_mmio_info`) and watchpoints | not yet |
| Function matching: ELF symbols, vector table, FunctionID, call-graph context | done (STM32F4 HAL references) |
| Matching for other vendor HALs; the full LibMatch submodule | not yet |
| avatar2 `HalrsTarget` (drive halrs QEMU from avatar2 scripts) | not yet |
| Pcode backend | waiting on the Pcode target; see contract below |
| Benchmarks: plugin overhead with no hooks; stock HALucinator comparison | not yet |

### Compatibility notes

- avatar2 is pinned to **v1.3.1**, the version HALucinator pins. Newer avatar2
  passes `origin=` to peripheral handlers, which HALucinator's
  `GenericPeripheral` rejects.
- HALucinator is used from `extern/halucinator/src` on `sys.path`, not
  installed: its `distutils` `setup.py` doesn't install on 3.12.
  `halrs.compat` restores `inspect.getargspec`, and `setuptools<81` provides
  `distutils` and `pkg_resources` for avatar2.

## Pcode-target contract

A backend gets the whole HAL layer unchanged if it provides four things:

1. TCG plugin support (it comes free with `translator_loop`).
2. A return-stub encoding plus argument and return registers for its ABI. The
   Sleigh `.cspec` can generate these.
3. gdb-xml register names, which `qemu_plugin_get_registers` exposes.
4. A machine that can map `halrs-periph` windows.
