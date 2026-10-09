"""Test handlers: raise USART1's IRQ from a HALucinator-style handler and count
entries into its ISR (exercises shim -> halrs_native.set_irq -> BQL -> NVIC)."""

from halucinator.bp_handlers.bp_handler import BPHandler, bp_handler

USART1_IRQ = 37


class IrqProbe(BPHandler):
    def __init__(self):
        self.raised = False
        self.fired = 0

    @bp_handler(["HAL_UART_GetState"])
    def get_state(self, qemu, addr):
        if not self.raised:
            self.raised = True
            qemu.enable_interrupt(USART1_IRQ)
            qemu.irq_set(USART1_IRQ)
        return True, 0x20  # READY

    @bp_handler(["USART1_IRQHandler"])
    def isr(self, qemu, addr):
        self.fired += 1
        qemu.irq_clear(USART1_IRQ)
        import halrs_native  # only exists inside libhalrs.so

        halrs_native.log("USART1 ISR entered (%d)" % self.fired)
        return True, None
