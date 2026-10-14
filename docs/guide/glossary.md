# Glossary

Terms used in the simulator, the traces and the course, in alphabetical order.

| Term | Meaning |
|---|---|
| **AAPCS** | Arm's procedure call standard: arguments in r0-r3, result in r0, r4-r11 callee-saved, LR holds the return address, stack 8-byte aligned at calls. |
| **BFAR** | BusFault Address Register (`0xE000ED38`): the faulting address of a precise data bus error, valid only when BFSR.BFARVALID is set. |
| **BFSR** | BusFault Status Register, bits 15:8 of CFSR: IBUSERR, PRECISERR, IMPRECISERR, UNSTKERR, STKERR, BFARVALID. |
| **Boot step** | One of the six milestones the simulator checks: power_on, vector_table, reset_handler, data_copied, bss_zeroed, main_entered. |
| **.bss** | Uninitialised (zero-initialised) globals. Nothing is stored in flash; the startup code must zero the range in SRAM. |
| **Callee-saved registers** | r4-r11: a function (or a context switch) must preserve them. The hardware frame does not contain them, so PendSV saves them in software. |
| **CFSR** | Configurable Fault Status Register (`0xE000ED28`) = UFSR:BFSR:MMFSR. Sticky; write 1s to clear. |
| **Context switch** | Saving one task's registers and stack pointer and restoring another's. Here: in PendSV, r4-r11 in software, the rest by the hardware frame. |
| **CONTROL** | Special register. Bit 0 nPRIV (thread mode unprivileged), bit 1 SPSEL (thread mode uses PSP). |
| **CPI** | Cycles per instruction = cycles / instructions retired. |
| **Critical section** | Code run with interrupts masked (`cpsid i` ... `cpsie i`, PRIMASK = 1). Its length bounds the interrupt latency. |
| **CYCCNT** | DWT cycle counter (`0xE0001004`). Enable with DEMCR.TRCENA and DWT_CTRL.CYCCNTENA. The trace's `t_cycles` plays its role. |
| **.data** | Initialised globals. Initial values live in flash (load address, `__data_load`); the startup code copies them to SRAM (run address, `__data_start`). |
| **Default_Handler** | Catch-all loop that every unused vector points to (usually through weak aliases). Ending up there means an exception without a handler. |
| **DWT** | Data Watchpoint and Trace unit; contains CYCCNT. |
| **Escalation** | A configurable fault becoming a HardFault (HFSR.FORCED) because its handler is disabled or its priority cannot pre-empt. |
| **Exception frame** | The 8 words (r0-r3, r12, LR, PC, xPSR) the core pushes on exception entry, plus an optional alignment pad word. |
| **EXC_RETURN** | Special value placed in LR on exception entry (`0xFFFFFFF1/F9/FD` here). Branching to it returns from the exception; bit 2 says which stack holds the frame. |
| **Execution priority** | The priority the core runs at: that of the most urgent active exception, or PRIMASK's 0, or "thread" when nothing is active. Only more urgent exceptions pre-empt. |
| **Guard region** | A small MPU region below a stack, so an overflow faults at its first bad write. The model makes it "no access"; on real Armv8-M (no such AP encoding) it is read-only privileged-only plus XN, which faults any unprivileged access and any write. PSPLIM/MSPLIM (STKOF) are the alternative. |
| **Handler mode** | The mode while an exception runs: always privileged, always on MSP. |
| **HardFault** | Exception 3, fixed priority -1. Takes escalated faults and faults that have no other handler. |
| **Hazard3** | The RISC-V (RV32IMAC-family) core design used for the RP2350's two RISC-V cores. |
| **HFSR** | HardFault Status Register (`0xE000ED2C`): FORCED (bit 30), VECTTBL (bit 1). |
| **IMAGE_DEF** | RP2350 metadata block the boot ROM looks for before it launches an image. |
| **instret / minstret** | Instructions retired. `minstret` is the RISC-V counter CSR; the simulator reports `instret` per profile. |
| **INVPC** | UFSR bit: invalid EXC_RETURN or a frame that fails the return integrity check. |
| **INVSTATE** | UFSR bit: executed with xPSR.T = 0 (an address without the Thumb bit was loaded into PC). |
| **IPSR** | xPSR bits 8:0: the number of the running exception, 0 in thread mode. IRQ n is 16 + n. |
| **Latency (interrupt)** | Time from the interrupt request to the first handler instruction (here: to the marker pin). Floor = entry cost; ceiling = longest masked region. |
| **Lockup** | The core stops after a fault it cannot handle (fault in HardFault, failed HardFault entry). PC reads `0xEFFFFFFE` on ARMv8-M. |
| **Memory map** | Which addresses are flash, SRAM, peripherals and system registers. |
| **MMFAR** | MemManage Fault Address Register (`0xE000ED34`), valid when MMFSR.MMARVALID is set. |
| **MMFSR** | MemManage Fault Status Register, bits 7:0 of CFSR: IACCVIOL, DACCVIOL, MUNSTKERR, MSTKERR, MMARVALID. |
| **MPU** | Memory Protection Unit: regions with access permissions per privilege level. ARMv8-M uses base/limit regions with 32-byte granularity; overlap is a fault. |
| **MSP / PSP** | Main and process stack pointers. Handlers and `main` use MSP; tasks use PSP (CONTROL.SPSEL = 1). |
| **NVIC** | Nested Vectored Interrupt Controller: enables, pends, prioritises interrupts. Registers ISER, ICER, ISPR, ICPR, IPR (byte-packed). |
| **PendSV** | Exception 14, pended by software (ICSR.PENDSVSET). Given the lowest priority so the context switch runs after every other handler. |
| **Pre-emption** | A more urgent exception interrupting a running handler; its EXC_RETURN is `0xFFFFFFF1`. |
| **PRIMASK** | When 1, masks every configurable exception (raises execution priority to 0). Set by `cpsid i`. |
| **PRIVDEFENA** | MPU control bit: privileged code may use the default memory map where no region matches. |
| **Privilege** | Unprivileged thread code cannot change PRIMASK/CONTROL (ignored) or touch the System Control Space (BusFault). |
| **Reset vector** | Word 1 of the vector table: the address of Reset_Handler with bit 0 set. |
| **SHCSR** | System Handler Control and State Register (`0xE000ED24`): enables MemManage, BusFault, UsageFault handlers (bits 16-18). |
| **SHPR1-3** | System Handler Priority Registers (`0xE000ED18`-`0xE000ED20`). PendSV is byte 2 and SysTick byte 3 of SHPR3. |
| **Stacking error** | A fault while pushing the exception frame (MSTKERR / STKERR): the stack pointer itself is invalid or in a protected area. |
| **SVC / SVCall** | `svc #n` raises exception 11: the controlled way for a task to enter the kernel. Arguments and results travel in the stacked frame. |
| **SysTick** | 24-bit down counter in every Cortex-M; period = RVR + 1 cycles; used as the kernel tick (exception 15). |
| **Tail-chaining** | Going from one handler straight to the next pending one without popping and re-pushing the frame. |
| **Thread mode** | Normal execution (no exception active): `main`, tasks. |
| **Thumb bit (T)** | xPSR bit 24; must be 1 on Cortex-M. Comes from bit 0 of vector entries and branch targets. |
| **UFSR** | UsageFault Status Register, bits 31:16 of CFSR: UNDEFINSTR, INVSTATE, INVPC, NOCP, STKOF, UNALIGNED, DIVBYZERO. |
| **Vector table** | Table at VTOR: word 0 initial MSP, then one handler address per exception. |
| **VTOR** | Vector Table Offset Register (`0xE000ED08`). |
| **Watermark** | Peak stack use found by filling the stack with a pattern (`0xA5A5A5A5`) and finding the deepest overwritten word. |
| **wfi** | Wait for interrupt: sleep until an exception is pending. |
| **XIP** | Execute in place: running code directly from external flash through a cache. |
| **xPSR** | Combined program status register: APSR flags (N Z C V), EPSR (T bit) and IPSR (exception number). |
