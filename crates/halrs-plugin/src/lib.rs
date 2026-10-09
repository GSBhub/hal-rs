//! libhalrs.so — QEMU TCG plugin that hosts hal-rs.
//!
//!   qemu-system-arm -plugin libhalrs.so,config=run.json[,outdir=DIR] \
//!                   -M halrs-m,plugin=libhalrs.so
//!
//! At install it loads `run.json`, writes intercept-patched firmware copies, and
//! builds the board layout the `halrs-m` machine asks for via `halrs_board()`.
//! Peripheral MMIO arrives through `halrs_mmio_{read,write}`; intercepted
//! functions are hooked only on their (patched) entry instruction.

mod abi;
#[cfg(feature = "python")]
mod py;
mod qemu;

use std::collections::HashMap;
use std::ffi::{CStr, c_char, c_int, c_uint, c_void};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};

use halrs_core::config::{HandlerRef, ModelKind, RunConfig};
use halrs_core::patch::patch_images;
use halrs_core::regfile::{PeriphModel, ReadNote};
use qemu_plugin_sys::{
    QEMU_PLUGIN_VERSION, qemu_info_t, qemu_plugin_cb_flags, qemu_plugin_id_t,
    qemu_plugin_insn_vaddr, qemu_plugin_register_atexit_cb, qemu_plugin_register_vcpu_init_cb,
    qemu_plugin_register_vcpu_insn_exec_cb, qemu_plugin_register_vcpu_tb_trans_cb,
    qemu_plugin_tb, qemu_plugin_tb_get_insn, qemu_plugin_tb_n_insns,
};

use abi::{Board, HalrsBoard, HalrsHostOps};
use qemu::{RegHandle, log};

#[unsafe(no_mangle)]
pub static qemu_plugin_version: c_int = QEMU_PLUGIN_VERSION as c_int;

struct Host {
    opaque: usize,
    set_irq: unsafe extern "C" fn(*mut c_void, u32, c_int),
}

struct State {
    cfg: RunConfig,
    board: Board,
    /// Patched entry address -> index into `cfg.intercepts`.
    intercepts: HashMap<u64, usize>,
    hits: Vec<AtomicU64>,
    models: Mutex<Vec<PeriphModel>>,
    regs: OnceLock<HashMap<String, RegHandle>>,
    host: Mutex<Option<Host>>,
    trace_mmio: bool,
}

static STATE: OnceLock<State> = OnceLock::new();

fn state() -> &'static State {
    STATE.get().expect("libhalrs: used before qemu_plugin_install")
}

fn parse_args(argc: c_int, argv: *mut *mut c_char) -> HashMap<String, String> {
    let mut args = HashMap::new();
    for i in 0..argc.max(0) as usize {
        let s = unsafe { CStr::from_ptr(*argv.add(i)) }.to_string_lossy();
        if let Some((k, v)) = s.split_once('=') {
            args.insert(k.to_string(), v.to_string());
        }
    }
    args
}

fn install(args: &HashMap<String, String>) -> Result<State, String> {
    let path = args.get("config").ok_or("missing config=<run.json>")?;
    let cfg = RunConfig::load(&PathBuf::from(path)).map_err(|e| e.to_string())?;
    let outdir = args
        .get("outdir")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::temp_dir().join(format!("halrs-{}", std::process::id())));
    let cfg = patch_images(&cfg, &outdir).map_err(|e| e.to_string())?;

    let intercepts = cfg.intercepts.iter().enumerate().map(|(i, ic)| (ic.addr, i)).collect();
    let hits = cfg.intercepts.iter().map(|_| AtomicU64::new(0)).collect();
    let models = cfg.peripherals.iter().map(PeriphModel::new).collect();
    log(&format!(
        "loaded {path}: {} memories, {} peripherals, {} intercepts",
        cfg.memory.len(),
        cfg.peripherals.len(),
        cfg.intercepts.len()
    ));
    Ok(State {
        board: Board::new(&cfg),
        cfg,
        intercepts,
        hits,
        models: Mutex::new(models),
        regs: OnceLock::new(),
        host: Mutex::new(None),
        trace_mmio: args.get("trace-mmio").is_some_and(|v| v == "on"),
    })
}

/// # Safety
/// Called by QEMU's plugin loader with valid `argv`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn qemu_plugin_install(
    id: qemu_plugin_id_t,
    _info: *const qemu_info_t,
    argc: c_int,
    argv: *mut *mut c_char,
) -> c_int {
    let args = parse_args(argc, argv);
    let st = match install(&args) {
        Ok(st) => st,
        Err(e) => {
            log(&format!("install failed: {e}"));
            return -1;
        }
    };
    if STATE.set(st).is_err() {
        log("install called twice");
        return -1;
    }
    if let Err(e) = init_python() {
        log(&e);
        return -1;
    }
    unsafe {
        qemu_plugin_register_vcpu_init_cb(id, Some(vcpu_init));
        qemu_plugin_register_vcpu_tb_trans_cb(id, Some(tb_trans));
        qemu_plugin_register_atexit_cb(id, Some(at_exit), std::ptr::null_mut());
    }
    0
}

#[cfg(feature = "python")]
fn init_python() -> Result<(), String> {
    match &state().cfg.python {
        Some(pc) => py::init(pc),
        None => Ok(()),
    }
}

#[cfg(not(feature = "python"))]
fn init_python() -> Result<(), String> {
    match &state().cfg.python {
        Some(_) => Err("run config needs Python but libhalrs was built without it".into()),
        None => Ok(()),
    }
}

unsafe extern "C" fn vcpu_init(_id: qemu_plugin_id_t, _vcpu: c_uint) {
    let st = state();
    let _ = st.regs.set(qemu::register_map());
}

unsafe extern "C" fn tb_trans(_id: qemu_plugin_id_t, tb: *mut qemu_plugin_tb) {
    let st = state();
    if st.intercepts.is_empty() {
        return;
    }
    unsafe {
        for i in 0..qemu_plugin_tb_n_insns(tb) {
            let insn = qemu_plugin_tb_get_insn(tb, i);
            if let Some(&idx) = st.intercepts.get(&qemu_plugin_insn_vaddr(insn)) {
                qemu_plugin_register_vcpu_insn_exec_cb(
                    insn,
                    Some(on_intercept),
                    qemu_plugin_cb_flags::QEMU_PLUGIN_CB_RW_REGS,
                    idx as *mut c_void,
                );
            }
        }
    }
}

unsafe extern "C" fn on_intercept(_vcpu: c_uint, udata: *mut c_void) {
    let st = state();
    let idx = udata as usize;
    let ic = &st.cfg.intercepts[idx];
    st.hits[idx].fetch_add(1, Ordering::Relaxed);
    let regs = st.regs.get().expect("vcpu_init ran");

    let ret = match &ic.handler {
        HandlerRef::Return { value } => *value,
        HandlerRef::Python { .. } => {
            // The shim writes r0 itself (HALucinator's execute_return).
            #[cfg(feature = "python")]
            py::dispatch(ic.addr);
            None
        }
    };
    if let (Some(v), Some(&r0)) = (ret, regs.get("r0")) {
        qemu::write_reg(r0, v, 4);
    }
}

unsafe extern "C" fn at_exit(_id: qemu_plugin_id_t, _udata: *mut c_void) {
    let st = state();
    for (ic, n) in st.cfg.intercepts.iter().zip(&st.hits) {
        let n = n.load(Ordering::Relaxed);
        if n > 0 {
            log(&format!("intercept {} hit {n}x", ic.symbol));
        }
    }
}

// ---- C ABI used by the halrs-m machine / halrs-periph device -------------------

#[unsafe(no_mangle)]
pub extern "C" fn halrs_board() -> *const HalrsBoard {
    match STATE.get() {
        Some(st) => &st.board.c,
        None => std::ptr::null(),
    }
}

/// # Safety
/// `ops` must point to a `HalrsHostOps` that outlives the plugin.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn halrs_attach_host(ops: *const HalrsHostOps) {
    let ops = unsafe { &*ops };
    if let Some(set_irq) = ops.set_irq {
        *state().host.lock().unwrap() = Some(Host { opaque: ops.opaque as usize, set_irq });
    }
}

/// Drive an NVIC external interrupt line.
pub fn set_irq(irq: u32, level: bool) {
    if let Some(h) = state().host.lock().unwrap().as_ref() {
        unsafe { (h.set_irq)(h.opaque as *mut c_void, irq, level as c_int) };
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn halrs_shutdown() {
    #[cfg(feature = "python")]
    py::shutdown();
}

#[unsafe(no_mangle)]
pub extern "C" fn halrs_mmio_read(id: u32, offset: u64, size: c_uint) -> u64 {
    let st = state();
    #[cfg(feature = "python")]
    if let Some(p) = st.cfg.peripherals.get(id as usize)
        && p.model == ModelKind::Python
    {
        return py::mmio_read(&p.name, offset, size);
    }
    let mut models = st.models.lock().unwrap();
    let Some(m) = models.get_mut(id as usize) else { return 0 };
    let (v, note) = m.read(offset, size);
    if note == ReadNote::PollBroken {
        log(&format!("{}+{offset:#x}: poll loop detected, returning {v:#x}", m.name));
    }
    if st.trace_mmio {
        log(&format!("rd {}+{offset:#x}/{size} -> {v:#x}", m.name));
    }
    v
}

#[unsafe(no_mangle)]
pub extern "C" fn halrs_mmio_write(id: u32, offset: u64, value: u64, size: c_uint) {
    let st = state();
    #[cfg(feature = "python")]
    if let Some(p) = st.cfg.peripherals.get(id as usize)
        && p.model == ModelKind::Python
    {
        return py::mmio_write(&p.name, offset, size, value);
    }
    let mut models = st.models.lock().unwrap();
    let Some(m) = models.get_mut(id as usize) else { return };
    if st.trace_mmio {
        log(&format!("wr {}+{offset:#x}/{size} <- {value:#x}", m.name));
    }
    m.write(offset, value, size);
}
