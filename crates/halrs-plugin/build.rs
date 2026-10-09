//! With the `python` feature, record where libpython lives so the plugin can
//! rpath it and re-open it RTLD_GLOBAL at runtime (QEMU loads plugins
//! RTLD_LOCAL, which would hide Py* symbols from C extension modules).
fn main() {
    #[cfg(feature = "python")]
    {
        let cfg = pyo3_build_config::get();
        if let Some(dir) = cfg.lib_dir() {
            println!("cargo:rustc-link-arg-cdylib=-Wl,-rpath,{dir}");
            let lib = cfg.lib_name().unwrap_or("python3");
            println!("cargo:rustc-env=HALRS_LIBPYTHON={dir}/lib{lib}.so");
        }
        pyo3_build_config::use_pyo3_cfgs();
    }
}
