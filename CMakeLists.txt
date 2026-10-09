cmake_minimum_required(VERSION 3.30)

project(hal-rs C)

# qmake builds QEMU (with our glue injected) out of tree. Default to the pinned
# submodule; point QMAKE_DIR at a local checkout to develop both together.
set(QMAKE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/extern/qmake"
    CACHE PATH "qmake checkout (out-of-tree QEMU module harness)")
set(QMAKE_QEMU_TARGETS "arm-softmmu" CACHE STRING "QEMU --target-list")

add_subdirectory("${QMAKE_DIR}" qmake)
list(APPEND CMAKE_MODULE_PATH "${QMAKE_DIR}/cmake/Modules")

add_subdirectory(qemu)

# The plugin is a plain cargo cdylib: rebuilding it never relinks QEMU.
find_program(CARGO cargo HINTS "$ENV{HOME}/.cargo/bin" REQUIRED)
set(HALRS_CARGO_PROFILE "release" CACHE STRING "cargo profile for libhalrs.so")
add_custom_target(halrs_plugin ALL
  COMMAND "${CARGO}" build --profile "${HALRS_CARGO_PROFILE}" -p halrs-plugin
          --target-dir "${CMAKE_BINARY_DIR}/cargo"
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
  USES_TERMINAL
  COMMENT "cargo build libhalrs.so")
