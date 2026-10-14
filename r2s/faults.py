"""Fault injection: the catalogue of bugs a scenario can switch on.

Each fault has a type, a one-line description and the knob it turns. Boot
faults are applied when the image is written (``r2s.boot.write_image``);
program faults change the builder options; kernel faults change the kernel
construction options. Keeping them in one table makes ``explain`` and the
viewer able to say what was injected.

    type                       where it acts         what it models
    -------------------------  --------------------  ---------------------------------------
    missing_bss_zero           startup code          reset handler skips the .bss loop
    missing_data_copy          startup code          reset handler skips the .data loop
    wrong_initial_sp           vector table word 0   linker script puts the stack nowhere
    bad_reset_vector           vector table word 1   reset vector points at data / nowhere
    vector_thumb_bit_missing   vector table entry    handler address without bit 0 set
    misaligned_vector_table    VTOR                  table not where the core expects it
    irq_not_enabled            main                  NVIC_ISER never written for the IRQ
    missing_irq_handler        vector table          IRQ vector left at Default_Handler
    pendsv_forgets_register    PendSV_Handler        software frame misses a callee-saved register
    pendsv_priority_not_lowest kernel init (SHPR3)   PendSV can pre-empt other handlers
    svc_return_in_live_r0      SVC_Handler           result written to r0, not the stacked r0
    no_mpu                     kernel init           stack guards not programmed
    task_overflows_stack       task C                deep recursion past the stack bottom
    task_privileged_op         task C                cpsid / SysTick write from unprivileged code
    systick_reload_off_by_one  SysTick setup         RVR = N instead of N - 1
    wrong_clock_assumption     SysTick setup         reload computed for 150 MHz, core runs slower
    timer_intr_not_cleared     Timer handler         level-triggered IRQ never acknowledged
    irq_priority_inversion     NVIC_IPR              urgent IRQ given the same/lower priority

Author: Farzana Akhter
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

FAULT_CATALOG: Dict[str, str] = {
    "missing_bss_zero": "Reset handler does not zero .bss: uninitialised globals keep power-on garbage.",
    "missing_data_copy": "Reset handler does not copy .data from flash: initialised globals are wrong.",
    "wrong_initial_sp": "Vector table word 0 holds a bad MSP: the first push (or exception entry) faults.",
    "bad_reset_vector": "Vector table word 1 does not point at code: core fetches garbage after reset.",
    "misaligned_vector_table": "The table is not at the address VTOR points to: wrong SP/PC at reset.",
    "pendsv_forgets_register": "PendSV saves/restores an incomplete register list: tasks corrupt each other.",
    "no_mpu": "The kernel never enables the MPU: a stack overflow silently corrupts the neighbour.",
    "task_overflows_stack": "Task C recurses without bound and runs off the bottom of its stack.",
    "task_privileged_op": "Task C executes a privileged instruction / touches SysTick from thread mode.",
    "timer_intr_not_cleared": "Timer handler forgets the write-1-to-clear: the IRQ re-fires forever.",
    "irq_priority_inversion": "The urgent button IRQ is not more urgent than the slow timer IRQ.",
    "vector_thumb_bit_missing": "A vector table entry has bit 0 clear: the handler starts with xPSR.T=0 and faults INVSTATE.",
    "irq_not_enabled": "main never sets the IRQ's bit in NVIC_ISER: the edge is latched by the peripheral, never taken.",
    "missing_irq_handler": "The IRQ is enabled but its vector still points at Default_Handler.",
    "pendsv_priority_not_lowest": "PendSV is given a priority above other handlers: the switch can run inside an ISR.",
    "svc_return_in_live_r0": "The SVC handler writes its result to r0 instead of the stacked r0: the caller never sees it.",
    "systick_reload_off_by_one": "SYST_RVR is loaded with N instead of N-1: every tick is one cycle too long.",
    "wrong_clock_assumption": "The reload is computed for 150 MHz but the core still runs from a slower clock.",
}

# fault type -> (builder option, value)
_OPTION_FAULTS: Dict[str, Tuple[str, Any]] = {
    "missing_bss_zero": ("skip_bss_zero", True),
    "missing_data_copy": ("skip_data_copy", True),
    "no_mpu": ("mpu", False),
    "task_overflows_stack": ("task_set", "overflow"),
    "task_privileged_op": ("task_set", "privileged_op"),
    "irq_not_enabled": ("irq_not_enabled", True),
    "missing_irq_handler": ("missing_irq_handler", True),
    "svc_return_in_live_r0": ("svc_return_bug", True),
    "systick_reload_off_by_one": ("reload_off_by_one", True),
}
_BOOT_FAULTS = {"wrong_initial_sp", "bad_reset_vector", "misaligned_vector_table", "vector_thumb_bit_missing"}


def describe(fault: Dict[str, Any]) -> str:
    t = fault.get("type", "?")
    text = FAULT_CATALOG.get(t, "unknown fault type")
    extras = {k: v for k, v in fault.items() if k != "type"}
    return f"{t}: {text}" + (f" {extras}" if extras else "")


def apply_to_options(faults: List[Dict[str, Any]], options: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``options`` with the program-level faults applied."""
    out = dict(options)
    for fault in faults:
        t = fault["type"]
        if t not in FAULT_CATALOG:
            raise ValueError(f"unknown fault type '{t}'; known: {sorted(FAULT_CATALOG)}")
        if t in _OPTION_FAULTS:
            key, value = _OPTION_FAULTS[t]
            out[key] = value
        elif t == "pendsv_forgets_register":
            reg = fault.get("register", "r8")
            regs = [f"r{i}" for i in range(4, 12) if f"r{i}" != reg]
            out["pendsv_save"] = ", ".join(regs)
            out["pendsv_restore"] = ", ".join(regs)
        elif t == "irq_priority_inversion":
            out["button_priority"] = int(fault.get("button_priority", 0x80))
            out["timer_priority"] = int(fault.get("timer_priority", 0x80))
        elif t == "pendsv_priority_not_lowest":
            out["pendsv_priority"] = int(fault.get("priority", 0x80))
        elif t == "wrong_clock_assumption":
            out["assumed_hz"] = int(fault.get("assumed_hz", 150_000_000))
    return out


def apply_to_config(faults: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """Faults that change the machine itself (e.g. the real core clock)."""
    out = dict(config)
    for fault in faults:
        if fault["type"] == "wrong_clock_assumption":
            out["cpu_hz"] = int(fault.get("actual_hz", 12_000_000))
    return out


def boot_faults(faults: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [f for f in faults if f["type"] in _BOOT_FAULTS]


def machine_faults(faults: List[Dict[str, Any]], machine) -> None:
    """Faults that need a live machine (devices)."""
    for fault in faults:
        if fault["type"] == "timer_intr_not_cleared":
            machine.timer.level_triggered_bug = True
