"""The teaching-ISA programs the scenarios run.

Every program is a builder that fills an :class:`r2s.boot.Image`: startup
code, ``main``, interrupt handlers and (for the kernel scenarios) the task
bodies. They are short on purpose so a student can read the whole thing
next to the trace. Register use follows the AAPCS calling convention
(r0-r3 arguments/scratch, r4-r11 callee-saved, lr return address).

NVIC priorities are written the way real code must write them: NVIC_IPR is
byte-packed, so IRQn's priority is byte ``n % 4`` of the word at
``NVIC_IPR0 + 4 * (n // 4)`` (see :func:`ipr_word`).

Author: Farzana Akhter
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .boot import DEFAULT_HANDLER_SOURCE, Image, reset_handler_source
from .kernel import Kernel
from .nvic import (EXC_BUSFAULT, EXC_HARDFAULT, EXC_MEMMANAGE, EXC_RESET, EXC_SYSTICK,
                   EXC_USAGEFAULT, IRQ_BASE, IRQ_GPIO, IRQ_TIMER0)


def ipr_word(irq: int, priority: int) -> tuple:
    """(byte offset of the NVIC_IPR word, value) that sets IRQ ``irq`` to ``priority``."""
    return 4 * (irq // 4), (priority & 0xFF) << (8 * (irq % 4))
from .peripherals import BUTTON_PIN, LED_PIN, MARKER_PIN


def _startup(image: Image, copy_data: bool = True, zero_bss: bool = True) -> None:
    image.add_section("startup", reset_handler_source(copy_data, zero_bss))
    image.add_section("default_handler", DEFAULT_HANDLER_SOURCE)
    image.set_handler(EXC_RESET, "Reset_Handler")


FAULT_PRINTER_SOURCE = """
Fault_Printer:
    kcall "fault:print"         ; print CFSR/HFSR/MMFAR/BFAR and the stacked frame on the UART
    halt                        ; stop here so the student can look at the record
"""


# ---------------------------------------------------------------------------
# P01 / P02 / P03: boot + blink + UART
# ---------------------------------------------------------------------------
BLINK_MAIN = f"""
main:
    uart_puts "boot ok\\n"
    push {{r4, lr}}             ; C prologue: the first stack write after reset
    mov  r4, #0                 ; blink counter
blink_loop:
    gpio_set #{LED_PIN}
    bl   delay
    gpio_clr #{LED_PIN}
    bl   delay
    add  r4, r4, #1
    cmp  r4, #3
    bne  blink_loop
    ldr  r0, =counter           ; global in .bss: should start at 0
    ldr  r1, [r0]
    add  r1, r1, #1
    str  r1, [r0]
    mov  r0, r1
    uart_puts "counter="
    kcall "lib:print_r0"
    ldr  r0, =greeting_len      ; global in .data: initial value from flash
    ldr  r0, [r0]
    uart_puts "greeting_len="
    kcall "lib:print_r0"
    pop  {{r4, lr}}             ; C epilogue
    halt

delay:
    push {{r4, lr}}             ; a leaf function compiled at -O0 still saves lr
    mov  r0, #20
delay_loop:
    subs r0, r0, #1
    bne  delay_loop
    pop  {{r4, pc}}
"""


def build_blink(image: Image, options: Dict[str, Any]) -> None:
    """P01-P03: boot, print, blink three times, read one .bss and one .data global."""
    _startup(image, copy_data=not options.get("skip_data_copy", False),
             zero_bss=not options.get("skip_bss_zero", False))
    image.add_section("main", BLINK_MAIN)
    image.add_section("faults", FAULT_PRINTER_SOURCE)
    image.add_data("greeting_len", 7)
    image.add_bss("counter")
    for number in (EXC_HARDFAULT, EXC_BUSFAULT, EXC_MEMMANAGE, EXC_USAGEFAULT):
        image.set_handler(number, "Fault_Printer")


# ---------------------------------------------------------------------------
# P04: interrupt latency (button -> marker pin)
# ---------------------------------------------------------------------------
_GPIO_IPR_OFF, _GPIO_IPR_40 = ipr_word(IRQ_GPIO, 0x40)

LATENCY_MAIN = f"""
main:
    ldr  r0, =NVIC_IPR0
    mov  r1, #{_GPIO_IPR_40:#x}            ; priority 0x40 in byte {IRQ_GPIO % 4} (IRQ{IRQ_GPIO} = IO_IRQ_BANK0)
    str  r1, [r0, #{_GPIO_IPR_OFF}]         ; NVIC_IPR{IRQ_GPIO // 4}: IRQ{4 * (IRQ_GPIO // 4)}..IRQ{4 * (IRQ_GPIO // 4) + 3}
    ldr  r0, =NVIC_ISER
    mov  r1, #{1 << IRQ_GPIO:#x}
    str  r1, [r0]               ; NVIC_ISER0 bit {IRQ_GPIO}: enable IRQ_GPIO
    uart_puts "latency test\\n"
    mov  r4, #0
work_loop:
    ; ---- "light" load: plain work, interrupts always enabled ----
    add  r4, r4, #1
    and  r5, r4, #7
    cmp  r5, #0
    bne  work_loop
    cmp  r6, #0                 ; r6 = 1 selects the heavy variant (set by main_variant)
    beq  work_loop
    ; ---- "heavy" load: a critical section with interrupts masked ----
    cpsid i
    mov  r0, #CRIT_LEN
crit_loop:
    subs r0, r0, #1
    bne  crit_loop
    cpsie i
    b    work_loop

GPIO_Handler:
    gpio_set #{MARKER_PIN}      ; scope marker: "handler started"
    ldr  r0, =GPIO_INTR
    mov  r1, #{1 << BUTTON_PIN}
    str  r1, [r0]               ; acknowledge the edge (write 1 to clear)
    ldr  r0, =presses
    ldr  r1, [r0]
    add  r1, r1, #1
    str  r1, [r0]
    gpio_clr #{MARKER_PIN}
    bx   lr                     ; EXC_RETURN
"""


def build_latency(image: Image, options: Dict[str, Any]) -> None:
    """P04 / P18: button IRQ with a scope marker, optional critical sections."""
    heavy = bool(options.get("heavy", False))
    crit = int(options.get("critical_section_len", 60))
    source = LATENCY_MAIN.replace("#CRIT_LEN", f"#{crit}")
    # r6 selects the variant; set it in a tiny prologue
    source = source.replace("main:\n", f"main:\n    mov  r6, #{1 if heavy else 0}\n", 1)
    if options.get("irq_not_enabled"):
        # BUG (P18): the NVIC_ISER write is missing; the peripheral still flags the edge
        source = source.replace("    ldr  r0, =NVIC_ISER\n", "    ; BUG: NVIC_ISER never written\n", 1)
        source = source.replace("    str  r1, [r0]               ; NVIC_ISER0", "    nop                         ; (was: str to NVIC_ISER0)", 1)
    _startup(image)
    image.add_section("main", source)
    image.add_bss("presses")
    if not options.get("missing_irq_handler"):
        image.set_handler(IRQ_BASE + IRQ_GPIO, "GPIO_Handler")


# ---------------------------------------------------------------------------
# P05: two interrupts, priorities, tail-chaining
# ---------------------------------------------------------------------------
PRIORITY_MAIN = f"""
main:
    ldr  r0, =NVIC_IPR0
    mov  r1, #TIMER_PRIO
    str  r1, [r0, #{4 * (IRQ_TIMER0 // 4)}]   ; IRQ_TIMER0 (IRQ{IRQ_TIMER0}): byte {IRQ_TIMER0 % 4} of NVIC_IPR{IRQ_TIMER0 // 4}
    mov  r1, #BUTTON_PRIO
    str  r1, [r0, #{4 * (IRQ_GPIO // 4)}]     ; IRQ_GPIO (IRQ{IRQ_GPIO}): byte {IRQ_GPIO % 4} of NVIC_IPR{IRQ_GPIO // 4}
    ldr  r0, =NVIC_ISER
    mov  r1, #{(1 << IRQ_GPIO) | (1 << IRQ_TIMER0):#x}
    str  r1, [r0]
    ldr  r0, =TIMER_ALARM0
    mov  r1, #ALARM_US
    str  r1, [r0]               ; alarm at ALARM_US microseconds
    uart_puts "priority test\\n"
idle_loop:
    wfi
    b    idle_loop

Timer_Handler:                  ; a LONG handler (simulates slow processing)
    mark "timer handler start"
    ldr  r0, =TIMER_INTR
    mov  r1, #1
    str  r1, [r0]               ; clear the alarm interrupt
    mov  r2, #TIMER_WORK
timer_work:
    subs r2, r2, #1
    bne  timer_work
    mark "timer handler end"
    bx   lr

GPIO_Handler:                   ; a SHORT, urgent handler
    gpio_set #{MARKER_PIN}
    ldr  r0, =GPIO_INTR
    mov  r1, #{1 << BUTTON_PIN}
    str  r1, [r0]
    gpio_clr #{MARKER_PIN}
    bx   lr
"""


def build_priority(image: Image, options: Dict[str, Any]) -> None:
    timer_prio = (int(options.get("timer_priority", 0x80)) & 0xFF) << (8 * (IRQ_TIMER0 % 4))
    button_prio = (int(options.get("button_priority", 0x80)) & 0xFF) << (8 * (IRQ_GPIO % 4))
    source = (PRIORITY_MAIN
              .replace("#TIMER_PRIO", f"#{timer_prio:#x}")
              .replace("#BUTTON_PRIO", f"#{button_prio:#x}")
              .replace("#ALARM_US", f"#{int(options.get('alarm_us', 20))}")
              .replace("#TIMER_WORK", f"#{int(options.get('timer_work', 200))}"))
    _startup(image)
    image.add_section("main", source)
    image.set_handler(IRQ_BASE + IRQ_TIMER0, "Timer_Handler")
    image.set_handler(IRQ_BASE + IRQ_GPIO, "GPIO_Handler")


# ---------------------------------------------------------------------------
# Kernel scenarios (P06 - P09, P12)
# ---------------------------------------------------------------------------
KERNEL_MAIN = """
main:
    uart_puts "kernel start\\n"
BUTTON_SETUP
    kcall "kernel:init"         ; build task stacks + frames, MPU regions, SysTick
    svc  #0                     ; start the scheduler: never returns here
    b    main                   ; (unreachable)

task_exit:
    svc  #4                     ; a task that returns from its entry ends up here
    b    task_exit

idle_task:
    wfi                         ; sleep until the next tick or interrupt
    b    idle_task
"""

TASK_BLINK = f"""
taskA:
    mov  r8, #0x1000            ; a value in a callee-saved register (P08 watches this)
    mov  r4, #0
taskA_loop:
    gpio_set #{LED_PIN}
    mov  r0, #WORK
    bl   spin
    gpio_clr #{LED_PIN}
    mov  r0, #WORK
    bl   spin
    add  r4, r4, #1
    add  r8, r8, #1             ; r8 = 0x1000 + iterations
    ldr  r0, =a_iters
    str  r4, [r0]
    ldr  r0, =a_r8_expected
    str  r8, [r0]
    cmp  r4, #0                 ; never exits
    b    taskA_loop

spin:
spin_loop:
    subs r0, r0, #1
    bne  spin_loop
    bx   lr
"""

_GPIO_IPR_C0 = ipr_word(IRQ_GPIO, 0xC0)

KERNEL_BUTTON_SETUP = f"""
    ldr  r0, =NVIC_IPR0
    mov  r1, #{_GPIO_IPR_C0[1]:#x}           ; IRQ_GPIO priority 0xC0: less urgent than SysTick (0x80)
    str  r1, [r0, #{_GPIO_IPR_C0[0]}]
    ldr  r0, =NVIC_ISER
    mov  r1, #{1 << IRQ_GPIO:#x}
    str  r1, [r0]               ; enable the button interrupt
"""

KERNEL_BUTTON_ISR = f"""
GPIO_Handler:                   ; a LONG handler: SysTick ticks while it runs
    push {{r4, r5, lr}}
    gpio_set #{MARKER_PIN}
    ldr  r0, =GPIO_INTR
    mov  r1, #{1 << BUTTON_PIN}
    str  r1, [r0]               ; acknowledge the edge
    mov  r4, #0                 ; r4 counts iterations...
    mov  r5, #0                 ; ...and r5 shadows it: they must stay equal
isr_loop:
    add  r4, r4, #1
    add  r5, r5, #1
    cmp  r4, #ISR_WORK
    blt  isr_loop
    cmp  r4, r5
    beq  isr_ok
    mark "GPIO handler: r4/r5 changed under the handler (a context switch ran inside it)"
    ldr  r0, =isr_corruptions
    ldr  r1, [r0]
    add  r1, r1, #1
    str  r1, [r0]
isr_ok:
    gpio_clr #{MARKER_PIN}
    pop  {{r4, r5, pc}}          ; pc <- EXC_RETURN: exception return
"""

TASK_UART = """
taskB:
    mov  r8, #0x2000            ; different r8 so a lost r8 shows up in task A
    mov  r5, #65                ; 'A'
taskB_loop:
    mov  r0, r5
    svc  #3                     ; write(r0) system call: returns 1 in r0
    cmp  r0, #1                 ; check the return value like real code would
    beq  taskB_sent
    mark "taskB: write() returned a wrong value"
    ldr  r1, =svc_errors
    ldr  r2, [r1]
    add  r2, r2, #1
    str  r2, [r1]
taskB_sent:
    add  r5, r5, #1
    cmp  r5, #91                ; past 'Z'?
    bne  taskB_next
    mov  r5, #65
    mov  r0, #10
    svc  #3                     ; newline
taskB_next:
    add  r8, r8, #1             ; B keeps changing r8: if PendSV loses r8, A sees B's value
    mov  r0, #WORK
    bl   spin
    b    taskB_loop
"""

TASK_COUNT = """
taskC:
    mov  r8, #0x3000
    mov  r6, #0
taskC_loop:
    add  r6, r6, #1
    ldr  r0, =c_count
    str  r6, [r0]
    svc  #1                     ; yield: let others run early
    b    taskC_loop
"""

TASK_R8_CHECK = """
taskA:
    mov  r8, #0x1000
    mov  r4, #0
taskA_loop:
    ldr  r0, =a_r8_expected
    str  r8, [r0]               ; remember what r8 should be
    mov  r0, #WORK
    bl   spin                   ; a tick (and a switch) usually lands here
    ldr  r0, =a_r8_expected
    ldr  r1, [r0]
    cmp  r8, r1                 ; did r8 survive the context switch?
    beq  taskA_ok
    mark "taskA: r8 corrupted after a context switch"
    ldr  r0, =corruptions
    ldr  r1, [r0]
    add  r1, r1, #1
    str  r1, [r0]
    mov  r8, #0x1000            ; resynchronise and keep going
    mov  r4, #0
taskA_ok:
    add  r4, r4, #1
    add  r8, r8, #1
    b    taskA_loop

spin:
spin_loop:
    subs r0, r0, #1
    bne  spin_loop
    bx   lr
"""

TASK_OVERFLOW = """
taskC:
    mov  r8, #0x3000
    mov  r6, #0
    mov  r4, #0xDEAD
    mov  r5, #0xBEEF
taskC_loop:
    add  r6, r6, #1
    ldr  r0, =c_count
    str  r6, [r0]
    cmp  r6, #START_AT          ; behave for a while first
    blt  taskC_yield
    mark "taskC: deep recursion begins (stack grows past its limit)"
taskC_recurse:
    push {r4, r5, r6, lr}       ; each 'call level' costs 16 bytes and never returns
    add  r6, r6, #1
    b    taskC_recurse
taskC_yield:
    svc  #1
    b    taskC_loop
"""

TASK_PRIV = f"""
taskC:
    mov  r8, #0x3000
    mov  r6, #0
taskC_loop:
    add  r6, r6, #1
    ldr  r0, =c_count
    str  r6, [r0]
    mov  r0, #67                ; 'C'
    svc  #3                     ; write via system call: allowed
    cmp  r6, #3
    bne  taskC_yield
    mark "taskC: trying a privileged operation directly"
    cpsid i                     ; forbidden in unprivileged thread mode
    ldr  r0, =SYST_CSR
    mov  r1, #0
    str  r1, [r0]               ; also forbidden: SysTick register from a task
taskC_yield:
    svc  #1
    b    taskC_loop
"""


def build_kernel(image: Image, options: Dict[str, Any]) -> Kernel:
    """P06-P09, P12: three tasks + idle on the tiny kernel."""
    work = int(options.get("task_work", 150))
    variant = options.get("task_set", "normal")
    task_a = TASK_R8_CHECK if variant == "r8_check" else TASK_BLINK
    task_c = {"overflow": TASK_OVERFLOW, "privileged_op": TASK_PRIV}.get(variant, TASK_COUNT)
    task_c = task_c.replace("#START_AT", f"#{int(options.get('misbehave_after', 4))}")
    button = bool(options.get("button_irq", False))
    main = KERNEL_MAIN.replace("BUTTON_SETUP\n", KERNEL_BUTTON_SETUP if button else "")
    source = (main + task_a + TASK_UART + task_c).replace("#WORK", f"#{work}")
    if button:
        source += KERNEL_BUTTON_ISR.replace("#ISR_WORK", f"#{int(options.get('isr_work', 3000))}")
    _startup(image)
    image.add_section("main", source)
    for name in ("a_iters", "a_r8_expected", "c_count", "corruptions", "svc_errors", "isr_corruptions"):
        image.add_bss(name)
    if button:
        image.set_handler(IRQ_BASE + IRQ_GPIO, "GPIO_Handler")
    tasks = options.get("tasks") or [
        {"name": "A", "entry": "taskA", "priority": 1},
        {"name": "B", "entry": "taskB", "priority": 1},
        {"name": "C", "entry": "taskC", "priority": 1},
    ]
    kernel = Kernel(image, tasks,
                    tick_cycles=int(options.get("tick_cycles", 2000)),
                    scheduler=options.get("scheduler", "round_robin"),
                    use_mpu=bool(options.get("mpu", True)),
                    unprivileged=bool(options.get("unprivileged", True)),
                    pendsv_save=options.get("pendsv_save", "r4-r11"),
                    pendsv_restore=options.get("pendsv_restore"),
                    stack_words=int(options.get("stack_words", 64)),
                    stop_after_switches=int(options.get("stop_after_switches", 0)),
                    stop_after_ticks=int(options.get("stop_after_ticks", 0)),
                    pendsv_priority=int(options.get("pendsv_priority", 0xFF)),
                    systick_priority=int(options.get("systick_priority", 0x80)),
                    svc_return_bug=bool(options.get("svc_return_bug", False)))
    return kernel


# ---------------------------------------------------------------------------
# P10 / P11: benchmark loop (flash vs SRAM, ISA profiles)
# ---------------------------------------------------------------------------
BENCH_MAIN = """
main:
    ldr  r0, =DWT_CYCCNT
    ldr  r4, [r0]               ; two back-to-back reads: what does measuring cost?
    ldr  r5, [r0]
    sub  r6, r5, r4             ; r6 = measurement overhead (subtract it from results)
    ldr  r1, =measure_overhead
    str  r6, [r1]
    ldr  r4, [r0]               ; t0
    bl   benchmark
    ldr  r1, =DWT_CYCCNT
    ldr  r5, [r1]               ; t1
    sub  r0, r5, r4             ; raw cycles (includes one counter read)
    ldr  r1, =bench_cycles
    str  r0, [r1]
    uart_puts "bench cycles="
    kcall "lib:print_r0"
    mov  r0, r6
    uart_puts "overhead="
    kcall "lib:print_r0"
    halt
"""

BENCH_LOOP = """
benchmark:                      ; checksum over N words of a table in SRAM
    push {r4-r7, lr}
    ldr  r4, =table
    mov  r5, #N_ITERS
    mov  r6, #0                 ; accumulator
    mov  r7, #0                 ; index
bench_loop:
    ldr  r0, [r4]
    add  r6, r6, r0
    eor  r6, r6, r7
    lsl  r1, r6, #1
    add  r6, r1, r6
    str  r6, [r4]
    add  r7, r7, #1
    cmp  r7, r5
    bne  bench_loop
    mov  r0, r6
    pop  {r4-r7, pc}
"""


def build_bench(image: Image, options: Dict[str, Any]) -> None:
    region = options.get("code_region", "flash")
    iters = int(options.get("iterations", 200))
    _startup(image)
    image.add_section("main", BENCH_MAIN)
    image.add_section("benchmark", BENCH_LOOP.replace("#N_ITERS", f"#{iters}"), region=region)
    image.add_data("table", 0x1234)
    image.add_bss("bench_cycles")
    image.add_bss("measure_overhead")


# ---------------------------------------------------------------------------
# P16: SysTick period, measured with CYCCNT and with the 1 MHz timer
# ---------------------------------------------------------------------------
TICK_MAIN = """
main:
    ldr  r0, =reload_value      ; RVR value computed "at compile time" from the ASSUMED clock
    ldr  r1, [r0]
    ldr  r0, =SYST_RVR
    str  r1, [r0]               ; SYST_RVR = reload (period = RVR + 1 cycles)
    mov  r1, #0
    ldr  r0, =SYST_CVR
    str  r1, [r0]               ; any write clears the current value
    ldr  r0, =SYST_CSR
    mov  r1, #7                 ; ENABLE | TICKINT | CLKSOURCE (processor clock)
    str  r1, [r0]
    uart_puts "tick test\\n"
wait_loop:
    wfi                         ; sleep until the next tick
    ldr  r0, =ticks
    ldr  r1, [r0]
    cmp  r1, #12
    blt  wait_loop
    ldr  r0, =t_start_cycles     ; 10 periods: from tick 2 to tick 12 (tick 1 runs
                                ; its handler from a cold XIP cache: +20 cycles)
    ldr  r1, [r0]
    ldr  r0, =t_end_cycles
    ldr  r2, [r0]
    sub  r0, r2, r1
    uart_puts "10 ticks: cycles="
    kcall "lib:print_r0"
    ldr  r0, =t_start_us
    ldr  r1, [r0]
    ldr  r0, =t_end_us
    ldr  r2, [r0]
    sub  r0, r2, r1
    uart_puts "10 ticks: us="
    kcall "lib:print_r0"
    halt

SysTick_Handler:
    ldr  r0, =ticks
    ldr  r1, [r0]
    add  r1, r1, #1
    str  r1, [r0]
    ldr  r2, =DWT_CYCCNT
    ldr  r2, [r2]               ; timestamp in core cycles
    ldr  r3, =TIMER_TIMERAWL
    ldr  r3, [r3]               ; timestamp in microseconds (1 MHz timer, independent of the core clock)
    cmp  r1, #2
    bne  not_start
    ldr  r0, =t_start_cycles
    str  r2, [r0]
    ldr  r0, =t_start_us
    str  r3, [r0]
not_start:
    cmp  r1, #12
    bne  tick_done
    ldr  r0, =t_end_cycles
    str  r2, [r0]
    ldr  r0, =t_end_us
    str  r3, [r0]
tick_done:
    bx   lr
"""


def build_tick(image: Image, options: Dict[str, Any]) -> None:
    """P16: program SysTick for a period in microseconds from an assumed clock."""
    tick_us = int(options.get("tick_us", 1000))
    assumed_hz = int(options.get("assumed_hz", 150_000_000))
    cycles = assumed_hz * tick_us // 1_000_000
    reload = cycles if options.get("reload_off_by_one") else cycles - 1
    _startup(image)
    image.add_section("main", TICK_MAIN)
    image.add_section("faults", FAULT_PRINTER_SOURCE)
    image.add_data("reload_value", reload)
    for name in ("ticks", "t_start_cycles", "t_end_cycles", "t_start_us", "t_end_us"):
        image.add_bss(name)
    image.set_handler(EXC_SYSTICK, "SysTick_Handler")
    for number in (EXC_HARDFAULT, EXC_BUSFAULT, EXC_MEMMANAGE, EXC_USAGEFAULT):
        image.set_handler(number, "Fault_Printer")


BUILDERS = {
    "blink": build_blink,
    "latency": build_latency,
    "priority": build_priority,
    "kernel": build_kernel,
    "bench": build_bench,
    "tick": build_tick,
}
