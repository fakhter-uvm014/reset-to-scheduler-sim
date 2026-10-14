"""The program image, the "linker" and the reset sequence.

What a student writes in weeks 3-4 of the course is modelled here as data:

* a **vector table** at the start of flash: word 0 is the initial MSP,
  word 1 the address of ``Reset_Handler``, then one handler address per
  exception (HardFault, MemManage, ..., SVCall, PendSV, SysTick, IRQ0...).
  Every handler entry has **bit 0 set** (the Thumb bit): the core copies it
  into xPSR.T, and an entry with bit 0 clear makes the first handler
  instruction fault with UsageFault INVSTATE. The linker sets the bit
  automatically for functions marked as Thumb (``.thumb_func`` / C code);
  hand-written tables that use plain labels or constants lose it.
* On the RP2350 the boot ROM looks for a small **IMAGE_DEF metadata block**
  in the first part of the image before it will run it, then takes the
  initial SP and reset handler from the vector table. The model reserves
  room for that block between the vector table and ``.text`` but does not
  model the boot ROM itself: execution starts at the reset vector.
* ``.text`` sections (teaching-ISA programs) placed in flash or SRAM,
* ``.data`` (initialised globals): the *initial values* live in flash
  (``__data_load``) and the reset handler copies them to SRAM
  (``__data_start`` .. ``__data_end``),
* ``.bss`` (zero-initialised globals): nothing in flash; the reset handler
  zeroes ``__bss_start`` .. ``__bss_end`` in SRAM.

The reset handler itself is a real teaching-ISA program (see
:func:`reset_handler_source`) so the copy and zero loops show up in the
trace instruction by instruction. The **boot steps** a student should be
able to point at in the debugger are emitted as ``boot_step`` events by the
:class:`BootMonitor`:

    1. power_on         SRAM holds garbage (seeded pseudo-random words)
    2. vector_table     core reads word 0 -> MSP, word 1 -> PC
    3. reset_handler    first instruction executes
    4. data_copied      .data values in SRAM match the flash image
    5. bss_zeroed       every .bss word is zero
    6. main_entered     ``bl main`` happened

Injectable boot faults (``faults:`` in a scenario): ``missing_bss_zero``,
``missing_data_copy``, ``wrong_initial_sp``, ``bad_reset_vector``,
``misaligned_vector_table``, ``vector_thumb_bit_missing``.

Author: Farzana Akhter
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import memory as mm
from .isa import AssemblyError, Literal, Program, Symbol, assemble
from .nvic import (EXC_BUSFAULT, EXC_HARDFAULT, EXC_MEMMANAGE, EXC_PENDSV, EXC_RESET,
                   EXC_SVCALL, EXC_SYSTICK, EXC_USAGEFAULT, IRQ_BASE, IRQ_GPIO,
                   IRQ_TIMER0, NUM_IRQS)

STACK_TOP = 0x2008_2000           # end of SRAM on the RP2350: the usual MSP start
VECTOR_WORDS = IRQ_BASE + NUM_IRQS
TEXT_FLASH_START = mm.FLASH_BASE + 0x100   # after the 48-word vector table + room for the boot metadata block
KERNEL_ALIGN = 32                          # MPU granule: stacks and guards start on 32-byte boundaries
SRAM_DATA_START = mm.SRAM_BASE             # .data first, then .bss, then kernel objects


@dataclass
class Section:
    """A placed program: its source, requested region and final base address."""

    name: str
    program: Program
    region: str = "flash"   # "flash" | "sram"
    base: int = 0


@dataclass
class Image:
    """Everything the 'programmer' writes into flash before power-on."""

    sections: List[Section] = field(default_factory=list)
    data: Dict[str, int] = field(default_factory=dict)   # .data symbol -> initial value
    bss: List[str] = field(default_factory=list)         # .bss symbol names
    handlers: Dict[int, str] = field(default_factory=dict)  # exception number -> label
    symbols: Dict[str, int] = field(default_factory=dict)
    initial_sp: int = STACK_TOP
    vector_base: int = mm.FLASH_BASE
    data_load: int = 0
    data_start: int = 0
    data_end: int = 0
    bss_start: int = 0
    bss_end: int = 0
    kernel_reserved: int = 0   # first free SRAM word after .bss (kernel objects go here)
    globals_end: int = 0       # .data + .bss rounded up to the 32-byte MPU granule
    reserve: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # name -> (base, size)

    def add_section(self, name: str, source: str, region: str = "flash") -> Section:
        section = Section(name, assemble(source, name), region)
        self.sections.append(section)
        return section

    def add_data(self, name: str, value: int) -> None:
        self.data[name] = value & 0xFFFFFFFF

    def add_bss(self, name: str) -> None:
        self.bss.append(name)

    def set_handler(self, number: int, label: str) -> None:
        self.handlers[number] = label

    def reserve_sram(self, name: str, size: int) -> int:
        """Reserve ``size`` bytes of SRAM after .bss (kernel objects, task stacks...).

        Reservations are rounded to 32 bytes so each one can be an MPU region."""
        base = self.kernel_reserved
        self.reserve[name] = (base, size)
        self.kernel_reserved += (size + KERNEL_ALIGN - 1) & ~(KERNEL_ALIGN - 1)
        self.symbols[name] = base
        return base

    def section(self, name: str) -> Section:
        for s in self.sections:
            if s.name == name:
                return s
        raise KeyError(name)


def link(image: Image) -> Image:
    """Assign addresses: a very small linker script in Python.

    Layout::

        FLASH  0x10000000  vector table (48 words)
               0x10000100  .text sections requested in flash
               ...         .data load image (initial values)
        SRAM   0x20000000  .data
               ...         .bss
               ...         kernel reserved objects (TCBs, task stacks, guards)
               0x20082000  initial MSP (grows down)
    """
    flash_cursor = TEXT_FLASH_START
    sram_cursor = SRAM_DATA_START
    # .data and .bss first in SRAM so their addresses are stable and low.
    image.data_start = sram_cursor
    for name in image.data:
        image.symbols[name] = sram_cursor
        sram_cursor += 4
    image.data_end = sram_cursor
    image.bss_start = sram_cursor
    for name in image.bss:
        image.symbols[name] = sram_cursor
        sram_cursor += 4
    image.bss_end = sram_cursor
    # SRAM text (for the flash-vs-SRAM experiment) goes after .bss, before kernel objects.
    for section in image.sections:
        if section.region == "sram":
            section.base = sram_cursor
            sram_cursor += 4 * section.program.size_words
        else:
            section.base = flash_cursor
            flash_cursor += 4 * section.program.size_words
    image.data_load = flash_cursor
    flash_cursor += 4 * len(image.data)
    # Kernel objects start on an MPU granule boundary after everything else.
    sram_cursor = (sram_cursor + KERNEL_ALIGN - 1) & ~(KERNEL_ALIGN - 1)
    image.globals_end = sram_cursor
    # Kernel reservations were requested before link relative to 0: rebase them.
    rebased: Dict[str, Tuple[int, int]] = {}
    for name, (rel, size) in image.reserve.items():
        rebased[name] = (sram_cursor + rel, size)
        image.symbols[name] = sram_cursor + rel
    image.reserve = rebased
    image.kernel_reserved = sram_cursor + image.kernel_reserved
    # Labels of every section become global symbols.
    for section in image.sections:
        for label, index in section.program.labels.items():
            image.symbols[label] = section.base + 4 * index
    image.symbols.update({
        "__data_load": image.data_load, "__data_start": image.data_start,
        "__data_end": image.data_end, "__bss_start": image.bss_start,
        "__bss_end": image.bss_end, "__stack_top": image.initial_sp,
        "__vectors": image.vector_base,
    })
    for name, addr in (("SYST_CSR", mm.SYST_CSR), ("SYST_RVR", mm.SYST_RVR), ("SYST_CVR", mm.SYST_CVR),
                       ("SCB_ICSR", mm.SCB_ICSR), ("SCB_SHCSR", mm.SCB_SHCSR), ("NVIC_ISER", mm.NVIC_ISER),
                       ("NVIC_IPR0", mm.NVIC_IPR0), ("MPU_CTRL", mm.MPU_CTRL), ("GPIO_OUT", mm.GPIO_OUT),
                       ("GPIO_OUT_SET", mm.GPIO_OUT_SET), ("GPIO_OUT_CLR", mm.GPIO_OUT_CLR),
                       ("GPIO_INTR", mm.GPIO_INTR), ("UART_DR", mm.UART_DR), ("TIMER_ALARM0", mm.TIMER_ALARM0),
                       ("TIMER_INTR", mm.TIMER_INTR), ("TIMER_TIMERAWL", mm.TIMER_TIMERAWL),
                       ("DWT_CYCCNT", mm.DWT_CYCCNT), ("DWT_CTRL", mm.DWT_CTRL), ("DEMCR", mm.DEMCR),
                       ("SCB_SHPR3", mm.SCB_SHPR3), ("SCB_CFSR", mm.SCB_CFSR), ("SCB_HFSR", mm.SCB_HFSR),
                       ("NVIC_ISPR", mm.NVIC_ISPR), ("NVIC_ICER", mm.NVIC_ICER), ("NVIC_ICPR", mm.NVIC_ICPR)):
        image.symbols.setdefault(name, addr)
    _check_symbols(image)
    return image


def _check_symbols(image: Image) -> None:
    """Every symbol an instruction references must exist (the linker's
    'undefined reference' error), so a typo never becomes a run-time mystery."""
    for section in image.sections:
        for instr in section.program.instructions:
            for arg in instr.args:
                sym = arg.value if isinstance(arg, Literal) else arg
                if isinstance(sym, Symbol) and sym.name not in image.symbols:
                    raise AssemblyError(f"{section.name}:{instr.line}: undefined reference to '{sym.name}'")


def reset_handler_source(copy_data: bool = True, zero_bss: bool = True) -> str:
    """The startup code every student writes (teaching ISA).

    ``copy_data=False`` / ``zero_bss=False`` produce the two classic boot bugs.
    """
    data_loop = """
    ; ---- copy .data initial values from flash to SRAM ----
    ldr  r0, =__data_load       ; source (flash)
    ldr  r1, =__data_start      ; destination (SRAM)
    ldr  r2, =__data_end
copy_loop:
    cmp  r1, r2
    bge  copy_done
    ldr  r3, [r0]
    str  r3, [r1]
    add  r0, r0, #4
    add  r1, r1, #4
    b    copy_loop
copy_done:
    kcall "boot:data_copied"
""" if copy_data else """
    ; BUG: .data copy loop is missing -> globals keep whatever SRAM held at power-on
    kcall "boot:data_copied"
"""
    bss_loop = """
    ; ---- zero .bss ----
    mov  r0, #0
    ldr  r1, =__bss_start
    ldr  r2, =__bss_end
bss_loop:
    cmp  r1, r2
    bge  bss_done
    str  r0, [r1]
    add  r1, r1, #4
    b    bss_loop
bss_done:
    kcall "boot:bss_zeroed"
""" if zero_bss else """
    ; BUG: .bss zero loop is missing -> uninitialised globals contain garbage
    kcall "boot:bss_zeroed"
"""
    return f"""
Reset_Handler:
    kcall "boot:reset_handler"
{data_loop}
{bss_loop}
    kcall "boot:main_entered"
    bl   main
hang:
    b    hang                   ; main returned: spin forever (real startup code does this too)
"""


DEFAULT_HANDLER_SOURCE = """
Default_Handler:
    kcall "default_handler"
    b    Default_Handler        ; unexpected exception: spin so the debugger shows where we are
"""


def write_image(memory: mm.Memory, image: Image, faults: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Program the image into flash/SRAM storage (what the debug probe does)."""
    faults = faults or []
    fault_types = {f["type"]: f for f in faults}
    for section in image.sections:
        for i, instr in enumerate(section.program.instructions):
            instr.address = section.base + 4 * i
            memory.load_word(instr.address, instr)
    for i, (name, value) in enumerate(image.data.items()):
        memory.load_word(image.data_load + 4 * i, value)
    # vector table
    table: List[int] = [0] * VECTOR_WORDS
    initial_sp = image.initial_sp
    if "wrong_initial_sp" in fault_types:
        initial_sp = int(fault_types["wrong_initial_sp"].get("value", 0x0000_0000))
    table[0] = initial_sp
    default = image.symbols.get("Default_Handler", 0)
    for number in range(1, VECTOR_WORDS):
        label = image.handlers.get(number)
        # bit 0 = 1: the Thumb bit the linker adds to every Thumb function address
        table[number] = (image.symbols[label] if label else default) | 1
    if "bad_reset_vector" in fault_types:
        table[EXC_RESET] = int(fault_types["bad_reset_vector"].get("value", 0))
    if "vector_thumb_bit_missing" in fault_types:
        which = fault_types["vector_thumb_bit_missing"].get("exception", EXC_RESET)
        table[int(which)] &= ~1
    vector_base = image.vector_base
    if "misaligned_vector_table" in fault_types:
        vector_base = image.vector_base + int(fault_types["misaligned_vector_table"].get("offset", 4))
    for i, word in enumerate(table):
        memory.load_word(image.vector_base + 4 * i, word)
    return {"vector_base": vector_base, "initial_sp": initial_sp, "reset_vector": table[EXC_RESET],
            "table": table}


def fill_sram_with_garbage(memory: mm.Memory, rng: random.Random, start: int, end: int) -> None:
    """At power-on SRAM is not zero. The model fills the interesting part with
    seeded pseudo-random words so that 'forgot to zero .bss' is visible."""
    for address in range(start, end, 4):
        memory.load_word(address, rng.getrandbits(32))


class BootMonitor:
    """Emits ``boot_step`` events and verifies each step (the debugger view)."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self.steps: List[Dict[str, Any]] = []
        for name in ("boot:reset_handler", "boot:data_copied", "boot:bss_zeroed", "boot:main_entered"):
            machine.cpu.kcalls[name] = self._make(name.split(":", 1)[1])

    def _make(self, step: str):
        def handler(cpu) -> None:
            self.step(step)
        return handler

    def step(self, name: str, **details: Any) -> None:
        m = self.machine
        image = m.image
        ok = True
        reason = ""
        if name == "power_on":
            reason = ("power-on: SRAM contents are undefined (model: seeded pseudo-random words); "
                      "flash holds the image; nothing has run yet")
        elif name == "vector_table":
            sp, rv = details["initial_sp"], details["reset_vector"]
            reason = (f"core reads vector table at 0x{details['vector_base']:08X}: word0 -> MSP=0x{sp:08X}, "
                      f"word1 -> 0x{rv:08X} -> PC=0x{rv & ~1:08X}, xPSR.T={rv & 1}")
            problems = []
            region = m.memory.region_for((sp - 4) & 0xFFFFFFFF)
            if region is None or region.name != "SRAM" or sp % 8:
                problems.append("initial SP is not the top of a stack in SRAM (or not 8-byte aligned)")
            target = m.memory.peek(rv & ~1) if m.memory.region_for(rv & ~1) else None
            if not hasattr(target, "op"):
                problems.append("reset vector does not point at code")
            if not rv & 1:
                problems.append("reset vector has bit 0 clear (Thumb bit missing): the first instruction will "
                                "raise UsageFault INVSTATE")
            ok = not problems
            details["problems"] = problems
            if problems:
                reason += "  [" + "; ".join(problems) + "]"
        elif name == "reset_handler":
            reason = f"Reset_Handler entered at PC=0x{m.cpu.pc - 4:08X}, MSP=0x{m.cpu.msp:08X}, privileged thread mode"
        elif name == "data_copied":
            mism = []
            for i, (sym, value) in enumerate(image.data.items()):
                got = m.memory.peek(image.symbols[sym])
                if got != value:
                    mism.append(f"{sym}: SRAM=0x{got:08X} expected 0x{value:08X}")
            ok = not mism
            reason = (f".data copy done: {len(image.data)} word(s) copied from flash 0x{image.data_load:08X} "
                      f"to SRAM 0x{image.data_start:08X}" if ok else
                      f".data copy check FAILED: {'; '.join(mism)}")
            details["mismatches"] = mism
        elif name == "bss_zeroed":
            nonzero = []
            for sym in image.bss:
                got = m.memory.peek(image.symbols[sym])
                if got != 0:
                    nonzero.append(f"{sym}=0x{got:08X}")
            ok = not nonzero
            reason = (f".bss zeroed: {len(image.bss)} word(s) at 0x{image.bss_start:08X}..0x{image.bss_end:08X} are 0"
                      if ok else f".bss check FAILED, garbage left in: {', '.join(nonzero)}")
            details["nonzero"] = nonzero
        elif name == "main_entered":
            reason = f"bl main: C entry point reached after {m.cpu.cycles} cycles, MSP=0x{m.cpu.msp:08X}"
        record = {"step": name, "ok": ok, "t_cycles": m.cpu.cycles, **details}
        self.steps.append(record)
        m.trace.emit("boot", "boot_step", reason, frm=None, to=name, step=name, ok=ok, **details)
