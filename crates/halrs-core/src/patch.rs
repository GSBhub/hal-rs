//! Firmware patching for intercepts.
//!
//! QEMU 10.2's plugin API cannot redirect the PC, so intercepted functions are
//! rewritten in a *copy* of each image:
//!
//! - **stub**: the entry becomes a return instruction. The plugin hooks it, runs
//!   the handler and writes r0; the stub returns. The handler can't fall through.
//! - **trampoline**: the entry branches to a trampoline (see [`crate::thumb`])
//!   that returns or runs the relocated original entry, depending on a flag the
//!   handler leaves in r12. Used for handlers that may pass through. If the entry
//!   can't be relocated, the intercept falls back to a stub and says so.
//!
//! Trampolines live in the top of the memory region holding the function, past
//! the end of the image (the least likely place for firmware to use).

use std::path::{Path, PathBuf};

use crate::Error;
use crate::config::{InterceptMode, RunConfig};
use crate::thumb;

/// Bytes of a "return to caller" instruction for `arch`.
pub fn return_stub(arch: &str) -> Option<&'static [u8]> {
    match arch {
        // Thumb `bx lr`.
        "armv7m" | "armv6m" | "armv8m" => Some(&[0x70, 0x47]),
        _ => None,
    }
}

/// How one intercept ended up being patched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hook {
    /// Instruction the plugin must hook (entry for stubs, trampoline start otherwise).
    pub addr: u64,
    pub mode: InterceptMode,
    /// Set when a requested trampoline fell back to a stub.
    pub note: Option<String>,
}

pub struct Patched {
    /// The config with file-backed memories pointing at the patched copies.
    pub cfg: RunConfig,
    /// One entry per `cfg.intercepts`, same order.
    pub hooks: Vec<Hook>,
}

fn fail(symbol: &str, addr: u64, reason: impl Into<String>) -> Error {
    Error::Patch { symbol: symbol.to_string(), addr, reason: reason.into() }
}

/// Write patched copies of every file-backed memory into `out_dir`. Intercepts
/// outside any file-backed memory are hooked as-is (code loaded at runtime).
pub fn patch_images(cfg: &RunConfig, out_dir: &Path) -> Result<Patched, Error> {
    let stub = return_stub(&cfg.arch)
        .ok_or_else(|| fail("", 0, format!("no return stub for arch '{}'", cfg.arch)))?;
    std::fs::create_dir_all(out_dir).map_err(|e| Error::Io(out_dir.to_path_buf(), e))?;

    let mut out = cfg.clone();
    let mut hooks: Vec<Hook> = cfg
        .intercepts
        .iter()
        .map(|ic| Hook { addr: ic.addr, mode: InterceptMode::Stub, note: None })
        .collect();

    for (mi, mem) in out.memory.iter_mut().enumerate() {
        let Some(src) = mem.file.clone() else { continue };
        let original = std::fs::read(&src).map_err(|e| Error::Io(src.clone(), e))?;
        let mut image = original.clone();
        let mut touched = false;
        // Trampoline slots grow down from the top of the region.
        let mut next_slot = mem.size;

        for (ii, ic) in cfg.intercepts.iter().enumerate() {
            if !mem.contains(ic.addr) {
                continue;
            }
            let off = (ic.addr - mem.base) as usize;
            if off + stub.len() > original.len() {
                continue;
            }
            touched = true;

            if ic.mode == InterceptMode::Trampoline {
                let slot = next_slot.checked_sub(thumb::SLOT).filter(|s| *s >= original.len() as u64);
                let built = slot.and_then(|s| {
                    thumb::trampoline(ic.addr, &original[off..], mem.base + s).map(|t| (s, t))
                });
                match built {
                    Some((s, (entry_patch, tramp))) => {
                        let s = s as usize;
                        if image.len() < s + tramp.len() {
                            image.resize(s + tramp.len(), 0);
                        }
                        image[s..s + tramp.len()].copy_from_slice(&tramp);
                        image[off..off + 4].copy_from_slice(&entry_patch);
                        next_slot -= thumb::SLOT;
                        hooks[ii] = Hook {
                            addr: mem.base + s as u64,
                            mode: InterceptMode::Trampoline,
                            note: None,
                        };
                        continue;
                    }
                    None => {
                        hooks[ii].note = Some(if slot.is_none() {
                            format!("no room for a trampoline in '{}'", mem.name)
                        } else {
                            "entry instructions can't be relocated".into()
                        });
                    }
                }
            }
            image[off..off + stub.len()].copy_from_slice(stub);
        }

        if touched {
            let name = src.file_name().map(|n| n.to_string_lossy().into_owned());
            let dst: PathBuf = out_dir.join(format!("{mi}-{}", name.unwrap_or_default()));
            std::fs::write(&dst, &image).map_err(|e| Error::Io(dst.clone(), e))?;
            mem.file = Some(dst);
        }
    }
    Ok(Patched { cfg: out, hooks })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{HandlerRef, Intercept};

    fn setup(name: &str, image: &[u8]) -> (PathBuf, RunConfig) {
        let dir = std::env::temp_dir().join(format!("halrs-patch-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fw = dir.join("fw.bin");
        std::fs::write(&fw, image).unwrap();
        let cfg = RunConfig::from_json(&format!(
            r#"{{"memory":[
                {{"name":"alias","base":0,"size":4096,"rom":true,"file":"{0}"}},
                {{"name":"flash","base":134217728,"size":4096,"rom":true,"file":"{0}"}}]}}"#,
            fw.display()
        ))
        .unwrap();
        (dir, cfg)
    }

    fn intercept(addr: u64, mode: InterceptMode) -> Intercept {
        Intercept {
            symbol: "f".into(),
            addr,
            mode,
            handler: HandlerRef::Return { value: Some(0) },
        }
    }

    #[test]
    fn stub_patches_only_matching_region() {
        let (dir, mut cfg) = setup("stub", &[0u8; 0x100]);
        cfg.intercepts.push(intercept(0x0800_0010, InterceptMode::Stub));

        let p = patch_images(&cfg, &dir.join("out")).unwrap();
        assert_eq!(p.cfg.memory[0].file, cfg.memory[0].file, "alias untouched");
        let patched = std::fs::read(p.cfg.memory[1].file.as_ref().unwrap()).unwrap();
        assert_eq!(&patched[0x10..0x12], &[0x70, 0x47]);
        assert_eq!(p.hooks[0].addr, 0x0800_0010);
        assert_eq!(std::fs::read(dir.join("fw.bin")).unwrap()[0x10], 0, "original untouched");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn trampoline_at_top_of_region() {
        let mut img = vec![0u8; 0x100];
        img[0x10..0x14].copy_from_slice(&[0x80, 0xb5, 0x82, 0xb0]); // push {r7,lr}; sub sp,#8
        img[0x20..0x24].copy_from_slice(&[0x03, 0x4b, 0x00, 0xbf]); // ldr r3,[pc,#12]: not relocatable
        let (dir, mut cfg) = setup("tramp", &img);
        cfg.intercepts.push(intercept(0x0800_0010, InterceptMode::Trampoline));
        cfg.intercepts.push(intercept(0x0800_0020, InterceptMode::Trampoline));

        let p = patch_images(&cfg, &dir.join("out")).unwrap();
        let patched = std::fs::read(p.cfg.memory[1].file.as_ref().unwrap()).unwrap();
        let slot = 4096 - thumb::SLOT;

        assert_eq!(p.hooks[0].mode, InterceptMode::Trampoline);
        assert_eq!(p.hooks[0].addr, 0x0800_0000 + slot);
        assert_eq!(&patched[0x10..0x14], &thumb::b_w(0x0800_0010, 0x0800_0000 + slot).unwrap());
        assert_eq!(&patched[slot as usize..slot as usize + 4], &[0xbc, 0xf1, 0x00, 0x0f]);

        assert_eq!(p.hooks[1].mode, InterceptMode::Stub, "falls back");
        assert!(p.hooks[1].note.as_deref().unwrap().contains("relocated"));
        assert_eq!(&patched[0x20..0x22], &[0x70, 0x47]);
        std::fs::remove_dir_all(&dir).ok();
    }
}
