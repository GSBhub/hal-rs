"""IR contract: pydantic (python/src/halrs/ir.py) vs serde (halrs-core config.rs).

`tests/fixtures/ir/run.json` is produced by the Python front end and parsed by
`cargo test -p halrs-core` (see config.rs tests); both sides reject unknown
fields, so adding a field on one side only fails one of the two suites.
"""

import json
from pathlib import Path

from halrs import paths
from halrs.frontend import build_run_config, write_run_config
from halrs.ir import Hint, Mirror, RunConfig

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = paths.HALUCINATOR_SRC.parent / "test/STM32/example"
CONFIGS = [EXAMPLE / f"Uart_Hyperterminal_IT_O0_{k}.yaml" for k in ("config", "addrs", "memory")]


def _example_run(tmp_path):
    return build_run_config(CONFIGS, tmp_path, regmaps=[FIXTURES / "svd/STM32F429.svd"],
                            skip_intercepts=frozenset({"HAL_Init"}))


def test_fixture_is_current(tmp_path):
    """Regenerate with: uv run python -c 'import tests.test_ir as t; t.regen()'."""
    out = write_run_config(_example_run(tmp_path), tmp_path / "run.json")
    got = _normalise(json.loads(out.read_text()))
    want = json.loads((FIXTURES / "ir/run.json").read_text())
    assert got == want


def test_round_trip(tmp_path):
    cfg = RunConfig.model_validate(_example_run(tmp_path))
    again = RunConfig.model_validate_json(cfg.model_dump_json())
    assert again == cfg
    rcc = next(p for p in cfg.peripherals if p.name == "RCC")
    hints = {f.name: f.hint for r in rcc.registers for f in r.fields if f.hint is not None}
    assert hints["PLLRDY"] == Mirror(mirror=24)  # ready follows enable
    assert hints["SWS1"] == Mirror(mirror=1)
    flash = next(p for p in cfg.peripherals if p.name == "FLASH")
    assert any(f.hint == Hint.POLL_CLEAR for r in flash.registers for f in r.fields)


KEEP = {"logger", "RCC", "PWR", "FLASH", "USART1"}


def _normalise(run: dict) -> dict:
    """Drop machine-specific paths and trim to a representative subset."""
    run.pop("python", None)
    run["peripherals"] = [p for p in run["peripherals"] if p["name"] in KEEP]
    for m in run["memory"]:
        if "file" in m:
            m["file"] = Path(m["file"]).name
    return run


def regen(tmp=Path("/tmp/halrs-ir")):
    out = write_run_config(_example_run(tmp), tmp / "run.json")
    data = _normalise(json.loads(out.read_text()))
    (FIXTURES / "ir/run.json").write_text(json.dumps(data, indent=1) + "\n")
