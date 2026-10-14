# Scenario catalog

Generated from `scenarios/*.yaml` by `scripts/run_all.py`; edit the YAML, not this file.
Each scenario is one experiment from the course, healthy or with one injected bug.
Run one with `r2s run <ID>`, read its notes with `r2s explain <ID>`, or open it in the
[live viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/) with `#<ID>` appended to the URL.

| ID | Layer | Title | Injected faults | Variants |
|----|-------|-------|-----------------|----------|
| [P01](#p01) | 2 Boot | Clean boot from reset to blink and UART | - | - |
| [P02](#p02) | 2 Boot | Boot fault - startup code forgets to zero .bss | missing_bss_zero | - |
| [P03](#p03) | 2 Boot | Boot fault - wrong initial stack pointer in the vector table | wrong_initial_sp | - |
| [P04](#p04) | 4 Exceptions | Interrupt latency - button IRQ with a GPIO marker, light vs heavy load | - | light, heavy |
| [P05](#p05) | 4 Exceptions | Two interrupts - priority inversion vs fixed priorities, tail-chaining | - | inversion, fixed |
| [P06](#p06) | 5 Kernel | Three-task round-robin kernel on the SysTick | - | - |
| [P07](#p07) | 5 Kernel | Misbehaving task hits the MPU stack guard - kernel kills it, others continue | task_overflows_stack | - |
| [P08](#p08) | 5 Kernel | Context switch bug - PendSV forgets to save and restore r8 | pendsv_forgets_register | - |
| [P09](#p09) | 5 Kernel | Stack overflow without the MPU - watermark hits 100 percent, then corruption | task_overflows_stack, no_mpu | - |
| [P10](#p10) | Experiments | Flash vs SRAM - the same loop timed with the cycle counter, with and without the XIP cache | - | flash_cached, flash_uncached, sram |
| [P11](#p11) | 1 ISA | ISA comparison - the same benchmark under the arm_like and riscv_like cost profiles | - | arm_like, riscv_like |
| [P12](#p12) | 5 Kernel | SVC system call path - privilege round trip, an ignored cpsid and a forbidden SysTick write | task_privileged_op | - |
| [P13](#p13) | 2 Boot | Boot fault - startup code forgets to copy .data from flash | missing_data_copy | - |
| [P14](#p14) | 2 Boot | HardFault at reset - the reset vector is missing the Thumb bit | vector_thumb_bit_missing | - |
| [P15](#p15) | 5 Kernel | PendSV priority not lowest - the context switch runs inside an interrupt handler | pendsv_priority_not_lowest | correct, pendsv_high |
| [P16](#p16) | 3 Drivers | SysTick period - off-by-one reload and a wrong clock assumption | systick_reload_off_by_one, wrong_clock_assumption | correct, off_by_one, wrong_clock |
| [P17](#p17) | 5 Kernel | SVC returns the wrong value - result written to the live r0, not the stacked r0 | svc_return_in_live_r0 | - |
| [P18](#p18) | 3 Drivers | Button interrupt does nothing - NVIC not enabled vs no handler installed | irq_not_enabled, missing_irq_handler | not_enabled, no_handler |

## Layer 1 ISA

### P11

**ISA comparison - the same benchmark under the arm_like and riscv_like cost profiles** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P11))

The same checksum loop as P10 (in SRAM, to keep the memory hierarchy out of the picture) is charged with two illustrative cost tables. arm_like has load/store multiple and 16-bit encodings for simple operations; riscv_like models base RV32IMC without Zcmp: no store/load-multiple (one instruction per register; Hazard3 adds Zcmp's cm.push/cm.popret, which this profile leaves out), compare-and-branch instructions, and mostly 4-byte encodings. The summary reports cycles, instructions retired, CPI and static code size per profile. The numbers are illustrative models, not measurements of real cores.

- **Program:** `bench`
- **Expected:** Both profiles execute the same program; they differ in instret (riscv_like retires fewer because it has compare-and-branch instructions, but more for push/pop), in cycles and CPI, and in static code size (arm_like denser).
- **Checks:** `no_faults` [arm_like]; `no_faults` [riscv_like]; `variant_lt` (metric=bench.static_code_bytes, a=arm_like, b=riscv_like); `variant_lt` (metric=bench.bench_cycles, a=arm_like, b=riscv_like); `variant_lt` (metric=bench.instret, a=riscv_like, b=arm_like)
- **Symptom (mystery mode):** The RISC-V build is larger and retires fewer instructions for the same C code.

What to look for: summary.variants.arm_like.bench vs summary.variants.riscv_like.bench: bench_cycles, instret, cpi and static_code_bytes. The static instruction count (16) is the same because the source is the same; static_code_bytes differs because of the encodings (Thumb-2 16-bit forms vs 32-bit with some compressed forms). instret differs because RISC-V has compare-and-branch instructions (cmp+bne becomes one bne) while base RV32IMC without Zcmp needs five stores for push {r4-r7, lr}. CPI is cycles divided by instret and so moves in the opposite direction to instret. The "bench" CLI command prints this as a table.

Likely root causes on real hardware:

- A real ARM vs RISC-V difference in code size usually comes from Thumb-2's dense 16-bit encodings and load/store multiple; RISC-V narrows it with the C extension, and on Hazard3 with Zcmp (cm.push/cm.popret) when the compiler uses it.
- A difference in instruction count comes from ISA features: compare-and-branch instructions and no condition flags on RISC-V, multi-register push/pop on ARM (base RV32IMC has no store-multiple).
- A CPI difference on the real board also includes pipeline depth, branch prediction and memory latency, which this model does not have.

How to debug:

1. On the board: build the same C source for both cores with -O2 and compare 'size' output (text bytes) and the objdump instruction counts.
2. Read DWT CYCCNT on the Cortex-M33 and mcycle/minstret on Hazard3 around the same loop; compute CPI = cycles / instructions for each.
3. Vary the benchmark (load/store heavy vs branch heavy) and explain each change in terms of an ISA feature, not a guess.
4. Subtract the counter read overhead on each core before comparing.

What this means: Instructions, cycles and bytes are three different measures and an ISA can win one while losing another. Cycles per instruction is the bridge between them; comparing only wall time hides which architectural feature made the difference. This simulation shows the shape of the comparison; the real board gives the numbers.

## Layer 2 Boot

### P01

**Clean boot from reset to blink and UART** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P01))

The reference run. Power-on, the core reads the vector table (initial MSP and Reset_Handler), the startup code copies .data from flash to SRAM and zeroes .bss, then calls main. main prints "boot ok" over the UART, blinks the LED three times, increments a .bss counter and prints a .data value. Every boot step is a trace event, so this run is the baseline every other boot scenario is compared against.

- **Program:** `blink`
- **Expected:** All six boot steps pass, "boot ok", "counter=1" and "greeting_len=7" are printed, no faults, halt at the end of main.
- **Checks:** `boot_complete`; `no_faults`; `uart_contains` (text=boot ok); `uart_contains` (text=counter=1); `uart_contains` (text=greeting_len=7); `min_events` (kind=gpio, count=6); `event_sequence` (kinds=['boot_step:power_on', 'boot_step:vector_table', 'boot_step:reset_handler', 'boot_step:data_copied', 'boot_step:bss_zeroed', 'boot_step:main_entered', 'uart_tx', 'gpio', 'halt'])
- **Symptom (mystery mode):** LED blinks and the terminal shows three lines.

What to look for: The first six events are boot steps in the order power_on, vector_table, reset_handler, data_copied, bss_zeroed, main_entered. The vector_table event shows the two words the core read: MSP=0x20082000 (top of SRAM) and the Reset_Handler address in flash. The data_copied and bss_zeroed events report how many words were handled and that the check passed. main_entered gives the cycle count from reset to main, the number a student measures with the DWT counter on the board. After that: one UART line, six LED edges, two more UART lines, and a halt event.

Likely root causes on real hardware:

- This is the healthy baseline: nothing is wrong. If your board does not show this sequence, compare its first divergence against the P02/P03 traces.

How to debug:

1. On the board: set a hardware breakpoint at Reset_Handler, reset, and single-step; watch SP and PC in the registers view.
2. In GDB: 'monitor reset halt' stops in the RP2350 boot ROM; 'hbreak Reset_Handler' and 'continue', then 'x/2xw 0x10000000': SP must equal word 0 and PC word 1 with bit 0 cleared, because the boot ROM loads them from your table.
3. Count cycles from reset to main with DWT CYCCNT (enable TRCENA in DEMCR first) and compare with the main_entered event.

What this means: Between power-on and main() the processor does only three things on its own: load MSP, load PC from the reset vector and start executing. Everything else (copying .data, zeroing .bss, calling main) is code the student wrote. The trace makes that split visible: the first two events are hardware, the rest is startup code.

### P02

**Boot fault - startup code forgets to zero .bss** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P02))

The same program as P01, but the reset handler skips the .bss loop. SRAM is not zero at power-on (the model fills it with seeded pseudo-random words), so the uninitialised global "counter" starts with garbage instead of 0 and the program prints a nonsense value. Nothing crashes, which is exactly why this bug is hard to find on real hardware.

- **Program:** `blink`
- **Injected:** `missing_bss_zero`: Reset handler does not zero .bss: uninitialised globals keep power-on garbage.
- **Expected:** Boot reaches main without faults, but the bss_zeroed step reports garbage and "counter=" is not 1.
- **Checks:** `boot_step` (step=bss_zeroed, ok=False); `boot_step` (step=data_copied, ok=True); `no_faults`; `uart_contains` (text=boot ok); `uart_not_contains` (text=counter=1
); `uart_contains` (text=greeting_len=7)
- **Symptom (mystery mode):** LED blinks, 'boot ok' prints, but 'counter=' shows a huge number instead of 1 and it changes between power cycles.

What to look for: Compare with P01. The boot_step event for bss_zeroed says the check FAILED and names the global that still holds garbage (counter=0x........). The program then runs normally: "boot ok", six LED edges, but the UART line "counter=" shows a large random number instead of 1, while "greeting_len=7" is still correct because .data was copied. Change the seed in the YAML and the garbage value changes: the symptom is non-deterministic on real hardware.

Likely root causes on real hardware:

- The reset handler has no .bss loop, or loops from __bss_start to __bss_start (wrong end symbol).
- The linker script does not define __bss_start/__bss_end, or they are not aligned so the loop stops early.
- A .bss section was renamed (.bss.*, COMMON) and the linker script does not collect it into the zeroed range.
- Globals placed in .noinit on purpose, then read as if they were zeroed.

How to debug:

1. In GDB after reset, before main: 'print &counter' then 'x/wx &counter'. A non-zero value before any code touched it is the bug.
2. Run 'arm-none-eabi-nm -n' or 'readelf -S' and check the symbol lives between __bss_start and __bss_end.
3. Break on main, inspect several .bss globals: if they are all random, the loop never ran; if only the last ones are, the end symbol is wrong.
4. Power-cycle the board (not just reset): the value changes between power cycles when SRAM retains nothing.

What this means: C promises that globals without an initialiser are zero, but it is the startup code, not the hardware, that keeps that promise. A missing .bss loop produces a program that is correct in a simulator that zeroes memory and wrong on a board whose SRAM wakes up with random contents.

### P03

**Boot fault - wrong initial stack pointer in the vector table** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P03))

Word 0 of the vector table (the initial MSP) is 0 instead of the top of SRAM. Reset itself works and the reset handler runs, because the startup loops never touch the stack. The first push (the "push {r4, lr}" prologue of main) writes below address 0, into unmapped memory: a precise BusFault. BusFault is disabled at reset, so it escalates to HardFault, and entering HardFault needs to push eight words on the same broken stack. On Armv8-M a fault while entering or running HardFault (priority -1) generally leads to lockup, and the model locks up here.

- **Program:** `blink`
- **Injected:** `wrong_initial_sp`: Vector table word 0 holds a bad MSP: the first push (or exception entry) faults.
- **Expected:** Boot steps up to main_entered pass (vector_table flagged); the first push is a BusFault escalated to HardFault, its entry fails with STKERR, and the core locks up.
- **Checks:** `boot_step` (step=vector_table, ok=False); `boot_step` (step=main_entered, ok=True); `fault_kind` (kind=HardFault); `fault_reason_contains` (text=stacking error); `fault_flag` (flag=STKERR); `fault_escalated_from` (kind=BusFault); `lockup` (value=True); `uart_contains` (text=boot ok)
- **Symptom (mystery mode):** Board prints 'boot ok' then hangs; the debugger shows the core in lockup with HFSR.FORCED set.

What to look for: The vector_table boot step is flagged: MSP=0x00000000 is not inside SRAM. Execution still reaches main (the startup loops do not use the stack) and "boot ok" is printed. The first instruction that touches the stack is "push {r4, lr}" in main, which would write 0xFFFFFFF8..0xFFFFFFFF: a BusFault PRECISERR with BFAR set to an address inside the pushed range (on the M33 typically the lowest; the model stores in ascending order and reports 0xFFFFFFF8), escalated to HardFault because BusFault is not enabled in SHCSR (HFSR.FORCED). Entering HardFault must push eight words below SP=0: a second fault event "stacking error ... STKERR", then a lockup event (the model treats a fault during HardFault entry as lockup, which is what Armv8-M generally does at priority -1). The last fault record has stacked_frame=null, CFSR=0x00009200 (PRECISERR, BFARVALID, STKERR) and sp=msp=0xFFFFFFE0: the decoder tells you the stack itself is the problem. Run "r2s fault P03" for the decoded registers.

Likely root causes on real hardware:

- The linker script's _estack / __stack_top symbol is wrong (0, inside flash, or past the end of SRAM).
- The vector table's first entry was written as a function instead of the stack symbol, or the table is missing the SP word entirely.
- The startup file sets MSP by hand from a wrong constant after reset.
- The table was placed somewhere else than where the boot ROM / VTOR expects it, so word 0 is really some other data.

How to debug:

1. In GDB: 'monitor reset halt' (the RP2350 halts in the boot ROM), 'hbreak Reset_Handler', 'continue', then 'x/2xw 0x10000000', 'p/x $sp' and 'p/x $pc'. The boot ROM loads MSP and PC from the table: SP must equal word 0 and be inside SRAM (0x2000xxxx), PC must equal word 1 with bit 0 cleared.
2. Read SCB->HFSR (0xE000ED2C): FORCED set means an escalated fault; SCB->CFSR (0xE000ED28) with STKERR/MSTKERR set means the exception frame could not be pushed.
3. On a lockup the debugger shows PC=0xEFFFFFFE on ARMv8-M (0xFFFFFFFE on ARMv7-M) and DHCSR.S_LOCKUP=1; do not look for the bug at that address, look at SP.
4. Fix the symbol, rebuild, and verify with 'arm-none-eabi-objdump -s -j .isr_vector' that word 0 is the SRAM top.

What this means: Word 0 of the vector table is loaded straight into MSP by hardware; no code checks it. A bad value is harmless until the first push or the first exception, which makes the crash appear far from its cause. A fault that cannot stack its frame is reported as a HardFault with STKERR, and that specific flag is the clue that the stack pointer is wrong.

### P13

**Boot fault - startup code forgets to copy .data from flash** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P13))

The same program as P01, but the reset handler skips the .data copy loop. The initial value of the global "greeting_len" (7) sits in flash at __data_load, but nothing copies it to its run-time address in SRAM, so the program reads whatever SRAM held at power-on. .bss is still zeroed, so the uninitialised counter is correct. Nothing faults; one number is simply wrong. Compare with P02, the mirror-image bug.

- **Program:** `blink`
- **Injected:** `missing_data_copy`: Reset handler does not copy .data from flash: initialised globals are wrong.
- **Expected:** Boot reaches main without faults; the data_copied step reports a mismatch; "counter=1" is correct but "greeting_len=" is not 7.
- **Checks:** `boot_step` (step=data_copied, ok=False); `boot_step` (step=bss_zeroed, ok=True); `no_faults`; `uart_contains` (text=counter=1); `uart_not_contains` (text=greeting_len=7); `global_ne` (name=greeting_len, value=7)
- **Symptom (mystery mode):** 'boot ok' and 'counter=1' print correctly, but a configuration value that was initialised in the source reads as a random number.

What to look for: The data_copied boot step says the check FAILED and prints the mismatch, for example "greeting_len: SRAM=0x5D6E... expected 0x00000007". The bss_zeroed step passes. The UART shows "boot ok" and "counter=1" (from .bss, correct) but "greeting_len=" followed by a large random number. image_map shows the two addresses involved: data_load (the flash copy of the initial values, 0x100001xx) and data.greeting_len (0x20000000, its run-time home). Run "r2s disasm blink" to see both in the map and the copy loop that is missing here.

Likely root causes on real hardware:

- The reset handler has no .data copy loop, or it copies from the wrong source symbol (the run-time address instead of the load address).
- The linker script does not give .data separate load (AT> FLASH) and run (> RAM) addresses, so __data_load is wrong.
- The loop bounds use __data_start/__data_end from a different section, or are not word aligned, so the last globals are skipped.
- The variable is const and was expected in flash, but a cast or a missing const put it in .data.

How to debug:

1. Before main runs, compare the two copies in GDB: 'x/4wx &__data_load' (flash) and 'x/4wx &__data_start' (SRAM). They must be equal after the copy loop.
2. 'arm-none-eabi-objdump -h' shows .data with different VMA (0x2000xxxx) and LMA (0x100xxxxx); if they are equal, the linker script is missing AT> FLASH.
3. Set a watchpoint on the global ('watch greeting_len') and reset: if it never changes before main, the copy never ran.
4. Power-cycle the board: a value that changes between power cycles is power-on SRAM garbage, not a logic bug.

What this means: An initialised global has two addresses: where its initial value is stored (flash, the load address) and where the program uses it (SRAM, the run address). Hardware never copies one to the other; the startup code does. P02 and P13 are the two halves of the same lesson: C's promises about globals are kept by the code that runs before main.

### P14

**HardFault at reset - the reset vector is missing the Thumb bit** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P14))

The vector table was written by hand and the reset entry holds the plain address of Reset_Handler with bit 0 clear. Cortex-M only executes Thumb code: bit 0 of every vector must be 1, and the core copies it into the xPSR T bit. With T=0 the very first instruction raises a UsageFault INVSTATE. UsageFault is disabled at reset, so it escalates to HardFault. The HardFault vector is correct, so the fault handler runs, prints the fault registers on the UART and stops. Not one line of the startup code ran.

- **Program:** `blink`
- **Injected:** `vector_thumb_bit_missing`: A vector table entry has bit 0 clear: the handler starts with xPSR.T=0 and faults INVSTATE.
- **Expected:** The vector_table step reports the missing Thumb bit; Reset_Handler never runs; a HardFault (FORCED) escalated from UsageFault INVSTATE with the stacked PC at Reset_Handler and stacked xPSR.T=0; the fault printer reports it.
- **Checks:** `boot_step` (step=vector_table, ok=False); `boot_problem_contains` (text=Thumb bit missing); `no_event` (kind=boot_step, to=reset_handler); `fault_kind` (kind=HardFault); `fault_flag` (flag=INVSTATE); `fault_escalated_from` (kind=UsageFault); `fault_count` (count=1); `uart_contains` (text=*** HardFault ***); `uart_contains` (text=UFSR.INVSTATE); `uart_not_contains` (text=boot ok); `lockup` (value=False)
- **Symptom (mystery mode):** The board never prints 'boot ok'; the only output is a HardFault dump.

What to look for: The vector_table boot step reads "word1 -> 0x10000100 -> PC=0x10000100, xPSR.T=0" and flags "reset vector has bit 0 clear". There is no reset_handler boot step: the first fetch never executes. The fault event says UsageFault INVSTATE at PC=0x10000100 escalated to HardFault (UsageFault disabled in SHCSR). The exception_entry for HardFault shows the stacked frame: pc=0x10000100 (Reset_Handler itself) and xpsr=0x00000000 (T bit clear, IPSR 0). The UART shows the fault printer's output: CFSR=0x00020000 (UFSR.INVSTATE), HFSR=0x40000000 (FORCED). Compare the vector table entries in image_map with P01: every handler entry there ends in an odd digit.

Likely root causes on real hardware:

- A hand-written vector table in assembly that uses a label without .thumb_func (or .type func, %function), so the assembler does not set bit 0.
- A vector table filled with computed addresses (e.g. (uint32_t)&handler & ~1, or a constant like 0x10000100).
- Relocating the vector table to SRAM and copying entries through a mask that clears bit 0.
- The same bug in a branch target: 'bx r0' with an even address, or a function pointer built from a data address, gives the same INVSTATE fault later in the program.

How to debug:

1. Dump the table: 'x/4xw 0x10000000' in GDB. Word 1 (reset) and every handler entry must be odd (0x....1, 0x....3 ...).
2. In the HardFault handler read HFSR (0xE000ED2C): FORCED set means look at CFSR (0xE000ED28); bit 17 (INVSTATE) points at the Thumb bit.
3. Look at the stacked xPSR: bit 24 (T) is 0. The stacked PC is the address the core tried to execute in the wrong state.
4. Fix the source (.thumb_func, or let the C compiler emit the table) and confirm with 'arm-none-eabi-objdump -s -j .vectors' that the entries are odd.

What this means: The Thumb bit is how Cortex-M checks that a code address really is a Thumb code address. It lives in bit 0 of every vector, of every bx target and of the stacked return state. Forgetting it does not crash the program somewhere random: it crashes the very first instruction that uses the bad address, and the fault status registers name the cause precisely (INVSTATE), which is why a fault decoder is the first tool to build.

## Layer 3 Drivers

### P16

**SysTick period - off-by-one reload and a wrong clock assumption** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P16))

A bare-metal program programs SysTick for a 1 ms tick and measures ten tick periods twice: with the core cycle counter (DWT CYCCNT) and with the 1 MHz system timer (TIMERAWL), which runs from its own reference and does not depend on the core clock. Three variants. "correct": RVR = 150000 - 1 for a 150 MHz core. "off_by_one": RVR = 150000, so every period is 150001 cycles. "wrong_clock": the reload is computed for 150 MHz, but the core is still running at 12 MHz (the clock setup was skipped), so each tick takes 12.5 times longer in real time while looking perfect in cycles.

- **Program:** `tick`
- **Injected:** `systick_reload_off_by_one` (variant `off_by_one`): SYST_RVR is loaded with N instead of N-1: every tick is one cycle too long.
- **Injected:** `wrong_clock_assumption` (variant `wrong_clock`): The reload is computed for 150 MHz but the core still runs from a slower clock.
- **Expected:** correct gives 150000 cycles and 1000 us per tick; off_by_one gives 150001 cycles per tick; wrong_clock gives 150000 cycles but 12500 us per tick.
- **Checks:** `no_faults` [correct]; `metric_equals` (metric=tick.period_cycles, value=150000) [correct]; `metric_equals` (metric=tick.period_us, value=1000) [correct]; `metric_equals` (metric=tick.period_cycles, value=150001) [off_by_one]; `metric_equals` (metric=tick.period_cycles, value=150000) [wrong_clock]; `metric_between` (metric=tick.period_us, min=12400, max=12600) [wrong_clock]
- **Symptom (mystery mode):** The scheduler tick looks right in the debugger, but task periods measured on the scope are 12.5 times too long (or drift slowly).

What to look for: The program times ticks 2 to 12, not 1 to 11: the first SysTick handler runs from a cold XIP cache and reads CYCCNT about 20 cycles later than the others, which would bias the result (a real effect worth seeing once: run "r2s run P16 --trace --kinds exception_entry,exception_return" and compare the first handler's duration with the second's). Then read the UART lines "10 ticks: cycles=" and "10 ticks: us=" in each variant, and summary.variants.<name>.tick (also in the measurements table). correct: 1500000 cycles and 10000 us. off_by_one: 1500010 cycles. One extra cycle at 150 MHz is about 6.7 ns per tick, 0.07 us over the 10 measured ticks, so the 1 MHz timer still reads 10000 us: invisible in a short test, but about 0.58 s per day at 1000 ticks per second. wrong_clock: 1500000 cycles (the cycle count cannot see the problem, it is counted in the wrong unit) but 125000 us. The irq_pend SysTick events are spaced 150000 or 150001 cycles apart; the t_us column of the trace uses the model's clock, so in wrong_clock the ticks are 12.5 ms apart.

Likely root causes on real hardware:

- RVR loaded with the period instead of period - 1: SysTick counts RVR..0, so the period is RVR + 1 cycles.
- The reload computed for the final PLL frequency while the core still runs from the crystal or ring oscillator (clock setup skipped, failed, or done after SysTick setup).
- CLKSOURCE (SYST_CSR bit 2) selecting the external reference instead of the processor clock, or the reverse, so the tick runs at a different rate than the formula assumes.
- A reload larger than 24 bits (0xFFFFFF) silently truncated by the hardware.

How to debug:

1. Time ten ticks with two independent clocks (CYCCNT and the 1 MHz timer, or a GPIO toggle on the scope). If cycles look right but time does not, the clock frequency is wrong; if both are off by a constant, the reload is wrong.
2. Read back SYST_RVR (0xE000E014) and SYST_CSR (0xE000E010) in GDB and redo the arithmetic: period = (RVR + 1) / f_clk.
3. Check the real core frequency: toggle a pin in a loop of known cycle count and measure it on the scope, or read the clock registers.
4. Convert every cycle measurement to time with the clock you measured, not the clock you intended.

What this means: A timer period is a count times a clock period, and both can be wrong independently. Counting in cycles confirms the count; only a second, independent time base confirms the clock. That is why the course asks for every on-chip number to be cross-checked on the oscilloscope.

### P18

**Button interrupt does nothing - NVIC not enabled vs no handler installed** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P18))

The latency program from P04 with one button press, in two broken variants. "not_enabled": main writes the priority but never sets the IRQ's bit in NVIC_ISER. The GPIO block still latches the edge and the NVIC marks IRQ 21 pending, but a disabled interrupt is never taken: the program runs on as if nothing happened. "no_handler": the interrupt is enabled, but the vector table entry for IRQ 21 still points at Default_Handler, the catch-all loop: the core takes the interrupt and then spins forever.

- **Program:** `latency`
- **Injected:** `irq_not_enabled` (variant `not_enabled`): main never sets the IRQ's bit in NVIC_ISER: the edge is latched by the peripheral, never taken.
- **Injected:** `missing_irq_handler` (variant `no_handler`): The IRQ is enabled but its vector still points at Default_Handler.
- **Expected:** not_enabled shows the IRQ pending but never taken (no exception entry, presses=0, the run ends on max_cycles); no_handler enters IRQ_GPIO at Default_Handler and stays there.
- **Checks:** `no_faults` [not_enabled]; `min_events` (kind=irq_pend, to=IRQ_GPIO, count=1) [not_enabled]; `no_event` (kind=exception_entry) [not_enabled]; `global_equals` (name=presses, value=0) [not_enabled]; `stop_reason_contains` (text=max_cycles) [not_enabled]; `min_events` (kind=exception_entry, to=ISR:IRQ_GPIO, count=1) [no_handler]; `stop_reason_contains` (text=Default_Handler) [no_handler]; `global_equals` (name=presses, value=0) [no_handler]
- **Symptom (mystery mode):** Pressing the button does nothing. In one build the program keeps running normally; in another build it freezes on the first press.

What to look for: not_enabled: an irq_pend event "GPIO edge on pin 14" and then nothing: no exception_entry, no marker pin, presses stays 0, and the run only ends at max_cycles. On the board NVIC_ISPR0 bit 21 is 1 (pending) while NVIC_ISER0 bit 21 is 0 (disabled). no_handler: exception_entry IRQ_GPIO with handler=Default_Handler's address (compare with image_map.vectors and the symbol in "r2s disasm latency"), then a kernel event "Default_Handler reached for IRQ_GPIO (IPSR=37)" and the run stops there. IPSR = 16 + 21 = 37 is the number to read in the debugger.

Likely root causes on real hardware:

- NVIC_EnableIRQ (or the NVIC_ISER write) missing, or written with the wrong bit/word: IRQ n is bit n % 32 of NVIC_ISER[n / 32].
- The peripheral's own interrupt enable (here the GPIO edge enable) set but not the NVIC's, or the reverse: both are needed.
- Handler name misspelled (GPIO_IRQHandler vs IO_IRQ_BANK0_IRQHandler), so the weak alias to Default_Handler stays in the vector table.
- Handler defined in C++ without extern "C", so its mangled name does not override the weak symbol.

How to debug:

1. In GDB read NVIC_ISER0 (0xE000E100) and NVIC_ISPR0 (0xE000E200): pending but not enabled is the first variant.
2. When the board 'hangs', halt it and 'p/x $xpsr': the low 9 bits (IPSR) give the exception number; 37 = IRQ 21. 'info symbol $pc' shows Default_Handler.
3. Check the vector table entry: 'x/xw 0x10000000 + 4*37' must be your handler's address with bit 0 set.
4. 'arm-none-eabi-nm' the ELF: if the handler symbol is missing or is a weak alias, the name does not match the startup file.

What this means: An interrupt needs three things to run your code: the peripheral must raise it, the NVIC must have it enabled, and the vector table must point at your handler. The first variant fails the second condition silently; the second variant fails the third and parks the core in a loop. Reading the pending/enable bits and IPSR tells the two apart in a minute.

## Layer 4 Exceptions

### P04

**Interrupt latency - button IRQ with a GPIO marker, light vs heavy load** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P04))

A pushbutton on GP14 triggers IRQ_GPIO (IRQ 21, IO_IRQ_BANK0 on the RP2350, priority 0x40 written into byte 1 of NVIC_IPR5). The handler's first instruction sets the marker pin GP16, which an oscilloscope would see. The scenario presses the button 24 times at irregular intervals and measures, for each press, the cycles from the edge to the marker. Two variants run the same program: in "light" the main loop never masks interrupts; in "heavy" it spends part of its time inside cpsid/cpsie critical sections, so a press that lands inside one waits until cpsie.

- **Program:** `latency`
- **Expected:** Light load gives a tight histogram near the 12-cycle entry cost; heavy load spreads it up to the critical-section length with much larger jitter.
- **Checks:** `no_faults` [light]; `no_faults` [heavy]; `latency_jitter_gt` (a=heavy, b=light); `variant_lt` (metric=latency.max, a=light, b=heavy)
- **Symptom (mystery mode):** Button response is usually instant but sometimes visibly late; the latency histogram has a long tail.

What to look for: summary.variants.light.latency: samples=24, min and max within a cycle of 12 (the exception entry cost in the arm_like profile; Arm quotes 12 cycles for the Cortex-M33 with zero-wait-state memory), jitter 0-1 (the instruction in flight must finish first). summary.variants.heavy.latency: the same minimum (presses that land outside a critical section) but a maximum close to the critical-section length, a histogram with several buckets and jitter of well over 100 cycles. In the event log each press is a gpio stimulus event, then irq_pend, exception_entry and the MARKER pin 16 -> 1 event. In the heavy variant some presses are followed by a critical_section event "cpsie i after N cycles with PRIMASK=1; IRQ_GPIO was pending for M cycles": that M is exactly the extra latency. summary.variants.heavy.critical_sections gives the count and the longest masked stretch.

Likely root causes on real hardware:

- Long critical sections (cpsid i / __disable_irq) in the main loop or in drivers.
- Another interrupt of the same or higher priority running when the button fires (see P05).
- The GPIO marker is set late in the handler (after prologue work), so the scope measures handler code, not latency.
- A slow flash fetch of the handler (XIP cache miss) adds cycles the first time; see P10.

How to debug:

1. Toggle the marker pin as the very first instruction of the handler and trigger the scope on the button edge; measure edge-to-marker.
2. Read DWT CYCCNT at the start of the handler and keep min/max/avg in globals; print them from the main loop.
3. Search the code for cpsid/__disable_irq and measure each region's length in cycles; the longest region bounds the worst-case latency.
4. Repeat with the main loop idle (wfi) to get the floor, then add load and watch the histogram widen.

What this means: Interrupt latency has a hardware floor (the cycles to stack the frame and fetch the vector) and a software ceiling set by the longest stretch during which interrupts cannot be taken. The average hides the ceiling; the histogram and the maximum show it. On the board the scope confirms the marker, and any difference from the counter is the cost of the measurement code itself.

### P05

**Two interrupts - priority inversion vs fixed priorities, tail-chaining** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P05))

A slow timer interrupt (IRQ_TIMER0, 200 loop iterations of work) and an urgent button interrupt (IRQ_GPIO). The button is pressed while the timer handler is running. In the "inversion" variant both share priority 0x80, so the button must wait for the whole timer handler and is then tail-chained. In the "fixed" variant the button gets priority 0x40 (more urgent), pre-empts the timer handler in the middle, and returns into it.

- **Program:** `priority`
- **Expected:** With equal priorities the button waits ~500 cycles and is tail-chained after the timer; with a higher priority it is taken in ~12 cycles by pre-empting the timer handler.
- **Checks:** `no_faults` [inversion]; `no_faults` [fixed]; `tail_chains_min` (count=1) [inversion]; `priority_preempted` (value=False) [inversion]; `priority_preempted` (value=True) [fixed]; `variant_lt` (metric=priority.button_latency, a=fixed, b=inversion)
- **Symptom (mystery mode):** Button response time depends on whether the timer handler happens to be running.

What to look for: Inversion variant: exception_entry for IRQ_TIMER0, then the button irq_pend while "ISR:IRQ_TIMER0" is the context, then no entry until the timer handler's "exception_return ... tail-chain" event, followed by a tail_chain event for IRQ_GPIO. summary.variants.inversion.priority.button_latency is several hundred cycles and tail_chains is 1. Fixed variant: exception_entry for IRQ_GPIO with from="ISR:IRQ_TIMER0" (pre-emption), the marker pin toggles, exception_return back to "ISR:IRQ_TIMER0", and the timer handler finishes afterwards. button_latency is 12 cycles, the GPIO entry event shows LR=EXC_RETURN 0xFFFFFFF1 ("return to handler mode") and its stacked frame holds the timer handler's registers, with the stacked PC inside Timer_Handler's loop. The priorities are byte-packed: IRQ_TIMER0 is byte 0 of NVIC_IPR0, IRQ_GPIO (IRQ 21) is byte 1 of NVIC_IPR5.

Likely root causes on real hardware:

- All interrupts left at the default priority 0 (or all the same), so nothing can pre-empt anything.
- Priorities written to the wrong NVIC_IPR byte (they are byte-packed on the real chip) or written with the wrong shift for the implemented bits.
- A long handler doing work that belongs in the main loop or a task.
- Priority grouping (AIRCR.PRIGROUP) set so that the intended pre-emption bits are sub-priority bits.

How to debug:

1. Read back NVIC->IPR for both IRQs in GDB and check the upper bits actually differ.
2. Put a marker pin in each handler (set on entry, clear on exit) and look at both on the scope: nested pulses mean pre-emption, back-to-back pulses mean tail-chaining.
3. Set a breakpoint in the button handler and inspect the stacked frame: if the stacked PC is inside the timer handler, the pre-emption worked.
4. Shorten the slow handler and move its work to the main loop; the long tail disappears regardless of priorities.

What this means: Only a numerically lower priority can pre-empt a running handler. An interrupt of equal priority cannot pre-empt: it stays pending until the running handler returns, and when several are pending at the same priority the one with the lowest exception number is taken first (not the one that arrived first). The NVIC then saves the unstack/restack by tail-chaining it. A priority scheme is the only way to bound the latency of the urgent interrupt, and the stacked frame of the pre-empting handler is the proof that the processor stopped in the middle of another handler.

## Layer 5 Kernel

### P06

**Three-task round-robin kernel on the SysTick** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P06))

The final checkpoint of weeks 7-9. main builds three task stacks (A blinks the LED, B prints the alphabet through the write system call, C counts and yields), programs the MPU with a guard below each stack, starts SysTick at one tick per 2000 cycles and executes "svc #0". The SVC handler returns into task A as an unprivileged thread on the PSP. Every tick pends PendSV, whose handler saves r4-r11, asks the scheduler for the next task and restores its registers. The run stops after 30 ticks.

- **Program:** `kernel`
- **Expected:** 30 ticks, 40+ context switches, all tasks alive, the alphabet appears on the UART, the LED toggles, stack watermarks around 25 percent, no faults.
- **Checks:** `no_faults`; `min_switches` (count=40); `task_alive` (task=A); `task_alive` (task=C); `uart_contains` (text=ABCDEFGHIJKLMNOPQRSTUVWXYZ); `global_gt` (name=a_iters, value=5); `global_gt` (name=c_count, value=5); `global_equals` (name=svc_errors, value=0); `global_equals` (name=corruptions, value=0); `event_sequence` (kinds=['boot_step:main_entered', 'task_state:READY', 'mpu', 'syscall', 'exception_entry:ISR:SVCall', 'context_switch:A', 'exception_return', 'irq_pend:SysTick', 'exception_entry:ISR:SysTick', 'irq_pend:PendSV', 'tail_chain:ISR:PendSV', 'context_switch:B'])
- **Symptom (mystery mode):** LED blinks, alphabet scrolls, counter increments; three things happen at once on one core.

What to look for: After main_entered: four task_state events (A, B, C and idle created, each with its stack range and initial PSP), an mpu event listing eight non-overlapping regions, the SysTick programming event (PendSV priority 0xFF reads back as 0xF0: four priority bits), then svc #0: exception_entry for SVCall from main, "svc start" with CONTROL=3, and an exception_return "back to thread mode (unprivileged)" from the PSP. From then on every 2000 cycles: irq_pend SysTick, exception_entry SysTick with EXC_RETURN 0xFFFFFFFD (frame pushed on the task's PSP), irq_pend PendSV, "SysTick returning, but PendSV is pending: tail-chain", tail_chain PendSV, context_switch A->B with both PSPs, exception_return into B. Task C yields with svc #1, so its slices are short. The viewer timeline shows A, B, C in rotation with thin handler strips between them; click a context_switch to see every suspended task's saved frame. summary.kernel_timing gives the switch cost (about 56 cycles: tail-chain, 8-register save and restore, scheduler, unstacking) and the tick handler cost. summary.kernel.tasks.*.watermark.percent is about 25 (the 64-byte initial frame on a 256-byte stack). The tick is 2000 cycles only to keep the trace short; a real kernel ticks at about 1 kHz (150000 cycles at 150 MHz).

Likely root causes on real hardware:

- Healthy baseline. Compare with P07/P08/P09 for the broken variants.
- If your board shows no switching: SysTick not enabled (CSR bits), TICKINT clear, or PendSV not pended from the tick handler.
- If the first task never starts: the initial frame is built in the wrong order, the xPSR word is missing the Thumb bit, or EXC_RETURN is 0xFFFFFFF9 instead of 0xFFFFFFFD.

How to debug:

1. Break in PendSV_Handler and print the PSP and the 16 words above it; they must match the TCB layout diagram (r4-r11 then the hardware frame).
2. Toggle a different GPIO pin in each task and watch the three square waves interleave on the scope at the tick period.
3. Check ICSR after the tick handler: PENDSVSET must be 1 until PendSV runs.
4. Count switches per second in a global and print it once a second; compare with ticks per second.

What this means: A preemptive kernel is a timer interrupt, a lowest-priority exception that does the switch, and a stack per task that holds the task's registers while it is not running. The hardware frame is pushed by the core for free; the kernel only adds r4-r11 and swaps PSP. Everything in the trace between two context_switch events is one time slice.

### P07

**Misbehaving task hits the MPU stack guard - kernel kills it, others continue** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P07))

Same kernel as P06, but task C starts recursing without bound after a few iterations. Each level pushes four words and never returns. The stack grows down into the 32-byte guard region below it, which the MPU marks no-access for everyone. The first push into the guard is a MemManage fault; the fault handler identifies the task from EXC_RETURN and PSP, marks it DEAD and switches to another task. A and B keep running and the UART keeps printing.

- **Program:** `kernel`
- **Injected:** `task_overflows_stack`: Task C recurses without bound and runs off the bottom of its stack.
- **Expected:** One MemManage fault with MMARVALID pointing into guard_C; task C is DEAD with a 100 percent watermark; A and B keep running; no lockup.
- **Checks:** `fault_kind` (kind=MemManage); `fault_reason_contains` (text=stack guard); `task_state` (task=C, state=DEAD); `task_alive` (task=A); `watermark_min_percent` (task=C, value=100); `lockup` (value=False); `uart_contains` (text=ABCDEFGHIJKLMNOPQRSTUVWXYZ); `event_sequence` (kinds=['marker', 'fault:ISR:MemManage', 'task_state:DEAD', 'context_switch'])
- **Symptom (mystery mode):** One of the three tasks stops; the other two keep going; the fault log shows a MemManage with an address just below a stack.

What to look for: A marker event "taskC: deep recursion begins", then a fault event from context C: "MemManage at PC=... (push {r4, r5, r6, lr}): write not permitted in stack guard 'guard_C'". The fault record shows MMFSR DACCVIOL and MMARVALID, MMFAR inside guard_C: an address inside the pushed range (on the M33 typically the lowest; the model stores in ascending order, so MMFAR = C's stack_base - 16 = 0x20000270), and sp=psp equal to C's stack_base: the push was abandoned, so SP did not move. Because PSP is already at the edge, the hardware cannot push the fault frame either, so a second fault event reports a stacking error (MSTKERR, CFSR=0x00000092); the kernel still identifies C from EXC_RETURN=0xFFFFFFFD (a task was running). Run "r2s fault P07" to decode both records. Then a task_state event C -> DEAD "kernel kills the task", a context_switch to the next task, and the alphabet continues. In the summary, C's watermark is 100 percent (every word overwritten) while A and B stay around 25 percent.

Likely root causes on real hardware:

- Unbounded recursion or a large local array in a task with a small stack.
- A task calling a library routine (printf, floating point) that needs far more stack than planned.
- Stack size computed in words but passed in bytes (or the reverse), making the real stack four times smaller than intended.

How to debug:

1. Read SCB->CFSR and SCB->MMFAR in the fault handler: DACCVIOL + MMARVALID with MMFAR in the guard just below a task's stack base (anywhere in the pushed range, typically its lowest address) is a stack overflow caught by the guard.
2. Check which stack the frame is on: EXC_RETURN bit 2 set (0xFFFFFFFD) means the task's PSP, so the faulting task is the current TCB.
3. Print the stack watermark of every task periodically; a task that climbs toward 100 percent is the next one to fault.
4. Give the task more stack or remove the recursion; keep the guard, it costs one MPU region per task.

What this means: Without protection a stack overflow silently corrupts whatever lies below the stack (see P09). An MPU guard region turns that silent corruption into a precise fault at the first bad write, with the address in MMFAR and the guilty task identifiable from PSP. The kernel can then contain the damage to that one task, which is the whole promise of memory protection.

### P08

**Context switch bug - PendSV forgets to save and restore r8** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P08))

The PendSV handler's register list is {r4-r7, r9-r11} instead of {r4-r11}. Nothing faults. Task A keeps a value in r8, stores what it expects, runs a delay loop (so a tick lands inside it), and compares r8 afterwards. Task B also uses r8 for its own counter. After the first switch A -> B -> A, task A finds B's r8 in its register and records a corruption. The scenario shows how a missing register in the software frame surfaces as data corruption far from the handler.

- **Program:** `kernel`
- **Injected:** `pendsv_forgets_register`: PendSV saves/restores an incomplete register list: tasks corrupt each other.
- **Expected:** No faults, tasks keep switching, but task A reports r8 corruptions (marker events) and the "corruptions" global is non-zero.
- **Checks:** `no_faults`; `min_switches` (count=30); `global_gt` (name=corruptions, value=0); `min_events` (kind=marker, count=1); `event_sequence` (kinds=['context_switch:B', 'context_switch:A', 'marker'])
- **Symptom (mystery mode):** Tasks run, but one of them occasionally computes garbage or resets its counter; nothing in the fault log.

What to look for: The scenario event lists the injected fault. The kernel's PendSV source in this run uses stmdb/ldmia with a seven-register list; the context_switch events look normal (PSPs change, nothing faults). Then, soon after the first switch back to A, a marker event "taskA: r8 corrupted after a context switch". The global "corruptions" counts how often it happened; a_iters is reset each time. Compare with P06 where the same task set with a complete register list never reports a corruption. In the viewer, click the context_switch events: the saved frame on A's stack has only seven words before the hardware frame, so the PSP in the TCB is off by four relative to the correct layout.

Likely root causes on real hardware:

- A register missing from the stmdb/ldmia list in the PendSV handler (classic: r8 or r11, because the list was typed as r4-r7, r9-r11 or copied from an example that used a different set).
- The compiler uses a register the switch does not save (a kernel written in C with a naked handler and the wrong clobber list).
- Saving with one register order and restoring with another, so the values go back into the wrong registers.
- FPU context: if the FPU is enabled, s16-s31 also need saving (lazy stacking).

How to debug:

1. Write a GDB script that breaks in PendSV, prints the PSP and 16 words above it, and compares to the stack-layout diagram; count the words before the hardware frame (must be 8).
2. In each task, keep a known constant in r8-r11 (register variables) and check it after every delay; the first task to see a wrong value points at the register.
3. Review the handler's register list against the ABI table: r4-r11 are callee-saved and must be in the software frame, r0-r3/r12/lr/pc/xPSR are already in the hardware frame.
4. Run two tasks with different values in the suspect register and dump both TCB frames: the missing word shifts everything below it.

What this means: A context switch must preserve every register the ABI calls callee-saved, because the task was interrupted at an arbitrary instruction and expects all of them intact. Missing one does not fault; it leaks one task's value into another, intermittently, only when a tick happens while the register is live. That is why the course asks for a stack-layout diagram before any switch code is written.

### P09

**Stack overflow without the MPU - watermark hits 100 percent, then corruption** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P09))

The same overflowing task C as P07, but the kernel never enables the MPU. C's stack grows past its base, through the unused guard bytes and into the neighbouring task stacks and globals. The watermark of C reaches 100 percent before anything goes wrong; then other tasks resume with corrupted frames, and eventually an exception return pops a garbage frame (INVPC UsageFault) or a push reaches unmapped memory (BusFault). Compare the fault list and the task states with P07.

- **Program:** `kernel`
- **Injected:** `task_overflows_stack`: Task C recurses without bound and runs off the bottom of its stack.
- **Injected:** `no_mpu`: The kernel never enables the MPU: a stack overflow silently corrupts the neighbour.
- **Expected:** No MemManage; instead a late BusFault/UsageFault chain, more than one task dead, C's watermark 100 percent and corrupted globals.
- **Checks:** `watermark_min_percent` (task=C, value=100); `min_events` (kind=fault, count=2); `task_state` (task=C, state=DEAD); `task_state` (task=A, state=DEAD); `global_gt` (name=corruptions, value=1)
- **Symptom (mystery mode):** Random tasks crash with UsageFault or BusFault at nonsense addresses; which one crashes changes with timing.

What to look for: The mpu event says "MPU left disabled". After the marker "deep recursion begins" there is a long gap with no fault: C's watermark climbs to 100 percent (visible in the watermarks attached to context_switch events) and C keeps writing below its stack base. Later faults appear in the wrong places: a BusFault from C when the stack finally reaches the bottom of SRAM, then UsageFault INVPC events from tasks A and B whose saved frames were overwritten (xPSR with a nonsense exception number, PC near 0). The globals a_iters and corruptions hold garbage like 0xDEAD-derived values. Several tasks end DEAD, versus exactly one in P07.

Likely root causes on real hardware:

- A task overflowed its stack and the system has no MPU guard, so the damage lands in whatever was linked below: another task's stack, the TCB array or globals.
- The symptom (a fault in task A) is far from the cause (task C), which is the signature of memory corruption.

How to debug:

1. Add stack watermarks: fill each stack with 0xA5A5A5A5 at creation and report the deepest overwritten word; a task at 100 percent is the culprit even if another task crashed.
2. Place a canary word below each stack and check it at every tick; the first broken canary names the overflowing task.
3. Enable the MPU with a guard region per task (P07) so the first bad write faults immediately with the address in MMFAR.
4. In the fault handler, print the stacked frame: a PC that is not inside any function and an xPSR with a wrong exception number means the frame itself was overwritten.

What this means: Memory corruption is not detected where it happens but where its victim runs. The watermark is the early-warning instrument: it climbs before the crash. The MPU is the containment instrument: it converts the first bad write into a precise fault. P07 and P09 are the same bug with and without that containment, which is why the course asks for both.

### P12

**SVC system call path - privilege round trip, an ignored cpsid and a forbidden SysTick write** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P12))

Tasks run unprivileged on the PSP. Task C prints "C" through "svc #3" (write): the core enters handler mode (privileged, MSP), the SVC handler reads the SVC number from the svc instruction just before the stacked PC, takes the argument from the stacked r0, writes the UART, puts the return value into the stacked r0 and returns to unprivileged thread mode. On its third iteration C tries two privileged things directly. "cpsid i" is silently ignored, exactly as on the real core (PRIMASK stays 0, no fault). The write to SYST_CSR is an unprivileged access to the System Control Space: a precise BusFault with BFAR = 0xE000E010. The kernel kills C; A and B continue.

- **Program:** `kernel`
- **Injected:** `task_privileged_op`: Task C executes a privileged instruction / touches SysTick from thread mode.
- **Expected:** Several svc write calls succeed with a privilege round trip each; cpsid in task C is ignored; the SysTick write is a precise BusFault (BFAR=0xE000E010); C is DEAD, A and B keep running, no lockup.
- **Checks:** `min_events` (kind=syscall, count=6); `event_contains` (kind=privilege, text='cpsid' ignored); `fault_kind` (kind=BusFault); `fault_flag` (flag=PRECISERR); `metric_equals` (metric=fault_registers.BFAR, value=0xE000E010); `fault_reason_contains` (text=unprivileged access to PPB); `task_state` (task=C, state=DEAD); `task_alive` (task=B); `lockup` (value=False); `global_equals` (name=svc_errors, value=0); `uart_contains` (text=C); `event_sequence` (kinds=['syscall', 'exception_entry:ISR:SVCall', 'syscall:kernel:SVCall', 'exception_return', 'marker', 'privilege', 'fault:ISR:BusFault', 'task_state:DEAD'])
- **Symptom (mystery mode):** A task that tries to disable interrupts has no effect at all; the same task dies with a BusFault when it touches SysTick.

What to look for: For each system call: a cpu syscall event "svc #3 from unprivileged thread (r0=0x00000043)", an exception_entry SVCall whose stacked frame holds the argument in r0 and the return address in pc, a kernel syscall event that says where the SVC number came from (stacked PC - 4) and that the return value 1 was written to the stacked r0, and an exception_return "back to thread mode (unprivileged)". Then a marker "taskC: trying a privileged operation directly", a privilege event "'cpsid' ignored" (no fault, PRIMASK unchanged), and a fault event: BusFault PRECISERR at the "str r1, [r0]" instruction, privileged=false, context C, BFAR=0xE000E010 (SYST_CSR). The task_state event says C is DEAD and a context_switch moves on. The kernel event at scheduler start (CONTROL=3) is where the privilege drop happened.

Likely root causes on real hardware:

- A task calls a kernel function directly instead of through SVC: with CONTROL.nPRIV set, NVIC/SysTick/SCB/MPU registers give a BusFault and cpsid/cpsie/msr are ignored.
- The SVC handler reads arguments from the live r0-r3 instead of the stacked frame; and the return value must be written to the stacked r0 (see P17).
- The handler picks the wrong stack (MSP vs PSP) when locating the frame; test EXC_RETURN bit 2 (tst lr, #4).
- The SVC number is read from the wrong place: it is the immediate of the svc instruction at (stacked PC - 2) for the 16-bit Thumb encoding, not a register.

How to debug:

1. Break in the SVC handler: 'p/x $lr' (EXC_RETURN), then 'x/8xw $psp'. r0 must be the argument and the stacked PC must point just after the svc instruction ('x/i <stacked pc> - 2').
2. Read CONTROL from the task ('mrs r0, control' or 'p $control' in GDB): bit 0 = 1 means unprivileged, bit 1 = 1 means PSP.
3. Put cpsid i in a task on real hardware and read PRIMASK afterwards ('p $primask'): it stays 0, proving the instruction was ignored.
4. Decode the BusFault: CFSR (0xE000ED28) shows PRECISERR+BFARVALID, BFAR (0xE000ED38) = 0xE000E010, and the stacked PC is the store that tried to write SysTick.

What this means: SVC is the only door from an unprivileged task into the kernel: the hardware raises the privilege on entry and drops it again on return, and arguments and results travel through the stacked frame. Anything a task tries to do directly that needs privilege either fails loudly (BusFault on the System Control Space) or silently (cpsid ignored). Either way the kernel's state is protected, which is the reason tasks run unprivileged.

### P15

**PendSV priority not lowest - the context switch runs inside an interrupt handler** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P15))

The kernel from P06 plus a long button interrupt handler (priority 0xC0, less urgent than SysTick at 0x80). The handler counts to 3000 in r4 with a shadow copy in r5 and checks that they still agree at the end. SysTick ticks while the handler runs and pends PendSV. In the "correct" variant PendSV has the lowest priority (0xFF), so it waits until the button handler has returned and then switches tasks cleanly. In "pendsv_high" PendSV was given 0x80, the same as SysTick: it pre-empts the button handler, saves the handler's registers as if they were the task's, loads another task's registers and returns into the handler. The handler continues with another task's r4/r5, and the interrupted tasks resume with mixed-up registers.

- **Program:** `kernel`
- **Injected:** `pendsv_priority_not_lowest` (variant `pendsv_high`): PendSV is given a priority above other handlers: the switch can run inside an ISR.
- **Expected:** With PendSV lowest, the button handler always finishes intact and every switch happens with EXC_RETURN 0xFFFFFFFD; with PendSV at 0x80, switches run on top of the handler (EXC_RETURN 0xFFFFFFF1) and the handler detects corrupted registers.
- **Checks:** `no_faults` [correct]; `global_equals` (name=isr_corruptions, value=0) [correct]; `global_gt` (name=isr_corruptions, value=0) [pendsv_high]; `event_contains` (kind=context_switch, text=on top of another handler) [pendsv_high]; `event_contains` (kind=kernel, text=NOT the lowest priority) [pendsv_high]; `min_events` (kind=exception_entry, to=ISR:IRQ_GPIO, count=2) [correct]
- **Symptom (mystery mode):** Under button presses, a long interrupt handler occasionally computes a wrong result and a task's counter jumps; with no button presses everything is fine.

What to look for: Both variants: two button presses, each an exception_entry for IRQ_GPIO from a task, and SysTick ticks that pre-empt the long handler (exception entries with EXC_RETURN 0xFFFFFFF1, i.e. "I interrupted a handler"). In "correct" the irq_pend PendSV stays pending until the button handler's exception_return, then PendSV tail-chains with LR=0xFFFFFFFD and the switch is clean. In "pendsv_high" the kernel event at init already warns that PendSV is not the lowest priority; then the SysTick return tail-chains straight into PendSV with LR=0xFFFFFFF1 and the context_switch event warns "running ... on top of another handler". A marker "GPIO handler: r4/r5 changed under the handler" follows, and isr_corruptions counts it. Click the context_switch in the viewer and compare the saved r4/r5 of the switched-out task with the handler's loop counter.

Likely root causes on real hardware:

- PendSV priority left at its reset value 0 (the most urgent configurable level) because SHPR3 was never written.
- SHPR3 written as a whole word with the wrong byte order, so PendSV got SysTick's priority or vice versa.
- All system exceptions set to one 'kernel priority' for convenience, so PendSV ties with or beats device interrupts.
- A kernel that triggers the switch directly from SysTick or an ISR instead of pending PendSV.

How to debug:

1. Read SHPR3 in GDB: 'x/xw 0xE000ED20'. Byte 2 (PendSV) must be the numerically largest priority in the system (0xF0 with 4 priority bits).
2. In PendSV_Handler assert that LR == 0xFFFFFFFD (or check EXC_RETURN bit 3 = thread mode); halt if not.
3. Put a marker pin in the long ISR and another in PendSV: on the scope a PendSV pulse inside the ISR pulse is the bug.
4. In the ISR, keep a shadow copy of a callee-saved register as here and check it at the end; corruption that appears only under interrupt load points at the switch.

What this means: PendSV is lowest priority by design: it makes the switch wait until every other handler has finished, so the only thing it ever saves is a task. Give it a higher priority and the kernel will one day save an interrupt handler's registers into a task's context. Nothing faults; registers just move between contexts, which is the hardest kind of bug to find without knowing this rule.

### P17

**SVC returns the wrong value - result written to the live r0, not the stacked r0** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P17))

The kernel from P06 with one bug in the SVC handler: after handling write(), it puts the return value 1 into the register r0 instead of into the stacked r0 of the caller's exception frame. On exception return the hardware pops the frame and overwrites r0 with the stacked value, which is still the argument (the character code). Task B checks the return value of every write() and counts the failures. The character is still printed, so the system looks healthy from the outside.

- **Program:** `kernel`
- **Injected:** `svc_return_in_live_r0`: The SVC handler writes its result to r0 instead of the stacked r0: the caller never sees it.
- **Expected:** No faults, the alphabet is printed, but every write() returns its argument instead of 1, so svc_errors climbs and task B reports it.
- **Checks:** `no_faults`; `uart_contains` (text=ABCDEFGHIJKLMNOPQRSTUVWXYZ); `global_gt` (name=svc_errors, value=10); `event_contains` (kind=syscall, text=LIVE r0); `event_contains` (kind=marker, text=write() returned a wrong value)
- **Symptom (mystery mode):** Output appears normally, but every system call reports failure to its caller.

What to look for: Each kernel syscall event for write says "return value 1 written to the LIVE r0 (lost ...)". The following exception_return pops the frame: its popped_frame.r0 is the character (0x41, 0x42 ...), and in the viewer's register panel r0 after the return is that character, not 1. Task B's marker "write() returned a wrong value" follows every call and the global svc_errors grows. Compare with P06, where svc_errors stays 0 and the syscall event says "written to the stacked r0 at 0x2000....".

Likely root causes on real hardware:

- The SVC handler (C function) returns its result normally, and the assembly wrapper never stores r0 into the frame.
- The handler writes the frame through the wrong stack pointer (MSP instead of PSP), so it updates some other memory and the caller's frame keeps the argument.
- The frame pointer passed to the C dispatcher is computed before the hardware frame (off by 32 bytes), so 'frame[0] = result' lands in the wrong word.

How to debug:

1. Break at the end of the SVC handler: 'x/8xw $psp' and check word 0 (stacked r0) holds the result before 'bx lr'.
2. Step over the exception return ('stepi' on bx lr) and 'p/x $r0' in the task: it equals the stacked word, whatever the handler left in the live r0.
3. Write a test system call that returns a constant (e.g. 0x1234) and check it from a task.

What this means: An exception return restores r0-r3, r12, lr, pc and xPSR from the stack, so for the caller of svc the stacked frame IS the register file. Arguments are read from it and results must be written into it. Anything written to the live registers is overwritten by the hardware on the way out.

## Experiments

### P10

**Flash vs SRAM - the same loop timed with the cycle counter, with and without the XIP cache** ([open in the viewer](https://fakhter-uvm014.github.io/reset-to-scheduler-sim/#P10))

A 16-instruction checksum loop runs 200 iterations. main first reads DWT CYCCNT twice back to back to measure what a measurement costs, then reads it before and after the call and prints both numbers. Three variants place the loop in flash (through the XIP cache), in flash with the cache disabled (every fetch pays the miss penalty), and in SRAM. The data table is always in SRAM, so only instruction fetches move between regions.

- **Program:** `bench`
- **Expected:** sram is fastest, flash_cached is within a few percent of it (only cold misses), flash_uncached is several times slower; the instruction count is identical in all three.
- **Checks:** `no_faults` [sram]; `uart_contains` (text=bench cycles=) [sram]; `variant_lt` (metric=bench.bench_cycles, a=sram, b=flash_cached); `variant_lt` (metric=bench.bench_cycles, a=flash_cached, b=flash_uncached); `variant_gt` (metric=bench.xip_misses, a=flash_uncached, b=flash_cached)
- **Symptom (mystery mode):** The same function runs faster when moved to RAM, but only a little, unless the cache is off.

What to look for: summary.variants.<name>.bench: bench_cycles printed by the program and xip_hits/xip_misses. flash_cached has a handful of misses (the loop fits in a few cache lines; the first pass misses, the other 199 hit), so its bench_cycles is only slightly above sram. flash_uncached has a miss per fetch and bench_cycles several times higher. instret and static_code_bytes are identical in all three: the program is the same, only where the fetch comes from changes. The UART lines "bench cycles=" and "overhead=" are what the student prints on the board; bench_cycles_corrected subtracts the overhead (3 cycles here: one load from the PPB).

Likely root causes on real hardware:

- Code in flash running slower than expected: the loop is larger than the XIP cache, or the cache was disabled, or the flash clock divider is high.
- SRAM copy not faster than flash: the loop already fits in the cache, so the difference is only the cold misses; or the function was not really placed in SRAM (check the map file).
- Timing noise: an interrupt fired during the measurement; measure with interrupts off or repeat and take the minimum.

How to debug:

1. Use objdump or the map file to confirm the function's address: 0x10xxxxxx is flash, 0x20xxxxxx is SRAM (use __attribute__((section(".ram_code"))) or the linker script).
2. Read DWT CYCCNT before and after; measure an empty call first and subtract that overhead.
3. Disable the XIP cache (RP2350 XIP_CTRL) and repeat: the slowdown is the cache's contribution.
4. Vary the loop size until it exceeds the cache; the flash number jumps while the SRAM number stays linear.

What this means: Instruction fetch cost depends on where the code lives and whether a cache is hiding the slow memory. A small hot loop is almost free from flash because the cache hits after the first pass; a large or scattered program is not. Cycle counts, not milliseconds, make this visible, and the same instruction count with different cycle counts is the definition of a memory-hierarchy effect.
