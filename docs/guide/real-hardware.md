# Checking against real hardware

The simulator teaches the order of events and what to look for. This page
shows how to see the same things on a Raspberry Pi Pico 2 with a debugger,
in QEMU when no board is available, and what differs. Tool versions change;
when a command below does not match your installation, the tool's own
documentation wins.

## Kit

- Raspberry Pi Pico 2 (RP2350: two Cortex-M33 cores and two Hazard3 RISC-V
  cores; the model follows one Cortex-M33).
- Raspberry Pi Debug Probe (CMSIS-DAP over SWD, plus a UART bridge). Wire
  the probe's SWD cable to the Pico 2's 3-pin debug header and its UART
  pins to GP0 (TX) / GP1 (RX).
- Toolchain: Arm GNU Toolchain (`arm-none-eabi-gcc`, `-gdb` or
  `gdb-multiarch`), OpenOCD with RP2350 support, `picotool`, optionally VS
  Code with the Cortex-Debug or Raspberry Pi Pico extension.

## Boot metadata on the RP2350

The RP2350 boot ROM does not jump blindly into flash. It looks for a small
**IMAGE_DEF metadata block** near the start of the image (Raspberry Pi
documents where it must be and what it contains; the SDK ships it as an
assembly include). Without it the boot ROM does not start your code, which
looks exactly like "never reaches main". When the block is found, the boot
ROM takes the initial SP and the reset handler from your vector table and
starts it. The simulator leaves room for the block and skips the boot ROM.

Practical consequences:

- Copy the metadata block from the course starter code; write everything
  else (vector table, reset handler, linker script) yourself.
- Measure startup time from the first instruction of `Reset_Handler`, not
  from power-on: the boot ROM runs first and takes much longer than your
  `.data`/`.bss` loops.

## Connecting the debugger

Start OpenOCD (the Raspberry Pi documentation gives the current form;
typically):

```
openocd -f interface/cmsis-dap.cfg -f target/rp2350.cfg -c "adapter speed 5000"
```

Then GDB in a second terminal:

```
arm-none-eabi-gdb build/app.elf          # or: gdb-multiarch build/app.elf
(gdb) target extended-remote :3333
(gdb) monitor reset halt
(gdb) load                                # program the flash
(gdb) monitor reset halt
(gdb) info registers                      # r0-r12, sp, lr, pc, xPSR, msp, psp, primask, control
```

If your OpenOCD build lacks `target/rp2350.cfg`, use the build from
Raspberry Pi (the Pico setup installers and the VS Code extension include
one) or a newer upstream release.

## Seeing each layer on the board

### Boot (P01, P02, P03, P13, P14)

```
(gdb) monitor reset halt        # the RP2350 halts in the boot ROM, not in your code
(gdb) hbreak Reset_Handler      # hardware breakpoint: flash code cannot be patched
(gdb) continue                  # the boot ROM finds your image and starts it
(gdb) x/2xw 0x10000000          # word 0 = initial MSP, word 1 = reset handler | 1
(gdb) p/x $sp                   # must equal word 0
(gdb) p/x $pc                   # must equal word 1 & ~1 (bit 0 cleared)
(gdb) break main
(gdb) continue
(gdb) x/4wx &__data_start       # equal to the flash copy?
(gdb) x/4wx &__data_load
(gdb) x/4wx &__bss_start        # all zero?
```

Right after `monitor reset halt` the core is inside the boot ROM, so SP and
PC there say nothing about your image. The boot ROM loads MSP and PC from
your vector table when it launches the image; at the first instruction of
`Reset_Handler` they must match words 0 and 1. If the breakpoint is never
hit, the boot ROM did not accept the image (check the IMAGE_DEF block).

### Exceptions (P04, P05, P18)

```
(gdb) x/xw 0xE000E100           # NVIC_ISER0: enabled IRQs (bit n = IRQ n)
(gdb) x/xw 0xE000E200           # NVIC_ISPR0: pending IRQs
(gdb) x/4xb 0xE000E414          # NVIC_IPR5 bytes: IRQ 20..23 priorities (IRQ 21 = IO_IRQ_BANK0)
(gdb) x/xw 0xE000ED20           # SHPR3: byte 2 PendSV, byte 3 SysTick
(gdb) break *GPIO_Handler        # '*' = the first instruction, before any prologue moves SP
(gdb) continue                  # press the button
(gdb) p/x $lr                   # EXC_RETURN: bit 2 says which stack holds the frame
(gdb) p/x $xpsr                 # low 9 bits = IPSR = 16 + IRQ number
(gdb) x/8xw $psp                # if LR bit 2 is set (0xFFFFFFFD): frame on the PSP
(gdb) x/8xw $msp                # otherwise (0xFFFFFFF1 / 0xFFFFFFF9): frame on the MSP
                                # either way: r0 r1 r2 r3 r12 lr pc xPSR
```

### Faults (P03, P07, P12, P14)

In your fault handler, or in GDB once it stops there:

```
(gdb) x/xw 0xE000ED28           # CFSR  (MMFSR 7:0, BFSR 15:8, UFSR 31:16)
(gdb) x/xw 0xE000ED2C           # HFSR  (bit 30 FORCED, bit 1 VECTTBL)
(gdb) x/xw 0xE000ED34           # MMFAR (valid if CFSR bit 7)
(gdb) x/xw 0xE000ED38           # BFAR  (valid if CFSR bit 15)
(gdb) x/xw 0xE000ED24           # SHCSR: bits 16/17/18 enable MemManage/BusFault/UsageFault
(gdb) p/x $lr                   # EXC_RETURN: bit 2 = 1 -> frame on PSP
(gdb) x/8xw $psp                # or $msp: stacked r0 r1 r2 r3 r12 lr pc xPSR
(gdb) info symbol 0x...         # the stacked PC -> function name
(gdb) x/i 0x...                 # and the faulting instruction
```

`r2s fault <ID>` prints the same decoding for the simulated runs.

A **lockup** shows as the core stopped with `PC=0xEFFFFFFE` on ARMv8-M
(`0xFFFFFFFE` on ARMv7-M parts) and the S_LOCKUP flag in DHCSR
(`x/xw 0xE000EDF0`, bit 19). Look at SP and the vector table.

### Kernel (P06-P09, P12, P15, P17)

```
(gdb) break PendSV_Handler
(gdb) continue
(gdb) p/x $psp                  # the outgoing task's stack
(gdb) stepi 2                   # past mrs + stmdb
(gdb) x/16xw $r0                # r4..r11, then r0 r1 r2 r3 r12 lr pc xPSR
(gdb) p $control                # in the task: 3 = unprivileged + PSP
```

A small GDB script that prints every task's saved frame from your TCB array
is worth writing early; the layout must match the stack diagram in
[how-it-works.md](how-it-works.md).

## Cycle counter (DWT CYCCNT)

The counter must be enabled before it counts:

```c
#define DEMCR      (*(volatile uint32_t *)0xE000EDFC)
#define DWT_CTRL   (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT (*(volatile uint32_t *)0xE0001004)

DEMCR |= 1u << 24;      /* TRCENA: enable the DWT */
DWT_CYCCNT = 0;
DWT_CTRL |= 1u;         /* CYCCNTENA */

uint32_t t0 = DWT_CYCCNT;
uint32_t t1 = DWT_CYCCNT;
uint32_t overhead = t1 - t0;    /* subtract this from every measurement */
```

From GDB: `set *(unsigned *)0xE000EDFC |= 1<<24`,
`set *(unsigned *)0xE0001000 |= 1`, then `x/wx 0xE0001004`. Some
Cortex-M parts also need a DWT lock register unlocked first; check your
core's documentation if the counter stays at 0. The counter is 32 bits and
wraps after about 28.6 s at 150 MHz; use unsigned subtraction.

On the Hazard3 (RISC-V) cores the equivalents are the `mcycle` and
`minstret` CSRs (`csrr a0, mcycle`), which may need enabling through
`mcountinhibit`; see the Hazard3 documentation.

Cross-check every key number with a GPIO marker on the oscilloscope:
set a pin at the start of the measured code and clear it at the end, then
compare the pulse width with `cycles / f_clk`. The difference is the
measurement overhead plus the GPIO path.

## Mapping simulator numbers to the board

| Simulator | On the board |
|---|---|
| `t_cycles` in the trace | DWT CYCCNT (after enabling it) |
| `exception_entry` 12 cycles | about 12 cycles with zero-wait-state memory; more on a flash cache miss |
| XIP miss penalty 20 cycles | depends on the QSPI clock divider and flash; measure it (P10 method) |
| `kcall` costs (scheduler, tick) | your C code: measure it with CYCCNT |
| GPIO marker event | a scope edge, delayed by the GPIO path |
| 2000-cycle kernel tick | typically 1 ms (150 000 cycles at 150 MHz) |

## No board: QEMU

QEMU can run phases 1-5 with GDB attached in the same way, using an Arm
Cortex-M33 board model such as `mps2-an505`:

```
qemu-system-arm -M mps2-an505 -cpu cortex-m33 -nographic -kernel build/app.elf -s -S
arm-none-eabi-gdb build/app.elf -ex "target remote :1234"
```

Differences to plan for:

- The memory map and peripherals are those of the Arm MPS2 board, not the
  RP2350: the linker script (flash/SRAM addresses) and the UART driver must
  be adapted. Check QEMU's documentation for the board and the Arm
  application note it implements.
- No IMAGE_DEF block is needed; QEMU loads the ELF and starts at the
  vector table.
- QEMU does not model instruction timing. Instruction and cycle counts (DWT support is
  limited) are not meaningful, so the flash vs SRAM and ARM vs RISC-V
  experiments need the real board.
- Fault registers, exception frames, EXC_RETURN, PendSV, SVC and the MPU
  behave architecturally, so the fault decoder and the kernel can be
  developed in QEMU.

## Windows notes

- Install the Arm GNU Toolchain (Windows installer), and use the Raspberry
  Pi Pico setup for Windows or the VS Code Pico extension to get OpenOCD and
  picotool with RP2350 support. The Debug Probe is a standard CMSIS-DAP v2
  device; on current Windows versions it normally needs no extra driver.
- In PowerShell, run OpenOCD and GDB in two terminals exactly as above.
  Paths with spaces need quotes: `-f "C:\path to\rp2350.cfg"`.
- The simulator itself runs the same on Windows: inside the activated
  `.venv`, use `python -m r2s run P06` (or `r2s run P06`). If
  activating a venv is blocked, run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
- The serial console from the Debug Probe appears as a COM port; any
  terminal (PuTTY, the VS Code serial monitor) at the baud rate your UART
  driver uses will show the output.
