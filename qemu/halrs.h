/*
 * QEMU-side types for hal-rs: the generic `halrs-periph` SysBus device whose MMIO
 * is forwarded to libhalrs.so, and the plugin symbol table resolved by halrs-m.
 */
#ifndef HALRS_H
#define HALRS_H

#include <gmodule.h>
#include "hw/sysbus.h"
#include "halrs_abi.h"

#define TYPE_HALRS_PERIPH "halrs-periph"
OBJECT_DECLARE_SIMPLE_TYPE(HalrsPeriphState, HALRS_PERIPH)

/* Entry points resolved from the already-loaded plugin. */
typedef struct HalrsPlugin {
    GModule *module;
    halrs_board_fn board;
    halrs_attach_host_fn attach_host;
    halrs_mmio_read_fn mmio_read;
    halrs_mmio_write_fn mmio_write;
    halrs_shutdown_fn shutdown;     /* optional */
} HalrsPlugin;

struct HalrsPeriphState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    char *name;
    uint64_t size;
    uint32_t id;
    const HalrsPlugin *plugin;      /* set by the machine before realize */
};

#endif /* HALRS_H */
