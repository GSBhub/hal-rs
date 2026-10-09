/*
 * halrs-m: a Cortex-M machine whose whole layout (CPU, memories, peripheral
 * windows) comes from the libhalrs.so plugin. QEMU loads -plugin modules just
 * before board init, so we g_module_open() the same path (refcounting the
 * already-loaded instance) and ask it for the board description.
 *
 *   qemu-system-arm -plugin /path/libhalrs.so,config=run.json \
 *                   -M halrs-m,plugin=/path/libhalrs.so -nographic
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/boards.h"
#include "hw/irq.h"
#include "hw/loader.h"
#include "hw/qdev-clock.h"
#include "hw/qdev-properties.h"
#include "hw/arm/armv7m.h"
#include "hw/arm/boot.h"
#include "hw/arm/machines-qom.h"
#include "system/address-spaces.h"
#include "qemu/main-loop.h"
#include "system/runstate.h"
#include "target/arm/cpu-qom.h"
#include "halrs.h"

#define TYPE_HALRS_MACHINE MACHINE_TYPE_NAME("halrs-m")
OBJECT_DECLARE_SIMPLE_TYPE(HalrsMachineState, HALRS_MACHINE)

struct HalrsMachineState {
    MachineState parent_obj;

    char *plugin_path;
    HalrsPlugin plugin;
    ARMv7MState armv7m;
    HalrsHostOps host_ops;
    Notifier shutdown;
};

static void halrs_set_irq(void *opaque, uint32_t irq, int level)
{
    HalrsMachineState *s = opaque;
    bool locked = bql_locked();

    if (!locked) {
        bql_lock();
    }
    qemu_set_irq(qdev_get_gpio_in(DEVICE(&s->armv7m), irq), level);
    if (!locked) {
        bql_unlock();
    }
}

static void halrs_resolve(GModule *m, const char *sym, gpointer *out)
{
    if (!g_module_symbol(m, sym, out) || !*out) {
        error_report("halrs-m: plugin does not export '%s'", sym);
        exit(1);
    }
}

static void halrs_load_plugin(HalrsMachineState *s)
{
    const char *path = s->plugin_path ? s->plugin_path : g_getenv("HALRS_PLUGIN");

    if (!path) {
        error_report("halrs-m: set plugin=<path to libhalrs.so> (or HALRS_PLUGIN)");
        exit(1);
    }
    s->plugin.module = g_module_open(path, G_MODULE_BIND_LOCAL);
    if (!s->plugin.module) {
        error_report("halrs-m: cannot open %s: %s", path, g_module_error());
        exit(1);
    }
    halrs_resolve(s->plugin.module, "halrs_board", (gpointer *)&s->plugin.board);
    halrs_resolve(s->plugin.module, "halrs_attach_host",
                  (gpointer *)&s->plugin.attach_host);
    halrs_resolve(s->plugin.module, "halrs_mmio_read",
                  (gpointer *)&s->plugin.mmio_read);
    halrs_resolve(s->plugin.module, "halrs_mmio_write",
                  (gpointer *)&s->plugin.mmio_write);
    if (!g_module_symbol(s->plugin.module, "halrs_shutdown",
                         (gpointer *)&s->plugin.shutdown)) {
        s->plugin.shutdown = NULL;
    }
}

/*
 * Runs on the main thread before vCPUs are paused: lets the plugin unblock a
 * handler that is waiting (e.g. for UART input) inside a vCPU callback.
 */
static void halrs_on_shutdown(Notifier *n, void *opaque)
{
    HalrsMachineState *s = container_of(n, HalrsMachineState, shutdown);

    if (s->plugin.shutdown) {
        s->plugin.shutdown();
    }
}

static void halrs_map_memory(MemoryRegion *sysmem, const HalrsMem *m)
{
    MemoryRegion *mr = g_new0(MemoryRegion, 1);

    if (m->flags & HALRS_MEM_ROM) {
        memory_region_init_rom(mr, NULL, m->name, m->size, &error_fatal);
    } else {
        memory_region_init_ram(mr, NULL, m->name, m->size, &error_fatal);
    }
    memory_region_add_subregion(sysmem, m->base, mr);

    if ((m->flags & HALRS_MEM_FILE) &&
        load_image_targphys(m->file, m->base, m->size, NULL) < 0) {
        error_report("halrs-m: cannot load '%s' into %s", m->file, m->name);
        exit(1);
    }
}

static void halrs_machine_init(MachineState *machine)
{
    HalrsMachineState *s = HALRS_MACHINE(machine);
    MemoryRegion *sysmem = get_system_memory();
    const HalrsBoard *b;
    DeviceState *armv7m;
    Clock *sysclk, *refclk;
    g_autofree char *cpu_type = NULL;

    halrs_load_plugin(s);
    b = s->plugin.board();
    if (!b || b->abi_version != HALRS_ABI_VERSION) {
        error_report("halrs-m: plugin ABI %u, expected %u",
                     b ? b->abi_version : 0, HALRS_ABI_VERSION);
        exit(1);
    }

    for (size_t i = 0; i < b->n_mem; i++) {
        halrs_map_memory(sysmem, &b->mem[i]);
    }

    sysclk = clock_new(OBJECT(machine), "SYSCLK");
    clock_set_hz(sysclk, b->sysclk_hz);
    /* SysTick external reference = HCLK / 8, as on STM32. */
    refclk = clock_new(OBJECT(machine), "REFCLK");
    clock_set_mul_div(refclk, 8, 1);
    clock_set_source(refclk, sysclk);

    cpu_type = g_strdup_printf(ARM_CPU_TYPE_NAME("%s"), b->cpu_type);
    object_initialize_child(OBJECT(machine), "armv7m", &s->armv7m, TYPE_ARMV7M);
    armv7m = DEVICE(&s->armv7m);
    qdev_prop_set_uint32(armv7m, "num-irq", b->num_irq);
    qdev_prop_set_uint8(armv7m, "num-prio-bits", 4);
    qdev_prop_set_string(armv7m, "cpu-type", cpu_type);
    /*
     * STM32 HAL flips bits like RCC_CR.PLLON through the bit-band alias; the
     * alias regions sit above board memory, so they also win over a
     * catch-all peripheral window (HALucinator's "logger").
     */
    qdev_prop_set_bit(armv7m, "enable-bitband", true);
    qdev_connect_clock_in(armv7m, "cpuclk", sysclk);
    qdev_connect_clock_in(armv7m, "refclk", refclk);
    object_property_set_link(OBJECT(armv7m), "memory", OBJECT(sysmem),
                             &error_abort);
    sysbus_realize(SYS_BUS_DEVICE(armv7m), &error_fatal);

    for (size_t i = 0; i < b->n_periph; i++) {
        const HalrsPeriph *p = &b->periph[i];
        DeviceState *dev = qdev_new(TYPE_HALRS_PERIPH);

        HALRS_PERIPH(dev)->plugin = &s->plugin;
        qdev_prop_set_string(dev, "name", p->name);
        qdev_prop_set_uint64(dev, "size", p->size);
        qdev_prop_set_uint32(dev, "id", p->id);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
        /*
         * On overlap the more specific (smaller) window wins, e.g. an SVD
         * peripheral inside HALucinator's catch-all logger region; memories
         * (priority 0) win over every peripheral window.
         */
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(dev), 0, p->base,
                                -1 - (63 - clz64(MAX(p->size, 1))));
    }

    s->host_ops = (HalrsHostOps){ .opaque = s, .set_irq = halrs_set_irq };
    s->plugin.attach_host(&s->host_ops);
    s->shutdown.notify = halrs_on_shutdown;
    qemu_register_shutdown_notifier(&s->shutdown);

    /* Images were loaded per memory region; just reset into the vector table. */
    armv7m_load_kernel(s->armv7m.cpu, NULL, 0, 0);
}

static char *halrs_get_plugin(Object *obj, Error **errp)
{
    return g_strdup(HALRS_MACHINE(obj)->plugin_path);
}

static void halrs_set_plugin(Object *obj, const char *value, Error **errp)
{
    HalrsMachineState *s = HALRS_MACHINE(obj);

    g_free(s->plugin_path);
    s->plugin_path = g_strdup(value);
}

static void halrs_machine_class_init(ObjectClass *oc, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);

    mc->desc = "hal-rs configurable Cortex-M (layout from libhalrs.so)";
    mc->init = halrs_machine_init;
    mc->ignore_memory_transaction_failures = true;
    object_class_property_add_str(oc, "plugin", halrs_get_plugin, halrs_set_plugin);
    object_class_property_set_description(oc, "plugin",
                                          "Path to libhalrs.so (same as -plugin)");
}

static const TypeInfo halrs_machine_info = {
    .name = TYPE_HALRS_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(HalrsMachineState),
    .class_init = halrs_machine_class_init,
    .interfaces = arm_machine_interfaces,
};

static void halrs_machine_register_types(void)
{
    type_register_static(&halrs_machine_info);
}

type_init(halrs_machine_register_types)
