# Interpreting results

How to read what the simulator (and a debugger on the board) shows you:
register dumps, exception frames, EXC_RETURN, the fault status registers,
cycle measurements, and the ARM vs RISC-V comparison.

## Register dumps

`r2s regs P06 --at-cycle 3000` (or the viewer's register panel):

```
== P06: registers at cycle 3000 (stopped at cycle 3000 (requested 3000))
   context: B | thread mode, unprivileged, SP = PSP

   r0  0x00000078   r1  0x00000000   r2  0x00000000   r3  0x00000000
   r4  0x00000000   r5  0x00000043   r6  0x00000000   r7  0x00000000
   r8  0x00002002   r9  0x00000000   r10 0x00000000   r11 0x00000000
   r12 0x00000000
   sp  0x20000260  (psp)     msp 0x20081FE0   psp 0x20000260
   lr  0x10000225  return address (bit 0 = Thumb)
   pc  0x100001C8  next: subs r0, r0, #1
   xPSR 0x21000000  N=0 Z=0 C=1 V=0 T=1 IPSR=0 -> thread mode
   CONTROL 3  nPRIV=1 (unprivileged thread)  SPSEL=1 (thread uses PSP)
```

Task B is in its delay loop (`spin`): thread mode, unprivileged, on the PSP,
r5 = 0x43 is the letter 'C' it prints next, and LR is the odd return
address back into `taskB`. Try `--at-cycle 2240` to land inside PendSV
instead: IPSR=14, LR=0xFFFFFFFD, SP=MSP.

Read it in this order:

1. **IPSR** (xPSR bits 8:0). 0 = thread mode; otherwise the exception
   number: 3 HardFault, 4 MemManage, 5 BusFault, 6 UsageFault, 11 SVCall,
   14 PendSV, 15 SysTick, 16 + n for IRQ n (IRQ 21 = 37 on the RP2350).
2. **T bit** (xPSR bit 24) must be 1. If it is 0 the next instruction raises
   INVSTATE.
3. **CONTROL**: bit 0 nPRIV (1 = unprivileged thread), bit 1 SPSEL (1 =
   thread uses PSP). CONTROL describes *thread* mode; in a handler the core
   is always privileged and always on MSP, whatever CONTROL says.
4. **Which SP is live**: MSP in handlers and in `main`; PSP in tasks. The
   other one keeps its value, which is how you find a suspended context.
5. **LR**: an odd address is an ordinary return address (bit 0 = Thumb).
   `0xFFFFFFxx` is EXC_RETURN: you are inside a handler.

## Exception frames

On entry the core pushes eight words on the stack that was live, lowest
address first:

| Offset | Word | How to use it |
|---|---|---|
| +0 | r0 | first argument (SVC arguments are read from here) |
| +4, +8, +12 | r1, r2, r3 | more arguments / scratch |
| +16 | r12 | scratch |
| +20 | LR | where the interrupted function would have returned |
| +24 | PC | **return address**: the interrupted instruction, or the faulting instruction for a precise fault |
| +28 | xPSR | flags, T bit (must be 1), IPSR of the interrupted context, bit 9 = a pad word follows |

- If xPSR bit 9 is set, the core inserted a pad word to keep the frame
  8-byte aligned; the original SP is `frame + 36`, not `frame + 32`.
- For an IRQ, the stacked PC is the next instruction to run. For a precise
  fault (DACCVIOL, PRECISERR, UNDEFINSTR, INVSTATE...) it is the instruction
  that faulted. For an imprecise BusFault it is somewhere after the culprit.
- In a kernel, a suspended task's stack has the **software frame** (r4-r11,
  pushed by PendSV) below the hardware frame: from PSP upwards
  r4 ... r11, r0 ... xPSR. `r2s stack P06` and the viewer's stack panel
  label every slot. If the software frame is not exactly the eight
  callee-saved registers, every slot above it is misinterpreted (P08).
- A frame whose xPSR has IPSR != 0, or whose PC is not inside your code, was
  overwritten. The exception return detects the first case as INVPC (P09).

## EXC_RETURN

| Value | Bit 3 (mode) | Bit 2 (SPSEL) | Meaning |
|---|---|---|---|
| `0xFFFFFFF1` | 0 handler | 0 MSP | this handler pre-empted another handler |
| `0xFFFFFFF9` | 1 thread | 0 MSP | interrupted `main` or other MSP thread code |
| `0xFFFFFFFD` | 1 thread | 1 PSP | interrupted a task |

The rule a fault handler or SVC handler uses: `tst lr, #4` → PSP if set,
MSP if clear. PendSV should always see `0xFFFFFFFD`; anything else means it
pre-empted another handler (P15).

These values are valid in the Secure state without FPU context: the RP2350
implements the Security Extension and bare-metal code runs Secure. Non-secure
code, or an Armv8-M core without the Security Extension, sees
`0xFFFFFFB0` / `0xFFFFFFB8` / `0xFFFFFFBC` for the same three cases. With an
active FPU context bit 4 is 0 (`0xFFFFFFED` instead of `0xFFFFFFFD`) and the
frame is larger. The model produces none of those.

## Fault status registers (as modelled)

CFSR (`0xE000ED28`) is three registers in one word:

| Bits | Register | Bits the model sets | Address register |
|---|---|---|---|
| 7:0 | MMFSR (MemManage) | IACCVIOL (0), DACCVIOL (1), MUNSTKERR (3), MSTKERR (4), MMARVALID (7) | MMFAR `0xE000ED34`, valid only if MMARVALID |
| 15:8 | BFSR (BusFault) | IBUSERR (8), PRECISERR (9), UNSTKERR (11), STKERR (12), BFARVALID (15) | BFAR `0xE000ED38`, valid only if BFARVALID |
| 31:16 | UFSR (UsageFault) | UNDEFINSTR (16), INVSTATE (17), INVPC (18), UNALIGNED (24) | none |

HFSR (`0xE000ED2C`): FORCED (bit 30) = a configurable fault was escalated;
VECTTBL (bit 1) = the vector table read failed.

Decoding examples from the scenarios:

| CFSR / HFSR | Reads as | Scenario |
|---|---|---|
| `0x00000082` / `0` | DACCVIOL + MMARVALID: a data access hit an MPU no-access area; MMFAR has the address | P07 |
| `0x00000092` / `0` | the same, plus MSTKERR: the exception frame could not be pushed either (stack overflow) | P07 |
| `0x00008200` / `0` | PRECISERR + BFARVALID: precise bus error; BFAR has the address | P12 (BFAR `0xE000E010`) |
| `0x00009200` / `0x40000000` | PRECISERR + BFARVALID + STKERR, escalated: the stack itself is invalid | P03 |
| `0x00020000` / `0x40000000` | INVSTATE, escalated: executed with T = 0 | P14 |
| `0x00040000` | INVPC: bad EXC_RETURN or corrupted frame (the kernel cleared the earlier bits) | P09 |

Things to remember:

- The bits are **sticky**: they accumulate until software writes 1s to
  clear them. A handler that does not clear them reports old bits next time.
- MMFAR/BFAR are meaningful only with their VALID bit. An instruction-fetch
  fault (IACCVIOL, IBUSERR) has no valid address; use the stacked PC.
- FORCED alone says nothing about the cause; it says "look at CFSR".
- The model records a separate `fault` event for each detected fault, so a
  stacking error after a fault shows as two records; the hardware shows the
  OR of their bits.

## Cycle measurements

Every event has `t_cycles`, the model's DWT CYCCNT. The viewer's counters
show the value, the time at the model clock, the cycles since the previous
event, and the distance to cursor A (set it on one event, step to another:
that is your measurement, like two scope cursors).

When you turn cycles into a result, keep three things in mind:

1. **Convert with the clock you measured, not the one you intended.**
   P16 wrong_clock counts exactly the expected cycles, but the core runs at
   12 MHz instead of 150 MHz, so each tick takes 12.5 times longer.
2. **Subtract the measurement overhead.** Reading CYCCNT costs cycles (a
   load from the PPB: 3 in the model). Measure two back-to-back reads and
   subtract (P10 does this). The overhead itself depends on where the
   measuring code runs: 23 cycles in P10's uncached-flash variant.
3. **Watch for first-run effects.** The first execution of code from flash
   misses in the XIP cache. P16 measures ticks 2 to 12 because the first
   SysTick handler reads CYCCNT about 20 cycles later than the others.
   Report minimum, average and maximum, not a single number.

What each scenario's numbers mean:

| Number | Where | Interpretation |
|---|---|---|
| reset to main | all | the cost of your startup loops; grows with `.data`/`.bss` size |
| IRQ latency min / max / jitter | P04 | min = 12 (entry cost; on the board add the marker store and GPIO path); max = longest masked region; jitter = what users notice |
| button IRQ wait | P05 | 12 when it pre-empts, the rest of the other handler when it cannot |
| context switch avg | P06 | PendSV entry (or tail-chain) to return, including the 8-register save/restore, the scheduler and unstacking |
| SysTick handler avg | P06 | tick bookkeeping cost per tick; multiply by the tick rate for the overhead |
| benchmark cycles raw / corrected | P10, P11 | corrected = raw - overhead |
| XIP hits / misses | P10 | a handful of misses = cold start only; one miss per fetch = cache off or loop too big |

## ARM vs RISC-V comparison (P11): caveats

P11 runs **one** program under two illustrative cost tables. It shows the
*kinds* of differences students measure on the RP2350's Cortex-M33 and
Hazard3 cores, not real numbers:

- **Instructions retired.** Base RV32IMC has no load/store-multiple (a
  `push {r4-r7, lr}` becomes five stores); Hazard3 adds the Zcmp extension
  (`cm.push` / `cm.popret`), which the `riscv_like` profile deliberately
  leaves out, so it models RV32IMC without Zcmp. RISC-V has
  compare-and-branch instructions (`cmp` + `bne` is one `bne rs1, rs2`), and
  it needs two instructions (`lui` + `addi`) for a 32-bit address. The net
  count can go either way.
- **Code size.** Thumb-2's 16-bit encodings make the ARM build denser; the
  RISC-V C extension narrows the gap.
- **CPI** = cycles / instructions retired. A lower instruction count with
  similar cycles means a higher CPI: CPI alone does not say which core is
  faster.
- **Exceptions.** Cortex-M stacks registers in hardware (12-cycle entry);
  RISC-V traps do not, so the handler saves registers in software. Compare
  "entry to first useful instruction", not just the trap entry.
- **What the model ignores.** Pipeline hazards, branch prediction, bus
  contention, compiler differences. On the board, build the same C source
  for both cores at the same optimisation level, read CYCCNT on the M33 and
  `mcycle`/`minstret` on Hazard3 around the same code, subtract each core's
  measurement overhead, and explain each difference by an ISA feature.

## The checks and the PASS/FAIL pill

PASS means the run matched the scenario's **expected outcome**, not that
the system is healthy: P07 passes because the MemManage fault happened
exactly as expected. Each check prints what it found (`r2s run <ID>` lists
them), so a FAIL tells you which expectation broke.
