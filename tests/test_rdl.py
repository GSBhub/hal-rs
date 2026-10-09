from pathlib import Path

from halrs.ir import Access, Hint, Mirror
from halrs.rdl import load_rdl

RDL = Path(__file__).parent / "fixtures/rdl/demo_soc.rdl"


def test_rdl_peripherals():
    ps = {p.name: p for p in load_rdl(RDL)}
    uart, clk = ps["uart0"], ps["clk"]
    assert (uart.base, uart.size, uart.irqs) == (0x40011000, 0xC, [37])
    assert clk.base == 0x40023800 and clk.irqs == []

    sr = next(r for r in uart.registers if r.name == "SR")
    f = {x.name: x for x in sr.fields}
    assert sr.reset == 1 << 7
    assert f["TXE"].access == Access.READ_ONLY and f["TXE"].hint == Hint.POLL_SET
    assert f["RXNE"].access == Access.READ_CLEAR and f["RXNE"].hint is None
    assert f["ORE"].access == Access.W1C
    assert f["BSY"].hint == Hint.POLL_CLEAR
    keyr = next(r for r in uart.registers if r.name == "KEYR")
    assert keyr.fields[0].access == Access.WRITE_ONLY

    cr = clk.registers[0]
    assert next(x for x in cr.fields if x.name == "PLLRDY").hint == Mirror(mirror=24)
