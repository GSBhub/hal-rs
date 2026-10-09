//! Embedded CPython hosting HALucinator handlers via `halrs.runtime`.
//!
//! The interpreter lives in QEMU's process. `halrs_native` is the module the
//! Python side (the avatar2-target shim) uses to reach registers, memory and
//! IRQs; the plugin calls back into `halrs.runtime.{dispatch,mmio_read,mmio_write}`.

use std::ffi::{CString, c_int, c_long, c_ulong};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use halrs_core::config::PythonConfig;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::qemu::{self, log};

struct Runtime {
    dispatch: Py<PyAny>,
    mmio_read: Py<PyAny>,
    mmio_write: Py<PyAny>,
}

static RUNTIME: OnceLock<Runtime> = OnceLock::new();
static SHUTDOWN: AtomicBool = AtomicBool::new(false);
/// Python thread id of the vCPU while it is inside a handler (0 = none).
static IN_HANDLER: AtomicU64 = AtomicU64::new(0);

unsafe extern "C" {
    // libpython; same id space as threading.get_ident() / SetAsyncExc.
    fn PyThread_get_thread_ident() -> c_ulong;
}

#[pymodule]
mod halrs_native {
    use super::*;

    fn reg(name: &str) -> PyResult<qemu::RegHandle> {
        crate::state()
            .regs
            .get()
            .and_then(|m| m.get(name).copied())
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_string()))
    }

    #[pyfunction]
    fn read_reg(name: &str) -> PyResult<u64> {
        Ok(qemu::read_reg(reg(name)?))
    }

    #[pyfunction]
    fn write_reg(name: &str, value: u64) -> PyResult<()> {
        qemu::write_reg(reg(name)?, value, 4);
        Ok(())
    }

    #[pyfunction]
    fn read_mem<'py>(py: Python<'py>, addr: u64, len: usize) -> PyResult<Bound<'py, PyBytes>> {
        let data = qemu::read_mem(addr, len).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("cannot read {len} bytes at {addr:#x}"))
        })?;
        Ok(PyBytes::new(py, &data))
    }

    #[pyfunction]
    fn write_mem(addr: u64, data: &[u8]) -> bool {
        qemu::write_mem(addr, data)
    }

    /// May be called from Python threads (zmq server); the host takes the BQL,
    /// so drop the GIL first to avoid a GIL/BQL lock-order inversion.
    #[pyfunction]
    fn set_irq(py: Python<'_>, irq: u32, level: c_int) {
        py.detach(|| crate::set_irq(irq, level != 0));
    }

    #[pyfunction]
    #[pyo3(name = "log")]
    fn py_log(msg: &str) {
        log(msg);
    }
}

/// Make libpython's symbols global: QEMU dlopens plugins RTLD_LOCAL, and C
/// extension modules (zmq, ...) expect Py* symbols in the global scope.
fn promote_libpython() {
    unsafe extern "C" {
        fn dlopen(filename: *const std::ffi::c_char, flag: c_int) -> *mut std::ffi::c_void;
    }
    const RTLD_NOW: c_int = 2;
    const RTLD_GLOBAL: c_int = 0x100;
    if let Some(path) = option_env!("HALRS_LIBPYTHON") {
        let c = CString::new(path).unwrap();
        if unsafe { dlopen(c.as_ptr(), RTLD_NOW | RTLD_GLOBAL) }.is_null() {
            log(&format!("warning: could not promote {path} to RTLD_GLOBAL"));
        }
    }
}

pub fn init(cfg: &PythonConfig) -> Result<(), String> {
    promote_libpython();
    if let Some(home) = &cfg.home {
        // Only the main thread runs at plugin install; nothing reads env concurrently.
        unsafe { std::env::set_var("PYTHONHOME", home) };
    }
    pyo3::append_to_inittab!(halrs_native);
    Python::initialize();

    Python::attach(|py| -> PyResult<()> {
        let site = py.import("site")?;
        for sp in &cfg.site_packages {
            site.call_method1("addsitedir", (sp,))?;
        }
        let sys_path = py.import("sys")?.getattr("path")?;
        for p in cfg.sys_path.iter().rev() {
            // sys.path entries must be str: the import system skips pathlib.Path.
            sys_path.call_method1("insert", (0, p.to_string_lossy()))?;
        }

        let rt = py.import("halrs.runtime")?;
        rt.call_method1(
            "setup",
            (cfg.configs.clone(), cfg.outdir.clone(), cfg.rx_port, cfg.tx_port),
        )?;
        let _ = RUNTIME.set(Runtime {
            dispatch: rt.getattr("dispatch")?.unbind(),
            mmio_read: rt.getattr("mmio_read")?.unbind(),
            mmio_write: rt.getattr("mmio_write")?.unbind(),
        });
        Ok(())
    })
    .map_err(|e| {
        Python::attach(|py| e.print(py));
        format!("python runtime setup failed: {e}")
    })
}

fn report(py: Python<'_>, what: &str, e: PyErr) {
    log(&format!("{what}: {e}"));
    e.print(py);
}

/// Run the HALucinator handler for the intercept at `addr`.
pub fn dispatch(addr: u64) {
    let Some(rt) = RUNTIME.get() else { return };
    if SHUTDOWN.load(Ordering::Acquire) {
        return;
    }
    Python::attach(|py| {
        let tid = unsafe { PyThread_get_thread_ident() };
        IN_HANDLER.store(u64::from(tid), Ordering::Release);
        let r = rt.dispatch.call1(py, (addr,));
        IN_HANDLER.store(0, Ordering::Release);
        match r {
            Err(e) if SHUTDOWN.load(Ordering::Acquire) => {
                log(&format!("handler at {addr:#x} aborted for shutdown ({e})"))
            }
            Err(e) => report(py, &format!("handler at {addr:#x} raised"), e),
            Ok(_) => {}
        }
    });
}

/// QEMU is shutting down: interrupt a handler that is blocked (e.g. waiting for
/// UART input) so the vCPU can leave its callback and be paused.
pub fn shutdown() {
    SHUTDOWN.store(true, Ordering::Release);
    if RUNTIME.get().is_none() {
        return;
    }
    // A busy-waiting handler still yields the GIL every switch interval.
    Python::attach(|py| {
        let tid = IN_HANDLER.load(Ordering::Acquire);
        if tid != 0 {
            let exc = py.get_type::<pyo3::exceptions::PyKeyboardInterrupt>();
            unsafe { pyo3::ffi::PyThreadState_SetAsyncExc(tid as c_long, exc.as_ptr()) };
        }
    });
}

pub fn mmio_read(name: &str, offset: u64, size: u32) -> u64 {
    let Some(rt) = RUNTIME.get() else { return 0 };
    Python::attach(|py| match rt.mmio_read.call1(py, (name, offset, size)) {
        Ok(v) => v.extract::<u64>(py).unwrap_or(0),
        Err(e) => {
            report(py, &format!("{name}+{offset:#x} read raised"), e);
            0
        }
    })
}

pub fn mmio_write(name: &str, offset: u64, size: u32, value: u64) {
    let Some(rt) = RUNTIME.get() else { return };
    Python::attach(|py| {
        if let Err(e) = rt.mmio_write.call1(py, (name, offset, size, value)) {
            report(py, &format!("{name}+{offset:#x} write raised"), e);
        }
    });
}
