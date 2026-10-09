//! Firmware patching for intercepts.
//!
//! QEMU 10.2's plugin API cannot redirect the PC, so an intercepted function's
//! entry is rewritten (in a *copy* of each image) to a return stub. The plugin
//! hooks the stub instruction, runs the handler, and writes the return value;
//! the stub then returns to the caller. Nothing else pays a hook cost.

use std::path::{Path, PathBuf};

use crate::Error;
use crate::config::{InterceptMode, RunConfig};

/// Bytes of a "return to caller" instruction for `arch`.
pub fn return_stub(arch: &str) -> Option<&'static [u8]> {
    match arch {
        // Thumb `bx lr`.
        "armv7m" | "armv6m" | "armv8m" => Some(&[0x70, 0x47]),
        _ => None,
    }
}

/// Write patched copies of every file-backed memory into `out_dir` and return a
/// config pointing at them. Intercepts outside any file-backed memory are left
/// to the caller (they may live in RAM loaded at runtime).
pub fn patch_images(cfg: &RunConfig, out_dir: &Path) -> Result<RunConfig, Error> {
    let stub = return_stub(&cfg.arch).ok_or_else(|| Error::Patch {
        symbol: String::new(),
        addr: 0,
        reason: format!("no return stub for arch '{}'", cfg.arch),
    })?;
    std::fs::create_dir_all(out_dir).map_err(|e| Error::Io(out_dir.to_path_buf(), e))?;

    let mut out = cfg.clone();
    for (i, mem) in out.memory.iter_mut().enumerate() {
        let Some(src) = mem.file.clone() else { continue };
        let mut image = std::fs::read(&src).map_err(|e| Error::Io(src.clone(), e))?;
        let mut touched = false;

        for ic in &cfg.intercepts {
            if !mem.contains(ic.addr) {
                continue;
            }
            let off = (ic.addr - mem.base) as usize;
            if off + stub.len() > image.len() {
                continue;
            }
            if ic.mode == InterceptMode::Trampoline {
                return Err(Error::Patch {
                    symbol: ic.symbol.clone(),
                    addr: ic.addr,
                    reason: "trampoline intercepts are not implemented yet".into(),
                });
            }
            image[off..off + stub.len()].copy_from_slice(stub);
            touched = true;
        }

        if touched {
            let name = src.file_name().map(|n| n.to_string_lossy().into_owned());
            let dst: PathBuf = out_dir.join(format!("{i}-{}", name.unwrap_or_default()));
            std::fs::write(&dst, &image).map_err(|e| Error::Io(dst.clone(), e))?;
            mem.file = Some(dst);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{HandlerRef, Intercept, Memory};

    #[test]
    fn patches_only_matching_region() {
        let dir = std::env::temp_dir().join(format!("halrs-patch-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fw = dir.join("fw.bin");
        std::fs::write(&fw, vec![0u8; 0x100]).unwrap();

        let cfg = RunConfig::from_json(&format!(
            r#"{{"memory":[
                {{"name":"alias","base":0,"size":4096,"rom":true,"file":"{0}"}},
                {{"name":"flash","base":134217728,"size":4096,"rom":true,"file":"{0}"}}]}}"#,
            fw.display()
        ))
        .unwrap();
        let mut cfg = cfg;
        cfg.intercepts.push(Intercept {
            symbol: "f".into(),
            addr: 0x0800_0010,
            mode: InterceptMode::Stub,
            handler: HandlerRef::Return { value: Some(0) },
        });

        let out = patch_images(&cfg, &dir.join("out")).unwrap();
        assert_eq!(out.memory[0].file, cfg.memory[0].file, "alias untouched");
        let patched = std::fs::read(out.memory[1].file.as_ref().unwrap()).unwrap();
        assert_eq!(&patched[0x10..0x12], &[0x70, 0x47]);
        assert_eq!(std::fs::read(&fw).unwrap()[0x10], 0, "original untouched");
        std::fs::remove_dir_all(&dir).ok();
    }
}
