# Debug guide

Symptom first: find what you observe on the board, see what the simulator
shows for the same bug, the usual causes, and how to confirm it in GDB on
real hardware. Scenario IDs point at a run you can replay
(`r2s run <ID> --trace`, `r2s fault <ID>`, `r2s stack <ID>`, or the viewer).

GDB commands assume OpenOCD + GDB on a Pico 2 (see
[real-hardware.md](real-hardware.md)). Register addresses: CFSR
`0xE000ED28`, HFSR `0xE000ED2C`, MMFAR `0xE000ED34`, BFAR `0xE000ED38`,
SHCSR `0xE000ED24`, SHPR3 `0xE000ED20`, NVIC_ISER0 `0xE000E100`,
NVIC_ISPR0 `0xE000E200`, NVIC_IPR0 `0xE000E400`, SysTick CSR/RVR/CVR
`0xE000E010/14/18`, DWT_CYCCNT `0xE0001004`.

## Symptom table

| Symptom | What the simulator shows | Likely causes | How to confirm on the board |
|---|---|---|---|
| **Never reaches main** | `main_entered` boot step missing; a `fault` or `lockup` event before it, or the run ends at `max_cycles` inside the reset handler (P03, P14) | bad vector table (word 0 or 1), reset vector without the Thumb bit, an endless copy/zero loop (end symbol below start), boot metadata block missing so the boot ROM never starts the image | `monitor reset halt` (halts in the boot ROM), `hbreak Reset_Handler`, `continue`; then `x/2xw 0x10000000`, `p/x $sp` (= word 0), `p/x $pc` (= word 1 & ~1): the boot ROM loads MSP/PC from the table; breakpoint never hit = image not launched; `stepi` through `Reset_Handler`; `p &__bss_end - &__bss_start` |
| **HardFault right at reset** | `boot_step vector_table` flagged; `fault` UsageFault INVSTATE escalated to HardFault; stacked PC = `Reset_Handler`, stacked xPSR.T = 0 (P14) | vector entry with bit 0 clear (hand-written table without `.thumb_func`), table at the wrong address (VTOR / boot ROM looking elsewhere) | `x/xw 0xE000ED28` = `0x00020000` (INVSTATE), `x/xw 0xE000ED2C` = `0x40000000` (FORCED); `x/4xw 0x10000000`: every handler entry must be odd |
| **Initialised globals wrong** | `boot_step data_copied` FAIL with the mismatching symbol; no fault (P13) | no `.data` copy loop; copying from the run address instead of the load address; linker script without `AT> FLASH` | `x/4wx &__data_load` vs `x/4wx &__data_start` before `main`; `arm-none-eabi-objdump -h`: `.data` VMA (0x2000...) must differ from LMA (0x100...) |
| **`.bss` not zero** (uninitialised globals hold random values that change per power cycle) | `boot_step bss_zeroed` FAIL naming the variable (P02) | no `.bss` loop; wrong `__bss_end`; sections such as `COMMON` or `.bss.*` outside the zeroed range | break at `main`, `x/wx &counter`; `arm-none-eabi-nm -n app.elf` and check the symbol lies between `__bss_start` and `__bss_end` |
| **Stuck in Default_Handler** | `exception_entry` into the catch-all loop and a `kernel` event "Default_Handler reached for IRQ_GPIO (IPSR=37)"; run stops (P18 no_handler) | handler name does not match the startup file's weak symbol (typo, C++ name mangling), handler never linked | halt, `p/x $xpsr`: low 9 bits = IPSR; IRQ n = IPSR - 16; `info symbol $pc`; `x/xw 0x10000000 + 4*IPSR` |
| **Interrupt never fires** | `irq_pend` (the NVIC latched it) but no `exception_entry` (P18 not_enabled); or no `irq_pend` at all | NVIC enable bit not set (`NVIC_ISER`), peripheral interrupt enable not set, PRIMASK left at 1, priority not more urgent than the running handler | `x/xw 0xE000E100` (enabled?) and `x/xw 0xE000E200` (pending?); `p $primask`; read the peripheral's interrupt enable/status registers |
| **Interrupt fires once** | one `exception_entry`, later edges produce nothing | peripheral status flag cleared but the source not re-armed (one-shot alarm not reloaded), edge detection disabled, interrupt disabled inside the handler and never re-enabled | break in the handler, then check after it the peripheral enable/arm registers; `x/xw 0xE000E100` |
| **Interrupt fires forever / main starves** | the same IRQ re-pends immediately after every return ("INTR still set"); main never advances | handler does not clear the peripheral's status flag (level-sensitive line stays asserted), clears the wrong bit, or clears it before reading data | watch the status register before and after the clearing store; count handler entries in a global |
| **Latency higher than expected** | wide histogram (P04 heavy); `critical_section` events "IRQ_GPIO was pending for N cycles"; or the IRQ waits for a same-priority handler and is tail-chained (P05 inversion) | long `cpsid i` regions, equal or inverted priorities, slow handler prologue before the marker, flash cache miss on the handler | GPIO marker as the first handler instruction + scope; CYCCNT min/max; read back `NVIC_IPR` bytes (`x/4xb 0xE000E400 + 4*(n/4)`) |
| **Task never runs** | it stays `READY` with `switches_in=0`; no `context_switch` to it | scheduler never picks it (priority/round-robin bug), its initial frame is wrong (xPSR without T bit → INVSTATE, wrong PC), SysTick not enabled or PendSV never pended | `x/16xw <tcb.psp>`: word 14 = entry, word 15 = `0x01000000`; `x/xw 0xE000E010` (SysTick enabled, TICKINT); `x/xw 0xE000ED04` (PENDSVSET) |
| **Tasks corrupt each other's registers** | no fault; a task's check fails (marker "r8 corrupted", `corruptions` > 0); software frame of 7 words in the stack panel (P08); or a switch with EXC_RETURN `0xFFFFFFF1` (P15) | register missing from the PendSV list; save/restore lists differ; PendSV not the lowest priority; FPU registers not saved when tasks use the FPU | break in `PendSV_Handler`, `x/16xw $psp` after the `stmdb`; `p/x $lr` must be `0xFFFFFFFD`; `x/xw 0xE000ED20` byte 2 must be the largest priority |
| **Stack overflow** | watermark climbs to 100 %; with MPU: MemManage in the guard (P07); without: silent corruption, later INVPC/BusFault in *other* tasks (P09) | recursion, large local arrays, `printf`, stack size in words vs bytes | fill stacks with `0xA5A5A5A5` and find the deepest overwritten word; `x/16xw <stack_base - 32>`; enable an MPU guard or PSPLIM |
| **MemManage fault** | `fault` MemManage DACCVIOL with MMFAR (data) or IACCVIOL (fetch, no MMFAR); often MSTKERR right after (P07) | access outside the task's MPU regions, stack overflow into a guard, executing from an XN region, MPU regions overlapping (fault on ARMv8-M) | `x/xw 0xE000ED28` (MMFSR = low byte), `x/xw 0xE000ED34` (MMFAR, if MMARVALID); compare with the MPU region table; `p/x $lr` to pick the stack (bit 2) |
| **HardFault escalation** | `fault` with `escalated_from` set, HFSR.FORCED, the real cause in CFSR (P03, P14) | the specific fault handler is disabled (SHCSR reset state), or the fault happened with PRIMASK set or inside a more urgent handler; an `svc` with interrupts disabled | `x/xw 0xE000ED2C` (FORCED = bit 30), then `x/xw 0xE000ED28`; `x/xw 0xE000ED24` (SHCSR bits 16-18 enable MemManage/BusFault/UsageFault) |
| **Lockup** | `lockup` event; run stops (P03) | fault inside the HardFault handler, stacking error while entering HardFault (bad SP), bad HardFault vector | debugger shows `PC=0xEFFFFFFE` (ARMv8-M) and `S_LOCKUP`; look at SP and the vector table, not at the PC |
| **SVC returns wrong value** | `syscall` event says the result went to "the LIVE r0 (lost...)"; the `exception_return` pops r0 = the argument; caller's checks fail (P17) | result written to r0 instead of the stacked r0; frame located on the wrong stack (MSP vs PSP); frame pointer off by the software frame size | break at the end of the SVC handler, `x/8xw $psp`: word 0 must hold the result; `p/x $lr` bit 2 tells which stack |
| **Privileged operation has no effect** | `privilege` event "'cpsid' ignored"; a later BusFault if the task touched the PPB (P12) | task runs unprivileged (CONTROL.nPRIV = 1) and calls kernel code directly instead of through `svc` | `p $control`, `p $primask` after the `cpsid`; BusFault with BFAR in `0xE000xxxx` |
| **Timing off by a factor** | cycles correct but microseconds wrong (P16 wrong_clock: 12.5x); or a constant +1 cycle per period (P16 off_by_one) | core clock not what the code assumes (PLL setup skipped or failed), wrong SysTick CLKSOURCE, `RVR = N` instead of `N - 1` | time 10 periods with CYCCNT *and* an independent timer or a scope; `x/3xw 0xE000E010`; recompute `(RVR + 1) / f_clk` |

## Working method

1. **Reproduce and narrow.** Is it every run, every power cycle, only under
   interrupt load? Power-cycle dependence suggests uninitialised memory (P02,
   P13); load dependence suggests priorities or the context switch (P05, P15).
2. **Read the fault registers before anything else.** A fault handler that
   prints CFSR, HFSR, MMFAR, BFAR, EXC_RETURN and the stacked frame turns most
   crashes into a one-line diagnosis. `r2s fault <ID>` shows what such a
   handler prints.
3. **Find the frame.** EXC_RETURN bit 2 says MSP (0) or PSP (1). The stacked
   PC is the faulting instruction for precise faults; the stacked LR is
   where that function would have returned.
4. **Distrust the crash site.** Corruption is detected where the victim runs,
   not where the culprit wrote (P09). Watermarks, canaries and MPU guards move
   the detection to the culprit.
5. **Measure twice.** Every timing number gets a second, independent
   measurement (CYCCNT vs scope, CYCCNT vs the 1 MHz timer), and the cost of
   the measurement itself is subtracted.

## Practising with mystery mode

`r2s run <ID> --hidden` and the viewer's *Mystery mode* show only the
symptom and the evidence. `r2s quiz` picks a random broken scenario and
asks you to choose the root cause from four options; `--seed N` makes it
repeatable, `--answer K` checks your answer and explains it.
