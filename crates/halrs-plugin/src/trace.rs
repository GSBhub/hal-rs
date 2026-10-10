//! Structured run trace (`trace=<path>`): one JSON object per line.
//!
//! Every event carries `t` (host nanoseconds since install) and `ev`:
//!
//! - `start`: the board layout (`peripherals`, `intercepts`), written once.
//! - `mmio`: `op` (`r`/`w`), `p` (peripheral), `off`, `size`, `val`, plus
//!   `poll: true` when the poll-loop detector forced the value.
//! - `call`: an intercepted function (`sym`, `addr`, `handled`: false if the
//!   handler passed through to the original).
//! - `irq`: an interrupt line driven by a handler (`irq`, `level`).
//! - `exit`: per-intercept hit counts.
//!
//! Lines are buffered and flushed at exit and on QEMU shutdown.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::Instant;

use serde_json::{Value, json};

pub struct Trace {
    out: Mutex<BufWriter<File>>,
    start: Instant,
}

impl Trace {
    pub fn create(path: &Path) -> Result<Self, String> {
        let f = File::create(path).map_err(|e| format!("trace {}: {e}", path.display()))?;
        Ok(Self { out: Mutex::new(BufWriter::with_capacity(1 << 16, f)), start: Instant::now() })
    }

    /// Write `fields` (a JSON object) as one event of kind `ev`.
    pub fn event(&self, ev: &str, mut fields: Value) {
        let t = self.start.elapsed().as_nanos() as u64;
        if let Value::Object(m) = &mut fields {
            m.insert("t".into(), json!(t));
            m.insert("ev".into(), json!(ev));
        }
        let mut out = self.out.lock().unwrap();
        let _ = serde_json::to_writer(&mut *out, &fields);
        let _ = out.write_all(b"\n");
    }

    pub fn flush(&self) {
        let _ = self.out.lock().unwrap().flush();
    }
}
