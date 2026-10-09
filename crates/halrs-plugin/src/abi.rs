//! Rust mirror of `qemu/halrs_abi.h`. Keep the two in lock step.

use std::ffi::{CString, c_char, c_int, c_void};
use std::ptr;

use halrs_core::config::RunConfig;

pub const HALRS_ABI_VERSION: u32 = 1;
pub const HALRS_MEM_ROM: u32 = 1 << 0;
pub const HALRS_MEM_FILE: u32 = 1 << 1;

#[repr(C)]
pub struct HalrsMem {
    pub name: *const c_char,
    pub base: u64,
    pub size: u64,
    pub flags: u32,
    pub file: *const c_char,
}

#[repr(C)]
pub struct HalrsPeriph {
    pub name: *const c_char,
    pub base: u64,
    pub size: u64,
    pub id: u32,
}

#[repr(C)]
pub struct HalrsBoard {
    pub abi_version: u32,
    pub cpu_type: *const c_char,
    pub num_irq: u32,
    pub init_vtor: u32,
    pub sysclk_hz: u64,
    pub n_mem: usize,
    pub mem: *const HalrsMem,
    pub n_periph: usize,
    pub periph: *const HalrsPeriph,
}

#[repr(C)]
pub struct HalrsHostOps {
    pub opaque: *mut c_void,
    pub set_irq: Option<unsafe extern "C" fn(*mut c_void, u32, c_int)>,
}

/// Owns the C view of a [`RunConfig`]; the pointers inside stay valid while it lives.
pub struct Board {
    _strings: Vec<CString>,
    _mem: Vec<HalrsMem>,
    _periph: Vec<HalrsPeriph>,
    pub c: HalrsBoard,
}

// The board is built once at install and only read afterwards.
unsafe impl Send for Board {}
unsafe impl Sync for Board {}

impl Board {
    pub fn new(cfg: &RunConfig) -> Self {
        let mut strings = Vec::new();
        let mut cstr = |s: &str| {
            let c = CString::new(s).expect("NUL in config string");
            let p = c.as_ptr();
            strings.push(c);
            p
        };

        let cpu_type = cstr(&cfg.cpu);
        let mem: Vec<HalrsMem> = cfg
            .memory
            .iter()
            .map(|m| {
                let mut flags = if m.rom { HALRS_MEM_ROM } else { 0 };
                let file = match &m.file {
                    Some(f) => {
                        flags |= HALRS_MEM_FILE;
                        cstr(&f.to_string_lossy())
                    }
                    None => ptr::null(),
                };
                HalrsMem { name: cstr(&m.name), base: m.base, size: m.size, flags, file }
            })
            .collect();
        let periph: Vec<HalrsPeriph> = cfg
            .peripherals
            .iter()
            .enumerate()
            .map(|(i, p)| HalrsPeriph {
                name: cstr(&p.name),
                base: p.base,
                size: p.size,
                id: i as u32,
            })
            .collect();

        let c = HalrsBoard {
            abi_version: HALRS_ABI_VERSION,
            cpu_type,
            num_irq: cfg.num_irq,
            init_vtor: 0,
            sysclk_hz: cfg.sysclk_hz,
            n_mem: mem.len(),
            mem: mem.as_ptr(),
            n_periph: periph.len(),
            periph: periph.as_ptr(),
        };
        Board { _strings: strings, _mem: mem, _periph: periph, c }
    }
}
