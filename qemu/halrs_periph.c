/*
 * halrs-periph: a generic MMIO window whose reads/writes are handled by the
 * libhalrs.so plugin (SVD/RDL-derived register models, HALucinator peripheral
 * models, Python overrides). One instance per peripheral in the board layout.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/qdev-properties.h"
#include "halrs.h"

static uint64_t halrs_periph_read(void *opaque, hwaddr addr, unsigned size)
{
    HalrsPeriphState *s = opaque;

    return s->plugin->mmio_read(s->id, addr, size);
}

static void halrs_periph_write(void *opaque, hwaddr addr, uint64_t value,
                               unsigned size)
{
    HalrsPeriphState *s = opaque;

    s->plugin->mmio_write(s->id, addr, value, size);
}

static const MemoryRegionOps halrs_periph_ops = {
    .read = halrs_periph_read,
    .write = halrs_periph_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void halrs_periph_realize(DeviceState *dev, Error **errp)
{
    HalrsPeriphState *s = HALRS_PERIPH(dev);

    if (!s->plugin) {
        error_setg(errp, "halrs-periph: no plugin attached (create via -M halrs-m)");
        return;
    }
    if (!s->size) {
        error_setg(errp, "halrs-periph: 'size' must be non-zero");
        return;
    }
    memory_region_init_io(&s->iomem, OBJECT(s), &halrs_periph_ops, s,
                          s->name ? s->name : TYPE_HALRS_PERIPH, s->size);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
}

static const Property halrs_periph_properties[] = {
    DEFINE_PROP_STRING("name", HalrsPeriphState, name),
    DEFINE_PROP_UINT64("size", HalrsPeriphState, size, 0),
    DEFINE_PROP_UINT32("id", HalrsPeriphState, id, 0),
};

static void halrs_periph_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = halrs_periph_realize;
    dc->user_creatable = false;
    device_class_set_props(dc, halrs_periph_properties);
}

static const TypeInfo halrs_periph_info = {
    .name = TYPE_HALRS_PERIPH,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(HalrsPeriphState),
    .class_init = halrs_periph_class_init,
};

static void halrs_periph_register_types(void)
{
    type_register_static(&halrs_periph_info);
}

type_init(halrs_periph_register_types)
