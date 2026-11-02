"""End-to-end parity: HALucinator's STM32 UART example under halrs.

Runs the unmodified HALucinator config + handlers inside QEMU (libhalrs.so),
types 10 characters through HALucinator's own `hal_dev_uart` external device,
and checks the firmware echoes them and finishes -- the same observable
behaviour as stock HALucinator.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from halrs import paths

EXAMPLE = paths.HALUCINATOR_SRC.parent / "test/STM32/example"
CONFIGS = [EXAMPLE / f"Uart_Hyperterminal_IT_O0_{k}.yaml" for k in ("config", "addrs", "memory")]
UART_ID = 0x40011000  # USART1, the id HALucinator's handler publishes with

needs_build = pytest.mark.skipif(
    not (paths.qemu_binary().exists() and paths.plugin_library().exists()),
    reason="build QEMU (cmake --build build) and libhalrs.so (cargo build --release) first",
)


def _halrs_run(outdir: Path, *extra: str, configs=CONFIGS) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "halrs.cli", "run", "-n", "e2e", "--outdir", str(outdir)]
    for c in configs:
        cmd += ["-c", str(c)]
    return subprocess.Popen(cmd + list(extra), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


SVD = Path(__file__).parent / "fixtures/svd/STM32F429.svd"
# HALucinator skips clock/power init with ReturnZero; drop those so the real
# HAL init runs against SVD-derived models + hints instead.
SVD_ONLY_INIT = ["--svd", str(SVD)] + [
    a for f in ("HAL_Init", "HAL_RCC_OscConfig", "HAL_RCC_ClockConfig",
                "HAL_PWREx_EnableOverDrive") for a in ("--skip-intercept", f)]


# Every peripheral as a Python model from `halrs models` (what `peakrdl
# halucinator` writes), through HALucinator's memory config instead of --svd.
GENERATED = SVD_ONLY_INIT[2:]   # (the clock-init skips, without --svd)


def _generated_configs(tmp: Path) -> tuple[list[Path], list[str]]:
    from halrs.cli import main
    gen = tmp / "gen"
    gen.mkdir()
    main(["models", str(SVD), "-o", str(gen / "f429_regs.py"), "--memory", str(gen / "periph.yaml")])
    mem = yaml.safe_load(CONFIGS[2].read_text())
    for m in mem["memories"].values():
        if "file" in m:
            m["file"] = str(EXAMPLE / m["file"])
    del mem["peripherals"]   # the catch-all GenericPeripheral logger
    (gen / "mem.yaml").write_text(yaml.safe_dump(mem))
    return [*CONFIGS[:2], gen / "mem.yaml", gen / "periph.yaml"], ["--handlers", str(gen)]


# HALucinator's ArgumentLogger(intercept=False) on HAL_UART_Init: the handler
# runs, then the real function executes via a trampoline (against SVD models).
PASSTHROUGH = ["--svd", str(SVD), "--trace-mmio",
               "-c", str(Path(__file__).parent / "fixtures/passthrough.yaml")]


@needs_build
@pytest.mark.parametrize("extra", [[], SVD_ONLY_INIT, PASSTHROUGH, GENERATED],
                         ids=["halucinator-config", "svd-init", "passthrough", "generated-models"])
def test_uart_round_trip(tmp_path, extra):
    if extra is GENERATED:
        configs, handlers = _generated_configs(tmp_path)
        emu = _halrs_run(tmp_path, *extra, *handlers, configs=configs)
    else:
        emu = _halrs_run(tmp_path, *extra)
    time.sleep(1.5)
    dev = subprocess.Popen(
        [sys.executable, "-m", "halucinator.external_devices.uart", "-i", str(UART_ID)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONPATH": str(paths.HALUCINATOR_SRC)},
    )
    time.sleep(2)
    dev.stdin.write("1234567890\n")
    dev.stdin.flush()
    time.sleep(3)

    emu.send_signal(signal.SIGINT)
    emu_out, _ = emu.communicate(timeout=20)
    dev.send_signal(signal.SIGINT)
    dev_out, _ = dev.communicate(timeout=10)

    assert "1234567890" in dev_out, dev_out
    assert "Example Finished" in dev_out, dev_out
    assert "intercept HAL_UART_Receive_IT hit 1x" in emu_out, emu_out
    if extra:
        assert "poll loop detected" not in emu_out, "static hints should suffice"
    assert "trampoline unavailable" not in emu_out
    if extra is GENERATED:
        assert "13 python peripherals" in emu_out, emu_out
    if extra is PASSTHROUGH:
        # The real HAL_UART_Init ran: it programs USART1's baud rate register.
        assert "wr USART1+0x8/4" in emu_out, emu_out


@needs_build
def test_sigint_while_handler_blocks(tmp_path):
    """No external device: HAL_UART_Receive_IT blocks; Ctrl-C must still exit."""
    emu = _halrs_run(tmp_path)
    time.sleep(3)
    emu.send_signal(signal.SIGINT)
    out, _ = emu.communicate(timeout=15)
    assert "aborted for shutdown" in out, out


@needs_build
def test_irq_from_python_handler(tmp_path):
    """A handler raises USART1's IRQ; the NVIC takes it and the ISR runs once."""
    here = Path(__file__).parent
    emu = _halrs_run(tmp_path, "-c", str(here / "fixtures/irq_probe.yaml"),
                     "--handlers", str(here / "handlers"))
    time.sleep(3)
    emu.send_signal(signal.SIGINT)
    out, _ = emu.communicate(timeout=15)
    assert "USART1 ISR entered (1)" in out, out
    assert "intercept USART1_IRQHandler hit 1x" in out, out
