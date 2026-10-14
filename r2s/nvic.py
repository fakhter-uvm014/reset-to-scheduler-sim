"""Exception model: numbers, priorities, pending/active state, tail-chaining.

Cortex-M rules this module models
---------------------------------
* Every exception has a number. 1-15 are system exceptions (Reset=1, NMI=2,
  HardFault=3, MemManage=4, BusFault=5, UsageFault=6, SecureFault=7 on
  ARMv8-M with the Security Extension (not modelled), SVCall=11,
  DebugMonitor=12, PendSV=14, SysTick=15); external interrupts are 16 + IRQn,
  and that number is what IPSR shows while the handler runs.
* Every exception has a priority; **lower number = more urgent**. Reset,
  NMI and HardFault have fixed negative priorities. The rest are
  configurable: SHPR1-3 for system exceptions, NVIC_IPR for IRQs. Both are
  **byte-packed**: the priority of IRQn is byte ``n % 4`` of word
  ``NVIC_IPR0 + 4 * (n // 4)``; PendSV is byte 2 and SysTick byte 3 of SHPR3.
  Writing a whole word therefore sets four priorities at once, a classic bug.
* Only the top ``PRIORITY_BITS`` bits of each priority byte are implemented
  (the model uses 4 bits, i.e. 16 levels in steps of 0x10; read back
  NVIC_IPR on your part to see how many bits it really has). Writing 0x41
  reads back as 0x40, and 0xFF reads back as 0xF0.
* The core runs at an *execution priority*: that of the most urgent active
  handler, or "thread" (lowest possible) when no handler is active. Setting
  PRIMASK (``cpsid i``) raises the execution priority to 0, which blocks
  every configurable exception but not HardFault/NMI.
* A pending exception is taken when its priority is numerically lower than
  the current execution priority (pre-emption).
* **Tail-chaining**: when a handler returns and another exception is
  pending that would be taken anyway, the core skips the unstack/restack
  and jumps straight to the next handler (cheaper than a full exit+entry).
* **Late arrival** is not modelled.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import memory as mm

EXC_RESET = 1
EXC_NMI = 2
EXC_HARDFAULT = 3
EXC_MEMMANAGE = 4
EXC_BUSFAULT = 5
EXC_USAGEFAULT = 6
EXC_SECUREFAULT = 7
EXC_SVCALL = 11
EXC_DEBUGMON = 12
EXC_PENDSV = 14
EXC_SYSTICK = 15
IRQ_BASE = 16

# Named IRQ lines used by the models. The numbers are the RP2350 ones
# (TIMER0_IRQ_0 = 0, IO_IRQ_BANK0 = 21); the model implements the first 32
# of the chip's IRQ lines, which is all the scenarios need.
IRQ_TIMER0 = 0
IRQ_GPIO = 21
NUM_IRQS = 32
PRIORITY_BITS = 4
PRIORITY_MASK = (0xFF << (8 - PRIORITY_BITS)) & 0xFF

EXC_NAMES = {
    EXC_RESET: "Reset", EXC_NMI: "NMI", EXC_HARDFAULT: "HardFault",
    EXC_MEMMANAGE: "MemManage", EXC_BUSFAULT: "BusFault", EXC_USAGEFAULT: "UsageFault",
    EXC_SECUREFAULT: "SecureFault", EXC_SVCALL: "SVCall", EXC_DEBUGMON: "DebugMonitor", EXC_PENDSV: "PendSV", EXC_SYSTICK: "SysTick",
    IRQ_BASE + IRQ_TIMER0: "IRQ_TIMER0", IRQ_BASE + IRQ_GPIO: "IRQ_GPIO",
}
# SHPR byte position of each configurable system exception: (register, byte)
_SHPR_SLOTS = {EXC_MEMMANAGE: (mm.SCB_SHPR1, 0), EXC_BUSFAULT: (mm.SCB_SHPR1, 1),
               EXC_USAGEFAULT: (mm.SCB_SHPR1, 2), EXC_SVCALL: (mm.SCB_SHPR2, 3),
               EXC_PENDSV: (mm.SCB_SHPR3, 2), EXC_SYSTICK: (mm.SCB_SHPR3, 3)}
THREAD_PRIORITY = 256  # "lower than anything": no handler active
FIXED_PRIORITY = {EXC_RESET: -3, EXC_NMI: -2, EXC_HARDFAULT: -1}


def exc_name(number: int) -> str:
    if number in EXC_NAMES:
        return EXC_NAMES[number]
    if number >= IRQ_BASE:
        return f"IRQ{number - IRQ_BASE}"
    return f"Exc{number}"


@dataclass
class PendingInfo:
    pend_cycles: int
    reason: str


@dataclass
class Nvic:
    """Interrupt controller + system exception state (SHPR/SHCSR live here too)."""

    trace: object = None
    enabled: Dict[int, bool] = field(default_factory=dict)       # IRQn -> enabled
    priority: Dict[int, int] = field(default_factory=dict)       # exception number -> priority
    pending: Dict[int, PendingInfo] = field(default_factory=dict)
    active: List[int] = field(default_factory=list)              # stack of active exception numbers
    primask: bool = False
    # SHCSR bits: which fault handlers are enabled (otherwise -> HardFault)
    memmanage_enabled: bool = False
    busfault_enabled: bool = False
    usagefault_enabled: bool = False
    clock = None

    # ---------------------------------------------------------- configuration
    def set_priority(self, number: int, prio: int) -> None:
        """Set a configurable priority; unimplemented low bits read as zero."""
        if number in FIXED_PRIORITY:
            return
        self.priority[number] = prio & PRIORITY_MASK

    def get_priority(self, number: int) -> int:
        if number in FIXED_PRIORITY:
            return FIXED_PRIORITY[number]
        return self.priority.get(number, 0)

    def enable_irq(self, irq: int, on: bool = True) -> None:
        self.enabled[irq] = on

    def is_enabled(self, number: int) -> bool:
        if number < IRQ_BASE:
            return True  # system exceptions are always "enabled" (faults may escalate)
        return self.enabled.get(number - IRQ_BASE, False)

    # --------------------------------------------------------------- pending
    def set_pending(self, number: int, reason: str = "", unit: str = "nvic") -> None:
        if number in self.pending:
            return  # already pending: a second pend is merged (one entry only)
        cycles = self.clock() if self.clock else 0
        self.pending[number] = PendingInfo(cycles, reason)
        if self.trace is not None:
            self.trace.emit(unit, "irq_pend", reason or f"{exc_name(number)} pended",
                            to=exc_name(number), exception=number,
                            priority=self.get_priority(number))

    def clear_pending(self, number: int) -> None:
        self.pending.pop(number, None)

    def is_pending(self, number: int) -> bool:
        return number in self.pending

    # ----------------------------------------------------------- arbitration
    def execution_priority(self) -> int:
        prio = THREAD_PRIORITY
        if self.active:
            prio = min(self.get_priority(n) for n in self.active)
        if self.primask:
            prio = min(prio, 0)
        return prio

    def next_exception(self, exclude_active_level: Optional[int] = None) -> Optional[int]:
        """Return the most urgent pending exception that may pre-empt now, or None.

        Ties between equal priorities go to the lower exception number
        (hardware rule). ``exclude_active_level`` lets exception return
        compute the priority *after* the returning handler is removed.
        """
        current = self.execution_priority() if exclude_active_level is None else exclude_active_level
        best: Optional[int] = None
        best_prio = current
        for number in sorted(self.pending):
            if not self.is_enabled(number):
                continue
            prio = self.get_priority(number)
            if prio < best_prio:
                best, best_prio = number, prio
        return best

    def priority_after_return(self) -> int:
        remaining = self.active[:-1]
        prio = THREAD_PRIORITY
        if remaining:
            prio = min(self.get_priority(n) for n in remaining)
        if self.primask:
            prio = min(prio, 0)
        return prio

    # ----------------------------------------------- memory-mapped registers
    def attach(self, memory: mm.Memory) -> None:
        memory.attach_device(mm.NVIC_ISER, 0x400, self._read, self._write)
        memory.attach_device(mm.SCB_SHPR1, 0x10, self._read, self._write)  # SHPR1-3 + SHCSR

    def _read(self, address: int) -> int:
        if address in (mm.SCB_SHPR1, mm.SCB_SHPR2, mm.SCB_SHPR3):
            word = 0
            for number, (reg, byte) in _SHPR_SLOTS.items():
                if reg == address:
                    word |= self.get_priority(number) << (8 * byte)
            return word
        if address == mm.NVIC_ISER or address == mm.NVIC_ICER:
            return sum(1 << i for i, on in self.enabled.items() if on)
        if address == mm.NVIC_ISPR or address == mm.NVIC_ICPR:
            return sum(1 << (n - IRQ_BASE) for n in self.pending if n >= IRQ_BASE)
        if mm.NVIC_IPR0 <= address < mm.NVIC_IPR0 + NUM_IRQS:
            first = address - mm.NVIC_IPR0          # IRQ number of byte 0 of this word
            return sum(self.get_priority(IRQ_BASE + first + b) << (8 * b) for b in range(4))
        if address == mm.SCB_SHCSR:
            return ((1 << 16) if self.memmanage_enabled else 0) | \
                   ((1 << 17) if self.busfault_enabled else 0) | \
                   ((1 << 18) if self.usagefault_enabled else 0)
        return 0

    def _write(self, address: int, value: int) -> None:
        if address == mm.NVIC_ISER:
            for i in range(NUM_IRQS):
                if value & (1 << i):
                    self.enabled[i] = True
        elif address == mm.NVIC_ICER:
            for i in range(NUM_IRQS):
                if value & (1 << i):
                    self.enabled[i] = False
        elif address == mm.NVIC_ISPR:
            for i in range(NUM_IRQS):
                if value & (1 << i):
                    self.set_pending(IRQ_BASE + i, f"software pend of IRQ{i} via NVIC_ISPR")
        elif address == mm.NVIC_ICPR:
            for i in range(NUM_IRQS):
                if value & (1 << i):
                    self.clear_pending(IRQ_BASE + i)
        elif mm.NVIC_IPR0 <= address < mm.NVIC_IPR0 + NUM_IRQS:
            first = address - mm.NVIC_IPR0          # a word write sets FOUR priorities
            for b in range(4):
                self.set_priority(IRQ_BASE + first + b, (value >> (8 * b)) & 0xFF)
        elif address in (mm.SCB_SHPR1, mm.SCB_SHPR2, mm.SCB_SHPR3):
            for number, (reg, byte) in _SHPR_SLOTS.items():
                if reg == address:
                    self.set_priority(number, (value >> (8 * byte)) & 0xFF)
        elif address == mm.SCB_SHCSR:
            self.memmanage_enabled = bool(value & (1 << 16))
            self.busfault_enabled = bool(value & (1 << 17))
            self.usagefault_enabled = bool(value & (1 << 18))
