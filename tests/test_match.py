"""Function matching against HALucinator's STM32 UART example.

Ground truth is the example's own addrs.yaml (from the unstripped ELF). The
Ghidra test runs `halrs match` on the stripped .bin with the default FIDB
(`halrs fidb build`); it is skipped when Ghidra or the FIDB is unavailable.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from halrs import paths
from halrs.symbols import elf_functions

EX = paths.HALUCINATOR_SRC.parent / "test/STM32/example"
TRUTH = yaml.safe_load((EX / "Uart_Hyperterminal_IT_O0_addrs.yaml").read_text())["symbols"]
INTERCEPTED = {i.get("symbol", i["function"]) for i in yaml.safe_load(
    (EX / "Uart_Hyperterminal_IT_O0_config.yaml").read_text())["intercepts"]} & set(TRUTH.values())
FIDB = paths.BUILD / "fid/stm32f4-hal.fidb"
SVD = Path(__file__).parent / "fixtures/svd/STM32F429.svd"


def test_tier1_matches_hal_make_addr():
    assert elf_functions(EX / "Uart_Hyperterminal_IT_O0.elf") == TRUTH


def test_thumb2_branch_detection():
    from halrs.fid import _thumb2_branch

    assert _thumb2_branch(bytes.fromhex("fef7e2fe"))      # bl, backward (S=1)
    assert _thumb2_branch(bytes.fromhex("00f0fefe"))      # bl, forward (S=0)
    assert not _thumb2_branch(bytes.fromhex("4ff47a73"))  # mov.w r3, #1000
    assert not _thumb2_branch(bytes.fromhex("fee7"))      # 16-bit b


def _ghidra_available() -> bool:
    try:
        paths.ghidra_install()
        import pyghidra  # noqa: F401
    except (SystemExit, ImportError):
        return False
    return FIDB.exists()


@pytest.mark.skipif(not _ghidra_available(), reason="needs Ghidra, pyghidra and `halrs fidb build`")
def test_stripped_firmware(tmp_path):
    out = tmp_path / "addrs.yaml"
    subprocess.run([sys.executable, "-m", "halrs.cli", "match", str(EX / "Uart_Hyperterminal_IT_O0.elf.bin"),
                    "--svd", str(SVD), "-o", str(out)], check=True, env=os.environ)
    got = yaml.safe_load(out.read_text())["symbols"]

    same = lambda a, b: a == b or a.lstrip("_") == b.lstrip("_")  # CMSIS __NVIC_* renames
    wrong = {hex(a): (n, TRUTH.get(a)) for a, n in got.items() if not same(n, TRUTH.get(a, ""))}
    assert not wrong, wrong
    named = {TRUTH[a] for a in got}
    hal = {n for n in TRUTH.values() if n.startswith("HAL_")}
    assert len(named & hal) >= 25, sorted(hal - named)
    assert len(named & INTERCEPTED) >= 12, sorted(INTERCEPTED - named)
