//! `run.json`: the resolved run configuration the Python front end hands the plugin.
//!
//! The Python pydantic models (`python/halrs/ir.py`) are the source of truth; these
//! mirror them. Memory and intercepts come from a HALucinator config, peripheral
//! register maps from SVD/SystemRDL.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::Error;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunConfig {
    #[serde(default = "default_arch")]
    pub arch: String,
    #[serde(default = "default_cpu")]
    pub cpu: String,
    #[serde(default = "default_num_irq")]
    pub num_irq: u32,
    #[serde(default = "default_sysclk")]
    pub sysclk_hz: u64,
    #[serde(default)]
    pub memory: Vec<Memory>,
    #[serde(default)]
    pub peripherals: Vec<Peripheral>,
    #[serde(default)]
    pub intercepts: Vec<Intercept>,
    /// Where handler Python code runs from; `None` disables embedded Python.
    #[serde(default)]
    pub python: Option<PythonConfig>,
}

fn default_arch() -> String {
    "armv7m".into()
}
fn default_cpu() -> String {
    "cortex-m4".into()
}
fn default_num_irq() -> u32 {
    96
}
fn default_sysclk() -> u64 {
    168_000_000
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Memory {
    pub name: String,
    pub base: u64,
    pub size: u64,
    #[serde(default)]
    pub rom: bool,
    #[serde(default)]
    pub file: Option<PathBuf>,
}

impl Memory {
    pub fn contains(&self, addr: u64) -> bool {
        addr >= self.base && addr - self.base < self.size
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Peripheral {
    pub name: String,
    pub base: u64,
    pub size: u64,
    #[serde(default)]
    pub irqs: Vec<u32>,
    #[serde(default)]
    pub registers: Vec<Register>,
    /// Who services this window's MMIO.
    #[serde(default)]
    pub model: ModelKind,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ModelKind {
    /// Data-driven register model in Rust (SVD/RDL, heuristics).
    #[default]
    Rust,
    /// A HALucinator/avatar2 `AvatarPeripheral` in the embedded interpreter.
    Python,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Register {
    pub name: String,
    pub offset: u64,
    /// Width in bits.
    #[serde(default = "default_reg_size")]
    pub size: u32,
    #[serde(default)]
    pub reset: u64,
    #[serde(default)]
    pub fields: Vec<Field>,
}

fn default_reg_size() -> u32 {
    32
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Field {
    pub name: String,
    pub lsb: u32,
    pub width: u32,
    #[serde(default)]
    pub access: Access,
    /// Heuristic hint assigned by the front end (e.g. a status "ready" bit).
    #[serde(default)]
    pub hint: Option<Hint>,
}

impl Field {
    pub fn mask(&self) -> u64 {
        let ones = if self.width >= 64 { u64::MAX } else { (1u64 << self.width) - 1 };
        ones << self.lsb
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Access {
    #[default]
    ReadWrite,
    ReadOnly,
    WriteOnly,
    /// Write 1 to clear.
    W1c,
    /// Cleared by a read.
    ReadClear,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Hint {
    /// Firmware waits for this bit to be 1 (RDY, TXE, TC, ...): always read as set.
    PollSet,
    /// Firmware waits for this bit to be 0 (BSY, BUSY, ...): always read as clear.
    PollClear,
    /// Status field that reports a control field of the same register (e.g.
    /// RCC_CFGR.SWS <- SW): reads return the field starting at this lsb.
    Mirror(u32),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Intercept {
    pub symbol: String,
    pub addr: u64,
    #[serde(default)]
    pub mode: InterceptMode,
    pub handler: HandlerRef,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum InterceptMode {
    /// Entry patched to a return stub; the handler always replaces the function.
    #[default]
    Stub,
    /// Entry branches to a trampoline so the handler may fall through to the original.
    Trampoline,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum HandlerRef {
    /// Return a constant (or nothing) without running any code.
    Return { value: Option<u64> },
    /// A HALucinator `bp_handler`, registered and dispatched by the embedded
    /// runtime (`halrs.runtime`) from the HALucinator config; `cls`/`function`
    /// are informational.
    Python {
        #[serde(default)]
        cls: String,
        #[serde(default)]
        function: String,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PythonConfig {
    /// `sys.base_prefix` of the interpreter libhalrs links (PYTHONHOME).
    #[serde(default)]
    pub home: Option<PathBuf>,
    /// venv site-packages, added with `site.addsitedir` so `.pth` files apply.
    #[serde(default)]
    pub site_packages: Vec<PathBuf>,
    /// Extra `sys.path` entries (e.g. extern/halucinator/src, handler dirs).
    #[serde(default)]
    pub sys_path: Vec<PathBuf>,
    /// HALucinator YAML configs the runtime loads (intercepts, memories, symbols).
    #[serde(default)]
    pub configs: Vec<PathBuf>,
    /// HALucinator output directory (stats.yaml, logs).
    pub outdir: PathBuf,
    #[serde(default = "default_rx_port")]
    pub rx_port: u16,
    #[serde(default = "default_tx_port")]
    pub tx_port: u16,
}

fn default_rx_port() -> u16 {
    5555
}
fn default_tx_port() -> u16 {
    5556
}

impl RunConfig {
    pub fn from_json(text: &str) -> Result<Self, Error> {
        Ok(serde_json::from_str(text)?)
    }

    /// Load `path`, resolving relative memory files against its directory.
    pub fn load(path: &Path) -> Result<Self, Error> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| Error::Io(path.to_path_buf(), e))?;
        let mut cfg = Self::from_json(&text)?;
        let dir = path.parent().unwrap_or(Path::new("."));
        for m in &mut cfg.memory {
            if let Some(f) = &m.file
                && f.is_relative()
            {
                m.file = Some(dir.join(f));
            }
        }
        Ok(cfg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Written by the Python front end (tests/test_ir.py); both sides reject
    /// unknown fields, so IR drift fails here or there.
    #[test]
    fn parses_python_fixture() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/fixtures/ir/run.json");
        let cfg = RunConfig::from_json(&std::fs::read_to_string(path).unwrap()).unwrap();
        assert!(cfg.peripherals.iter().any(|p| p.model == ModelKind::Python));
        let rcc = cfg.peripherals.iter().find(|p| p.name == "RCC").unwrap();
        let pllrdy = rcc
            .registers
            .iter()
            .flat_map(|r| &r.fields)
            .find(|f| f.name == "PLLRDY")
            .unwrap();
        assert_eq!(pllrdy.hint, Some(Hint::Mirror(24)));
        assert!(cfg.intercepts.iter().all(|i| matches!(i.handler, HandlerRef::Python { .. })));
    }
}
