import struct

from halrs.callgraph import propagate, vector_names


def test_vector_names_unique_slots_only():
    base = 0x0800_0000
    vec = [0x2002_0000] + [0] * 60
    vec[1] = base + 0x101          # Reset
    vec[3] = base + 0x201          # HardFault
    vec[2] = vec[4] = base + 0x301  # NMI + MemManage share a stub: unnamed
    vec[16 + 37] = base + 0x401    # IRQ 37
    image = struct.pack("<61I", *vec) + bytes(0x500)
    names = vector_names(image, base, {37: "USART1"})
    assert names == {base + 0x100: "Reset_Handler", base + 0x200: "HardFault_Handler",
                     base + 0x400: "USART1_IRQHandler"}


REF = {
    "HAL_RCC_ClockConfig": [["HAL_GetTick", "HAL_GetTick", "HAL_RCC_GetSysClockFreq",
                             "HAL_InitTick"]],
    "HAL_Init": [["HAL_NVIC_SetPriorityGrouping", "HAL_InitTick", "HAL_MspInit"]],
    "HAL_InitTick": [["HAL_RCC_GetHCLKFreq", "HAL_SYSTICK_Config", "HAL_NVIC_SetPriority"]],
}


def test_downward_from_anchor():
    calls = {0x10: [0x20, 0x20, 0x30, 0x40]}
    got = propagate({0x10: "HAL_RCC_ClockConfig"}, calls, REF)
    assert got == {0x20: "HAL_GetTick", 0x30: "HAL_RCC_GetSysClockFreq", 0x40: "HAL_InitTick"}


def test_upward_needs_two_named_callees():
    calls = {0x50: [0x60, 0x40, 0x70], 0x40: [0x80, 0x90, 0xA0]}
    # Only HAL_InitTick known: one named callee, no upward step for 0x50 yet,
    # but downward names its callees; then 0x50 has... still one named callee.
    got = propagate({0x40: "HAL_InitTick"}, calls, REF)
    assert 0x50 not in got
    got = propagate({0x40: "HAL_InitTick", 0x60: "HAL_NVIC_SetPriorityGrouping"}, calls, REF)
    assert got[0x50] == "HAL_Init" and got[0x70] == "HAL_MspInit"


def test_conflicts_and_length_mismatch_are_skipped():
    # Same length, but an anchor disagrees with the reference -> no proposals.
    calls = {0x10: [0x20, 0x21, 0x30, 0x40]}
    assert propagate({0x10: "HAL_RCC_ClockConfig", 0x21: "Other"}, calls, REF) == {}
    # Different call count (inlined/extra call) -> no proposals.
    assert propagate({0x10: "HAL_RCC_ClockConfig"}, {0x10: [0x20]}, REF) == {}


def test_cmsis_rename_is_tolerated():
    ref = {"f": [["__NVIC_SetPriorityGrouping", "g"]]}
    got = propagate({0x1: "f", 0x2: "NVIC_SetPriorityGrouping"}, {0x1: [0x2, 0x3]}, ref)
    assert got == {0x3: "g"}
