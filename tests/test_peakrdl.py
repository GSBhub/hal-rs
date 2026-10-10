import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

RDL = Path(__file__).parent / "fixtures/rdl/demo_soc.rdl"
PEAKRDL = Path(sys.executable).parent / "peakrdl"


def _peakrdl(*args, cwd):
    subprocess.run([str(PEAKRDL), *map(str, args)], cwd=cwd, check=True, capture_output=True)


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_model_matches_rust_semantics(tmp_path):
    _peakrdl("halucinator", RDL, "-o", tmp_path / "demo_regs.py", cwd=tmp_path)
    m = _load(tmp_path / "demo_regs.py")
    cls, base, size = m.PERIPHERALS["uart0"]
    assert (base, size, cls.IRQS) == (0x40011000, 0xC, (37,))
    uart = cls("uart0", base, 0x100)            # window wider than the registers
    rd = lambda off, n=4: uart.read_memory(base + off, n)
    wr = lambda off, v, n=4: uart.write_memory(base + off, n, v)

    assert rd(0) == 1 << 7                       # TXE: reset 1, poll-set; BSY poll-clear
    uart["SR"] |= 1 << 5 | 1 << 3 | 1 << 9      # device sets RXNE, ORE, BSY
    assert rd(0) == 1 << 7 | 1 << 5 | 1 << 3    # BSY forced clear for the poll loop
    assert rd(0) == 1 << 7 | 1 << 3             # RXNE cleared by the read
    wr(0, 0)
    assert rd(0) & 1 << 3                        # W1C: writing 0 leaves ORE
    wr(0, 1 << 3)
    assert not rd(0) & 1 << 3
    uart["SR"] |= 1 << 3
    wr(1, 0, 1)                                  # a byte write leaves other lanes' W1C bits
    assert uart["SR"] == 1 << 7 | 1 << 3 | 1 << 9   # and BSY is read-only
    wr(8, 0x1234)
    assert uart["KEYR"] == 0x1234 and rd(8) == 0  # write-only reads 0
    wr(4, 0x1A5)
    assert rd(4, 1) == 0xA5 and rd(5, 1) == 0x01
    wr(0x40, 0xDEAD)                             # unmapped offsets are plain storage
    assert rd(0x40) == 0xDEAD

    clk = m.clk("clk", 0x40023800, 4)
    clk.write_memory(0x40023800, 4, 1 << 24)     # PLLON -> PLLRDY mirrors it
    assert clk.read_memory(0x40023800, 4) == 1 << 24 | 1 << 25


def test_model_hooks(tmp_path):
    _peakrdl("halucinator", RDL, "-o", tmp_path / "demo_regs.py", cwd=tmp_path)
    gen = _load(tmp_path / "demo_regs.py")
    out = []

    class Uart(gen.uart0):
        def read_DR(self, value):
            return 0x42

        def write_DR(self, value, old):
            out.append(value)

    u = Uart("uart0", 0x40011000, 0xC)
    u.write_memory(0x40011004, 4, ord("x"))
    assert out == [ord("x")] and u.read_memory(0x40011004, 4) == 0x42


def test_memory_config_and_declared_udp(tmp_path):
    declared = tmp_path / "declared.rdl"
    declared.write_text("property halrs_irq { type = longint unsigned; component = addrmap; };\n"
                        + RDL.read_text())
    _peakrdl("halucinator-memory", declared, "-o", tmp_path / "mem.yaml",
             "--module", "demo_regs", cwd=tmp_path)
    periphs = yaml.safe_load((tmp_path / "mem.yaml").read_text())["peripherals"]
    # HALucinator wants whole 4 KB pages: one Bus per window of peripherals
    assert periphs == {
        "periph_40011000": {"base_addr": 0x40011000, "size": 0x1000,
                            "permissions": "rw-", "emulate": "demo_regs.Bus"},
        "periph_40023000": {"base_addr": 0x40023000, "size": 0x1000,
                            "permissions": "rw-", "emulate": "demo_regs.Bus"},
    }


def test_bus_routes_to_peripherals(tmp_path):
    _peakrdl("halucinator", RDL, "-o", tmp_path / "demo_regs.py", cwd=tmp_path)
    m = _load(tmp_path / "demo_regs.py")
    bus = m.Bus("periph_40023000", 0x40023000, 0x1000)
    assert list(bus.devices) == ["clk"]
    bus.write_memory(0x40023800, 4, 1 << 24)
    assert bus.read_memory(0x40023800, 4) == 1 << 24 | 1 << 25
    assert bus["clk"]["CR"] == 1 << 24
    bus.write_memory(0x40023010, 2, 0xBEEF)        # between peripherals: plain storage
    assert bus.read_memory(0x40023010, 2) == 0xBEEF
