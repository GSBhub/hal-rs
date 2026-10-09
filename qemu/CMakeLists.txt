# QEMU-side glue for hal-rs: the generic halrs-periph device and the halrs-m
# Cortex-M machine. Everything else (layout, models, handlers) lives in the
# libhalrs.so plugin, so this only needs rebuilding when halrs_abi.h changes.
include(QmakeAddModule)

qmake_add_qemu_module(
  NAME     halrs
  LANG     C
  SCOPE    TARGET
  TARGETS  arm
  SOURCES  halrs_machine.c halrs_periph.c
  HEADERS  halrs.h halrs_abi.h
  SELECTS  ARM_V7M)
