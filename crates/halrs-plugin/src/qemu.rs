//! Thin safe-ish wrappers over the QEMU plugin API and the bits of glib it needs.

use std::collections::HashMap;
use std::ffi::{CStr, CString, c_char, c_uint};

use qemu_plugin_sys::{
    GArray, GByteArray, qemu_plugin_get_registers, qemu_plugin_outs, qemu_plugin_read_memory_vaddr,
    qemu_plugin_read_register, qemu_plugin_reg_descriptor, qemu_plugin_register,
    qemu_plugin_write_memory_vaddr, qemu_plugin_write_register,
};

// glib is always loaded in the QEMU process; the plugin resolves these at dlopen.
unsafe extern "C" {
    fn g_byte_array_new() -> *mut GByteArray;
    fn g_byte_array_free(array: *mut GByteArray, free_segment: bool) -> *mut u8;
    fn g_byte_array_append(array: *mut GByteArray, data: *const u8, len: c_uint) -> *mut GByteArray;
    fn g_array_free(array: *mut GArray, free_segment: bool) -> *mut c_char;
}

pub fn log(msg: &str) {
    if let Ok(c) = CString::new(format!("halrs: {msg}\n")) {
        unsafe { qemu_plugin_outs(c.as_ptr()) };
    }
}

/// Owned glib byte array.
struct Bytes(*mut GByteArray);

impl Bytes {
    fn new() -> Self {
        Bytes(unsafe { g_byte_array_new() })
    }
    fn from(data: &[u8]) -> Self {
        let b = Self::new();
        unsafe { g_byte_array_append(b.0, data.as_ptr(), data.len() as c_uint) };
        b
    }
    fn as_slice(&self) -> &[u8] {
        unsafe {
            let a = &*self.0;
            if a.data.is_null() { &[] } else { std::slice::from_raw_parts(a.data, a.len as usize) }
        }
    }
}

impl Drop for Bytes {
    fn drop(&mut self) {
        unsafe { g_byte_array_free(self.0, true) };
    }
}

#[derive(Clone, Copy)]
pub struct RegHandle(*mut qemu_plugin_register);
// Handles are opaque integers in QEMU, valid for the whole run.
unsafe impl Send for RegHandle {}
unsafe impl Sync for RegHandle {}

/// Register handles by lower-case name; must be built in vCPU context.
pub fn register_map() -> HashMap<String, RegHandle> {
    let mut map = HashMap::new();
    unsafe {
        let arr = qemu_plugin_get_registers();
        if arr.is_null() {
            return map;
        }
        let a = &*arr;
        let descs = std::slice::from_raw_parts(a.data as *const qemu_plugin_reg_descriptor, a.len as usize);
        for d in descs {
            let name = CStr::from_ptr(d.name).to_string_lossy().to_ascii_lowercase();
            map.insert(name, RegHandle(d.handle));
        }
        g_array_free(arr, true);
    }
    map
}

/// Read a register (target byte order; ARM is little endian). Only valid inside a
/// callback registered with R/RW register access.
pub fn read_reg(h: RegHandle) -> u64 {
    let buf = Bytes::new();
    let n = unsafe { qemu_plugin_read_register(h.0, buf.0) };
    if n <= 0 {
        return 0;
    }
    let mut v = [0u8; 8];
    let s = buf.as_slice();
    let len = s.len().min(8);
    v[..len].copy_from_slice(&s[..len]);
    u64::from_le_bytes(v)
}

pub fn write_reg(h: RegHandle, value: u64, width: usize) {
    let buf = Bytes::from(&value.to_le_bytes()[..width]);
    unsafe { qemu_plugin_write_register(h.0, buf.0) };
}

pub fn read_mem(addr: u64, len: usize) -> Option<Vec<u8>> {
    if len == 0 {
        return Some(Vec::new());
    }
    let buf = Bytes::new();
    unsafe { qemu_plugin_read_memory_vaddr(addr, buf.0, len) }.then(|| buf.as_slice().to_vec())
}

pub fn write_mem(addr: u64, data: &[u8]) -> bool {
    if data.is_empty() {
        return true;
    }
    let buf = Bytes::from(data);
    unsafe { qemu_plugin_write_memory_vaddr(addr, buf.0) }
}
