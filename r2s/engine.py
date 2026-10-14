"""The machine: wires memory, CPU, NVIC, SysTick, timer, GPIO/UART and MPU
together, loads the image, performs reset and runs the main loop.

Time model
----------
There is one clock: the CPU cycle counter. After each instruction the
machine advances SysTick and the 1 MHz timer by the cycles the instruction
consumed, and fires any external stimulus (button press) scheduled for a
cycle that has now passed. ``wfi`` skips the clock forward to the next
event, so an idle system does not cost simulation time.

Run limits
----------
A run ends when the CPU halts (``halt`` instruction, lockup, or a kernel
decision), or when ``max_cycles`` / ``max_instructions`` is reached, or at
``stop_at_cycle`` (used by ``r2s regs --at-cycle``). The summary records
which one happened.

Reset
-----
``power_on`` does what the hardware does and nothing more: program the
image, leave SRAM with undefined (seeded pseudo-random) contents, read
word 0 of the vector table into MSP and word 1 into PC (bit 0 into xPSR.T),
then start fetching. A bad reset vector is *not* special-cased: the first
fetch or the first instruction faults exactly as it would on the board.

Author: Farzana Akhter
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import memory as mm
from .boot import BootMonitor, Image, fill_sram_with_garbage, link, write_image
from .cpu import Cpu
from .isa_profiles import IsaProfile, get_profile
from .memory import Memory, XipCache
from .mpu import Mpu
from .nvic import EXC_RESET, Nvic, exc_name
from .peripherals import Gpio, Uart
from .systick import SysTick
from .timer import SystemTimer
from .trace import Trace


@dataclass
class Stimulus:
    at_cycles: int
    action: str               # "button_press" | "button_release" | "halt" | "custom"
    details: Dict[str, Any] = field(default_factory=dict)
    fn: Optional[Callable[["Machine"], None]] = None
    done: bool = False


@dataclass
class MachineConfig:
    cpu_hz: int = 150_000_000
    profile: str = "arm_like"
    seed: int = 1
    max_cycles: int = 2_000_000
    max_instructions: int = 200_000
    xip_cache_enabled: bool = True
    xip_miss_penalty: int = 20
    garbage_sram: bool = True
    garbage_words: int = 256   # how many words after .data/.bss start get garbage at power-on
    variant: Optional[str] = None


class Machine:
    def __init__(self, image: Image, config: Optional[MachineConfig] = None,
                 faults: Optional[List[Dict[str, Any]]] = None) -> None:
        self.config = config or MachineConfig()
        self.faults = faults or []
        self.rng = random.Random(self.config.seed)
        self.image = link(image)
        self.profile: IsaProfile = get_profile(self.config.profile)
        self.memory = Memory(XipCache(miss_penalty=self.config.xip_miss_penalty,
                                      enabled=self.config.xip_cache_enabled))
        self.nvic = Nvic()
        self.cpu = Cpu(self.memory, self.nvic, None, self.profile, self.image.symbols)
        self.trace = Trace(lambda: self.cpu.cycles, self.config.cpu_hz)
        self.trace.variant = self.config.variant
        self.trace.state_fn = self._trace_state
        self.cpu.trace = self.trace
        self.cpu.machine = self
        self.nvic.trace = self.trace
        self.nvic.clock = lambda: self.cpu.cycles
        self.nvic.attach(self.memory)
        self.systick = SysTick(self.nvic)
        self.systick.attach(self.memory)
        self.timer = SystemTimer(self.nvic, lambda: self.cpu.cycles, self.config.cpu_hz // 1_000_000)
        self.timer.attach(self.memory)
        self.gpio = Gpio(self.nvic, self.trace)
        self.gpio.attach(self.memory)
        self.uart = Uart(self.trace)
        self.uart.attach(self.memory)
        self.mpu = Mpu(self.trace)
        self.mpu.attach(self.memory)
        self.boot = BootMonitor(self)
        self.stimuli: List[Stimulus] = []
        self.kernel: Any = None
        self.stop_reason = ""
        self.phase = "boot"
        self.instructions = 0
        self.cpu.kcalls["default_handler"] = self._default_handler
        self.cpu.kcalls["lib:print_r0"] = self._print_r0
        self.cpu.kcalls["lib:print_hex_r0"] = self._print_hex_r0
        self.cpu.kcalls["fault:print"] = self._fault_print
        self.extra_summary: Dict[str, Any] = {}
        self.stop_at_cycle: Optional[int] = None

    def _trace_state(self):
        cpu = self.cpu
        regs = {k: f"0x{v:08X}" for k, v in cpu.regs.items()}
        regs.update({"msp": f"0x{cpu.msp:08X}", "psp": f"0x{cpu.psp:08X}", "lr": f"0x{cpu.lr:08X}",
                     "pc": f"0x{cpu.pc:08X}", "xpsr": f"0x{cpu.xpsr:08X}", "control": cpu.control,
                     "primask": int(self.nvic.primask)})
        return cpu.context(), regs

    # ----------------------------------------------------------------- setup
    def add_stimulus(self, stimulus: Stimulus) -> None:
        self.stimuli.append(stimulus)
        self.stimuli.sort(key=lambda s: s.at_cycles)

    def context(self) -> str:
        return self.cpu.context()

    def default_context(self) -> str:
        """Context name without a kernel: boot, main, or the active handler."""
        if self.cpu.in_handler:
            return f"ISR:{exc_name(self.cpu.ipsr)}"
        return self.phase

    # ----------------------------------------------------------------- reset
    def power_on(self) -> None:
        """Power-on + reset: program flash, randomise SRAM, read the vector table."""
        if self.config.garbage_sram:
            fill_sram_with_garbage(self.memory, self.rng, mm.SRAM_BASE,
                                   mm.SRAM_BASE + 4 * self.config.garbage_words)
        info = write_image(self.memory, self.image, self.faults)
        if self.kernel is None:
            self.phase = "boot"
            self.cpu.context_fn = self.default_context
        self.boot.step("power_on", image_sections=[s.name for s in self.image.sections],
                       data_words=len(self.image.data), bss_words=len(self.image.bss))
        # The core reads the two first words of the table at VTOR.
        vector_base = info["vector_base"]
        self.cpu.vtor = vector_base
        initial_sp = self.memory.peek(vector_base) or 0
        reset_vector = self.memory.peek(vector_base + 4) or 0
        if not isinstance(initial_sp, int):
            initial_sp = 0
        if not isinstance(reset_vector, int):
            reset_vector = 0
        self.boot.step("vector_table", vector_base=vector_base, initial_sp=initial_sp,
                       reset_vector=reset_vector)
        self.cpu.msp = initial_sp & ~3          # MSP bits [1:0] are always zero
        self.cpu.pc = reset_vector & ~1
        self.cpu.thumb = bool(reset_vector & 1)  # EPSR.T comes from bit 0 of the reset vector
        self.cpu.lr = 0xFFFFFFFF                 # LR is "unknown" at reset; the model uses all ones
        self.cpu.ipsr = 0
        self._main_hook()

    def _main_hook(self) -> None:
        boot = self.boot
        original = self.cpu.kcalls["boot:main_entered"]

        def main_entered(cpu) -> None:
            original(cpu)
            self.phase = "main"
        self.cpu.kcalls["boot:main_entered"] = main_entered
        del boot

    # ------------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        cfg = self.config
        cpu = self.cpu
        while not cpu.halted:
            if cpu.cycles >= cfg.max_cycles:
                self.stop_reason = f"max_cycles ({cfg.max_cycles}) reached"
                break
            if self.instructions >= cfg.max_instructions:
                self.stop_reason = f"max_instructions ({cfg.max_instructions}) reached"
                break
            if self.stop_at_cycle is not None and cpu.cycles >= self.stop_at_cycle:
                self.stop_reason = f"stopped at cycle {cpu.cycles} (requested {self.stop_at_cycle})"
                break
            before = cpu.cycles
            cpu.step()
            self.instructions += 1
            self._advance(cpu.cycles - before)
            if self.kernel is not None and self.kernel.stop_requested:
                self.stop_reason = self.kernel.stop_reason
                break
        if cpu.halted and not self.stop_reason:
            self.stop_reason = cpu.halt_reason
        self.uart.flush()
        return self.summary()

    def _advance(self, cycles: int) -> None:
        if cycles > 0:
            self.systick.advance(cycles)
        self.timer.advance()
        now = self.cpu.cycles
        for stim in self.stimuli:
            if stim.done or stim.at_cycles > now:
                continue
            stim.done = True
            if stim.action == "button_press":
                self.gpio.press(stim.details.get("pin", 14), nominal=stim.at_cycles)
            elif stim.action == "button_release":
                self.gpio.release(stim.details.get("pin", 14))
            elif stim.action == "halt":
                self.cpu.halted = True
                self.cpu.halt_reason = stim.details.get("reason", "stimulus: halt")
            elif stim.fn is not None:
                stim.fn(self)

    def sleep_until_event(self) -> int:
        """``wfi``: skip ahead to the next SysTick wrap, timer alarm or stimulus."""
        if self.nvic.pending:
            return 0
        candidates = [self.systick.cycles_until_tick(), self.timer.cycles_until_alarm()]
        for stim in self.stimuli:
            if not stim.done:
                candidates.append(max(1, stim.at_cycles - self.cpu.cycles))
                break
        candidates.append(max(1, self.config.max_cycles - self.cpu.cycles))
        if self.stop_at_cycle is not None:
            candidates.append(max(1, self.stop_at_cycle - self.cpu.cycles))
        skip = max(1, min(candidates))
        self.trace.emit("cpu", "sleep", f"wfi: core sleeps {skip} cycles until the next event",
                        frm=self.context(), to="sleep", skip_cycles=skip)
        return skip

    # -------------------------------------------------------------- kcalls
    def _default_handler(self, cpu: Cpu) -> None:
        number = cpu.ipsr
        from .nvic import exc_name
        self.trace.emit("cpu", "kernel", f"Default_Handler reached for {exc_name(number)} (IPSR={number}): no "
                        f"handler was installed for this exception, so the vector table points at the catch-all "
                        f"loop; the core spins here forever", exception=number)
        cpu.halted = True
        cpu.halt_reason = f"spinning in Default_Handler for {exc_name(number)} (IPSR={number})"

    def _fault_print(self, cpu: Cpu) -> None:
        """What a student's fault handler prints: the status registers and the
        stacked frame, read exactly where the real handler reads them."""
        from .cpu import decode_cfsr
        frame_at = cpu.psp if cpu.lr & 4 else cpu.msp
        names = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")
        frame = {n: self.memory.peek(frame_at + 4 * i) for i, n in enumerate(names)}
        lines = [f"*** {exc_name(cpu.ipsr)} ***",
                 f"CFSR=0x{cpu.cfsr:08X} HFSR=0x{cpu.hfsr:08X} MMFAR=0x{cpu.mmfar:08X} BFAR=0x{cpu.bfar:08X}"]
        for bit in decode_cfsr(cpu.cfsr, cpu.hfsr):
            lines.append(f"  {bit['register']}.{bit['name']}")
        lines.append(f"EXC_RETURN=0x{cpu.lr:08X} frame@0x{frame_at:08X}")
        lines.append("PC=0x{pc:08X} LR=0x{lr:08X} xPSR=0x{xpsr:08X}".format(
            **{k: (v if isinstance(v, int) else 0) for k, v in frame.items()}))
        for line in lines:
            for ch in line + "\n":
                self.memory.write(mm.UART_DR, ord(ch))

    def _print_r0(self, cpu: Cpu) -> None:
        for ch in f"{cpu.regs['r0']}\n":
            self.memory.write(mm.UART_DR, ord(ch))

    def _print_hex_r0(self, cpu: Cpu) -> None:
        for ch in f"0x{cpu.regs['r0']:08X}\n":
            self.memory.write(mm.UART_DR, ord(ch))

    # ------------------------------------------------------------- summary
    def summary(self) -> Dict[str, Any]:
        cpu = self.cpu
        hz = self.config.cpu_hz
        summary: Dict[str, Any] = {
            "variant": self.config.variant,
            "profile": self.profile.name,
            "stop_reason": self.stop_reason,
            "halted": cpu.halted,
            "lockup": cpu.lockup,
            "cycles": cpu.cycles,
            "time_us": round(cpu.cycles * 1e6 / hz, 3),
            "instructions_executed": self.instructions,
            "instret": cpu.instret,
            "cpi": round(cpu.cycles / cpu.instret, 3) if cpu.instret else None,
            "exception_entries": cpu.exception_entries,
            "tail_chains": cpu.tail_chains,
            "wfi_sleeps": cpu.wfi_sleeps,
            "critical_sections": {"count": cpu.critical_sections, "longest_cycles": cpu.critical_max},
            "ignored_privileged_ops": cpu.ignored_privileged_ops,
            "fault_registers": {"CFSR": f"0x{cpu.cfsr:08X}", "HFSR": f"0x{cpu.hfsr:08X}",
                                "MMFAR": f"0x{cpu.mmfar:08X}", "BFAR": f"0x{cpu.bfar:08X}"},
            "boot_steps": self.boot.steps,
            "faults": [f.to_dict() for f in cpu.faults],
            "uart_output": self.uart.output,
            "uart_lines": list(self.uart.lines),
            "gpio_toggles": {str(k): v for k, v in sorted(self.gpio.toggles.items())},
            "xip_cache": {"hits": self.memory.xip_cache.hits, "misses": self.memory.xip_cache.misses,
                          "enabled": self.memory.xip_cache.enabled},
            "memory_accesses": dict(self.memory.access_counts),
            "cycles_by_context": dict(sorted(cpu.ctx_cycles.items())),
            "final_registers": cpu.snapshot(),
            "systick_ticks": self.systick.ticks,
            "globals": {sym: self.memory.peek(addr) for sym, addr in self.image.symbols.items()
                        if sym in self.image.data or sym in self.image.bss},
            "markers": [e.details.get("text", "") for e in self.trace.events if e.kind == "marker"][:50],
        }
        if self.kernel is not None:
            summary["kernel"] = self.kernel.stats()
        summary.update(self.extra_summary)
        return summary
