# What to expect

What a healthy run looks like, the numbers the model produces, the numbers
a real Pico 2 typically shows, and which differences should worry you.

All model numbers below come from `python -m r2s run-all` with the default
seeds. Real-hardware figures are typical values from Arm and Raspberry Pi
documentation and common lab experience; treat them as orders of
magnitude and measure your own board.

## A healthy boot (P01)

```
r2s run P01 --trace
```

You should see, in this order:

1. `boot_step power_on`: SRAM holds garbage, flash holds the image.
2. `boot_step vector_table`: `MSP=0x20082000` (top of the 520 KB SRAM) and
   `word1 -> 0x10000101 -> PC=0x10000100, xPSR.T=1`. Bit 0 of the reset vector
   is 1; the PC is the address with bit 0 cleared.
3. `boot_step reset_handler`: privileged thread mode, MSP.
4. `boot_step data_copied`: every `.data` word equals its flash copy.
5. `boot_step bss_zeroed`: every `.bss` word is 0.
6. `boot_step main_entered`: about **114 cycles** after reset in the model.
7. UART `boot ok`, six LED edges, `counter=1`, `greeting_len=7`, `halt`.

| Measure | Model | Real Pico 2 (typical) |
|---|---|---|
| Reset handler to `main` | 114 cycles for 1 `.data` + 1 `.bss` word | grows with the size of `.data`/`.bss`: a few cycles per word copied or zeroed, plus flash cache misses on the first pass |
| Power-on to `main` | same as above (no boot ROM) | dominated by the boot ROM (image search, IMAGE_DEF parsing), far more than your startup code; time from `Reset_Handler`, not from power-on |

**Suspicious:** any boot step marked FAIL, `main_entered` missing, a fault
before `main`, `counter` not 1, `greeting_len` not 7.

## Interrupts (P04, P05, P18)

| Measure | Model | Real Cortex-M33 (typical) |
|---|---|---|
| Interrupt entry (stack 8 words, fetch vector) | 12 cycles | 12 cycles with zero-wait-state memory (Arm's figure); more if the vector or handler misses in the flash cache |
| Exception return | 10 cycles | about 10 cycles |
| Tail-chain | 6 cycles | a few cycles (6 on Cortex-M3/M4) |
| Edge-to-marker latency, light load (P04) | min 12, max 13, jitter 1 | 12 cycles plus GPIO synchroniser delay plus the cost of the marker store; jitter of a few cycles from the instruction in flight |
| Same, with 60-iteration `cpsid` regions (P04 heavy) | max about 180, jitter about 170 | bounded by your longest critical section |
| Urgent IRQ waiting behind an equal-priority one (P05) | about 550 cycles | the whole duration of the other handler |
| Urgent IRQ with higher priority (P05) | 12 cycles, EXC_RETURN `0xFFFFFFF1` | 12 cycles (+ cache effects) |

**Healthy:** a narrow latency histogram near the hardware floor; pre-emption
visible as an `exception_entry` *from* a handler with LR = `0xFFFFFFF1`;
back-to-back handlers joined by `tail_chain`.

**Suspicious:** a long tail in the histogram (look for `critical_section`
events), an `irq_pend` that is never followed by `exception_entry` (P18,
NVIC not enabled), an exception that lands in `Default_Handler` (P18,
no handler), an interrupt that re-enters forever (status flag never cleared).

## The kernel (P06)

| Measure | Model | Real Pico 2 (typical) |
|---|---|---|
| Tick period | 2000 cycles (13.3 µs), chosen to keep traces short | usually 1 ms (150 000 cycles at 150 MHz) |
| Context switch (PendSV entry to return) | about 56 cycles | tens of cycles for a minimal assembly switch; one to a few hundred with a C scheduler, FPU context or MPU reprogramming |
| SysTick handler | 22 cycles | a few dozen cycles for simple tick bookkeeping |
| Stack watermark of a new task | 64 bytes (16 words = the initial frame) | the initial frame plus the deepest call chain; size stacks with a margin |
| Kernel overhead | about 7 % at a 2000-cycle tick | well under 1 % at a 1 ms tick |
| Switches per second | about 107 000 (because of the fast tick) | about 1000 to 2000 with a 1 ms tick and some yields |

**Healthy pattern every tick:** `irq_pend SysTick` → `exception_entry SysTick`
(LR `0xFFFFFFFD`, frame on the PSP) → `irq_pend PendSV` → `exception_return
... tail-chain` → `tail_chain PendSV` (LR still `0xFFFFFFFD`) →
`context_switch A -> B` → `exception_return` into B, unprivileged.
In the viewer every suspended task shows 8 software words above its PSP
and a hardware frame whose xPSR has T=1 and IPSR=0.

**Suspicious:**

| You see | It suggests | Scenario |
|---|---|---|
| a `context_switch` with EXC_RETURN `0xFFFFFFF1` | PendSV is not the lowest priority | P15 |
| a software frame of 7 words, or the watermark of every task at 60 B instead of 64 B | a register missing from the PendSV list | P08 |
| a task at 100 % watermark | stack overflow (with the MPU: MemManage in its guard; without: corruption) | P07, P09 |
| `UsageFault INVPC` on exception return in a task that did nothing wrong | its saved frame was overwritten | P09 |
| `svc_errors` growing, every `write()` returning its argument | result written to the live r0 | P17 |
| `privilege ... ignored` events | a task tried a privileged instruction | P12 |

## Faults (P03, P07, P12, P14)

| Situation | Expected record |
|---|---|
| bad initial SP (P03) | BusFault PRECISERR at the first `push`, escalated (FORCED), then STKERR while entering HardFault, then **lockup** |
| stack overflow into an MPU guard (P07) | MemManage DACCVIOL + MMARVALID, MMFAR inside the guard below the task's stack base (an address in the pushed range, on the M33 typically the lowest: `0x20000270` in the model); then MSTKERR (CFSR `0x00000092`); task killed, others continue |
| unprivileged SysTick write (P12) | BusFault PRECISERR + BFARVALID, BFAR `0xE000E010` |
| reset vector without the Thumb bit (P14) | HardFault, CFSR `0x00020000` (INVSTATE), HFSR `0x40000000` (FORCED), stacked PC = Reset_Handler, stacked xPSR T=0 |

These encodings are architectural, so a real fault handler that prints
CFSR/HFSR/MMFAR/BFAR and the stacked frame shows the same bits. On the
board, lockup shows `PC=0xEFFFFFFE` (ARMv8-M) and DHCSR.S_LOCKUP set.

## Experiments (P10, P11, P16)

| Measure | Model | Real Pico 2 (typical) |
|---|---|---|
| Loop of 200 iterations, code in SRAM | 2441 cycles (corrected) | similar order; exact count depends on the compiler output |
| Same from flash, cache warm | 2481 cycles (+1.6 %: 8 cold misses) | close to SRAM once the loop is in the 16 KB XIP cache |
| Same from flash, cache off | 38 601 cycles (about 16x) | several times slower; the factor depends on the QSPI clock and burst settings |
| Cost of reading CYCCNT | 3 cycles (23 when the measuring code itself runs from uncached flash) | a few cycles; always measure and subtract it |
| arm_like vs riscv_like CPI | 1.40 vs 1.65; code 36 B vs 80 B | depends on the compiler and the benchmark; Thumb-2 is usually denser, instruction counts differ in both directions |
| SysTick for 1 ms at 150 MHz (P16) | RVR 149 999 → 150 000 cycles, 1000 µs | same arithmetic on the chip |

**Healthy:** the same instruction count (instret) in every P10 variant; only
cycles change. **Suspicious:** cycles right but microseconds wrong (P16
wrong_clock: the core runs at a different frequency than assumed), or a
constant one-cycle drift per period (P16 off_by_one: RVR = N instead of N-1).

## Why the model and the board differ

- The model charges fixed costs per instruction; the Cortex-M33 has a
  pipeline, bus wait states and branch effects.
- The model's flash cache is a small direct-mapped toy with a fixed miss
  penalty; the RP2350 XIP cache and QSPI interface are more complex.
- Interrupt sources are idealised: a button edge pends the IRQ instantly,
  with no synchroniser or debounce.
- The boot ROM, clocks/PLL setup, the second core and the Security
  Extension are not modelled.

Use the model to learn **what** happens and in **which order**, and to know
what to look for; use the board and an oscilloscope for **how long** it takes.
See [interpreting-results.md](interpreting-results.md) for how to read the
numbers and [real-hardware.md](real-hardware.md) for how to measure them.
