//! hal-rs core: the run configuration (IR), data-driven peripheral models and
//! firmware patching. Independent of QEMU so it can be unit tested on its own.

pub mod config;
pub mod patch;
pub mod regfile;

use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}: {1}")]
    Io(PathBuf, std::io::Error),
    #[error("invalid run config: {0}")]
    Json(#[from] serde_json::Error),
    #[error("intercept {symbol}@{addr:#x}: {reason}")]
    Patch { symbol: String, addr: u64, reason: String },
}
