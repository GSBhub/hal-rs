/*
 * hal-rs C ABI shared between the QEMU-side glue (halrs-m machine, halrs-periph
 * device) and the libhalrs.so TCG plugin. The plugin owns all configuration and
 * peripheral behaviour; QEMU only asks it for the board layout and forwards MMIO.
 *
 * Mirrored on the Rust side in crates/halrs-plugin/src/abi.rs; bump
 * HALRS_ABI_VERSION on any layout change.
 */
#ifndef HALRS_ABI_H
#define HALRS_ABI_H

#include <stddef.h>
#include <stdint.h>

#define HALRS_ABI_VERSION 1

#define HALRS_MEM_ROM   (1u << 0)   /* read-only to the guest */
#define HALRS_MEM_FILE  (1u << 1)   /* `file` is loaded at `base` */

typedef struct HalrsMem {
    const char *name;
    uint64_t base;
    uint64_t size;
    uint32_t flags;
    const char *file;               /* valid when HALRS_MEM_FILE is set */
} HalrsMem;

typedef struct HalrsPeriph {
    const char *name;
    uint64_t base;
    uint64_t size;
    uint32_t id;                    /* passed back on every MMIO access */
} HalrsPeriph;

typedef struct HalrsBoard {
    uint32_t abi_version;
    const char *cpu_type;           /* QOM CPU name, e.g. "cortex-m4" */
    uint32_t num_irq;
    uint32_t init_vtor;
    uint64_t sysclk_hz;
    size_t n_mem;
    const HalrsMem *mem;
    size_t n_periph;
    const HalrsPeriph *periph;
} HalrsBoard;

/* Services QEMU provides to the plugin. */
typedef struct HalrsHostOps {
    void *opaque;
    /* Drive NVIC external interrupt `irq` (takes the BQL if needed). */
    void (*set_irq)(void *opaque, uint32_t irq, int level);
} HalrsHostOps;

/* Symbols exported by libhalrs.so. */
typedef const HalrsBoard *(*halrs_board_fn)(void);
typedef void (*halrs_attach_host_fn)(const HalrsHostOps *ops);
typedef uint64_t (*halrs_mmio_read_fn)(uint32_t id, uint64_t offset, unsigned size);
typedef void (*halrs_mmio_write_fn)(uint32_t id, uint64_t offset, uint64_t value,
                                    unsigned size);
/* Optional: QEMU is shutting down; abort any handler blocked in the vCPU. */
typedef void (*halrs_shutdown_fn)(void);

#endif /* HALRS_ABI_H */
