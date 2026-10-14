"""The tiny preemptive kernel: TCBs, task stacks, PendSV switch, SVC calls,
unprivileged tasks, MPU stack guards and statistics.

How the pieces map to what a student writes (weeks 7-9)
-------------------------------------------------------
* **TCB** (:class:`Task`): name, saved PSP, state, stack base/size, priority,
  sleep counter, run-time statistics.
* **Task stack layout** when a task is *not* running (grows down)::

        high  +-----------+
              |  xPSR     |  \\
              |  PC       |   |  hardware frame: pushed by the core at
              |  LR       |   |  exception entry (and popped on return)
              |  r12      |   |
              |  r3..r0   |  /
              +-----------+
              |  r11..r4  |  software frame: pushed by PendSV_Handler
        psp ->+-----------+  (saved in the TCB)
              |  ...      |  free stack, filled with the watermark pattern
        base  +-----------+
              |  guard    |  MPU guard region (optional; see below)

* **Starting** the first task: ``main`` calls ``svc #0``. The SVC handler
  builds the "pretend it was interrupted" frame on the task's stack (done
  at task creation), sets PSP, sets CONTROL (unprivileged + PSP) and
  returns with ``EXC_RETURN=0xFFFFFFFD`` so the core pops the frame and
  lands in the task's entry point.
* **Tick**: SysTick_Handler calls the scheduler bookkeeping and pends
  PendSV. Because PendSV has the lowest priority the switch happens only
  after every other handler has finished (and is tail-chained).
* **PendSV_Handler** (teaching-ISA source in :func:`pendsv_source`) saves
  r4-r11 on the old task's stack, asks the scheduler for the next task,
  restores r4-r11 from the new stack and returns.
* **SVC**: the handler finds the frame from EXC_RETURN bit 2 (1 = PSP),
  reads the SVC number from the ``svc`` instruction just before the stacked
  PC (``PC - 4`` in this model, ``PC - 2`` for the 16-bit Thumb ``svc`` on
  the board), reads the arguments from the stacked r0/r1 and writes the
  return value into the **stacked** r0, which the hardware restores into r0
  on return. Writing the live r0 instead is a classic bug (scenario P17).
* **MPU** (ARMv8-M style, no overlapping regions): flash read-only, the
  .data/.bss globals read-write, peripherals read-write, a guard below
  every task stack (modelled as "no access"; on real Armv8-M it would be
  read-only privileged-only plus XN, which faults any unprivileged access and
  any write while privileged reads still succeed; PSPLIM/MSPLIM stack limits
  are the alternative), and one region reprogrammed at every switch to
  cover exactly the running task's stack. Everything else in SRAM (other
  task stacks, kernel data) has no region, so an unprivileged task that
  touches it faults, while privileged kernel code uses the background map
  (PRIVDEFENA=1).
* **Faults**: MemManage/UsageFault/BusFault/HardFault handlers call
  ``kernel:fault``. If a task caused it, the task is killed and the
  scheduler switches to another one; if the kernel itself faulted, the
  system halts with the record printed.

System calls (``svc #n``)::

    0 start      start the scheduler (from main only)
    1 yield      give up the rest of the slice
    2 sleep      r0 = ticks to sleep
    3 write      r0 = character to print on the UART
    4 exit       terminate the calling task
    5 lock       r0 = mutex id (Master's scope)
    6 unlock     r0 = mutex id

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import memory as mm
from .boot import Image
from .cpu import EXC_RETURN_THREAD_PSP, FRAME_WORDS, XPSR_THUMB, Cpu
from .isa import Instruction
from .mpu import MpuRegion
from .nvic import (EXC_BUSFAULT, EXC_HARDFAULT, EXC_MEMMANAGE, EXC_PENDSV, EXC_SVCALL,
                   EXC_SYSTICK, EXC_USAGEFAULT, exc_name)

WATERMARK = 0xA5A5A5A5
SW_FRAME = ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11")
DEFAULT_STACK_WORDS = 64
GUARD_BYTES = 32
STATES = ("READY", "RUNNING", "BLOCKED", "DEAD")
SYSCALLS = {0: "start", 1: "yield", 2: "sleep", 3: "write", 4: "exit", 5: "lock", 6: "unlock"}


@dataclass
class Task:
    """Task control block."""

    name: str
    entry: str                    # label of the task's entry point
    stack_base: int = 0           # lowest address of the stack
    stack_words: int = DEFAULT_STACK_WORDS
    priority: int = 1             # lower = more urgent (priority scheduler only)
    psp: int = 0                  # saved process stack pointer
    state: str = "READY"
    sleep_ticks: int = 0
    run_cycles: int = 0
    switches_in: int = 0
    kill_reason: str = ""
    guard_base: int = 0
    arg: int = 0

    @property
    def stack_top(self) -> int:
        return self.stack_base + 4 * self.stack_words

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "state": self.state, "priority": self.priority,
                "stack_base": f"0x{self.stack_base:08X}", "stack_top": f"0x{self.stack_top:08X}",
                "stack_bytes": 4 * self.stack_words, "psp": f"0x{self.psp:08X}",
                "run_cycles": self.run_cycles, "switches_in": self.switches_in,
                "kill_reason": self.kill_reason}


def pendsv_source(save_regs: str = "r4-r11", restore_regs: Optional[str] = None) -> str:
    """PendSV_Handler in the teaching ISA. The register lists are parameters so
    that scenario P08 can inject the classic 'forgot r8' bug."""
    restore_regs = restore_regs or save_regs
    return f"""
PendSV_Handler:
    mrs   r0, psp               ; r0 = stack pointer of the task being switched out
    stmdb r0!, {{{save_regs}}}       ; software frame: callee-saved registers
    kcall "sched:switch"        ; current->psp = r0; pick next; r0 = next->psp
    ldmia r0!, {{{restore_regs}}}       ; restore the next task's callee-saved registers
    msr   psp, r0               ; PSP now points at the next task's hardware frame
    bx    lr                    ; EXC_RETURN 0xFFFFFFFD: pop the frame from PSP, thread mode
"""


KERNEL_HANDLERS_SOURCE = """
SysTick_Handler:
    kcall "kernel:tick"         ; bookkeeping; pends PendSV when a switch is due
    bx    lr

SVC_Handler:
    kcall "svc:dispatch"        ; reads the stacked frame for arguments, writes r0 back
    bx    lr

Fault_Handler:
    kcall "kernel:fault"        ; decode the record; kill the task or halt the system
    bx    lr
"""


def _parse_reglist(text: str) -> List[str]:
    """``"r4-r11"`` or ``"r4, r5, r6"`` -> ordered register names."""
    regs: List[str] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            regs.extend(f"r{i}" for i in range(int(lo[1:]), int(hi[1:]) + 1))
        elif part:
            regs.append(part)
    return sorted(set(regs), key=lambda r: int(r[1:]))


class Kernel:
    """Attaches to a machine: reserves SRAM, installs handlers and kcalls."""

    def __init__(self, image: Image, tasks: List[Dict[str, Any]], tick_cycles: int = 2000,
                 scheduler: str = "round_robin", use_mpu: bool = True, unprivileged: bool = True,
                 pendsv_save: str = "r4-r11", pendsv_restore: Optional[str] = None,
                 stack_words: int = DEFAULT_STACK_WORDS, idle: bool = True,
                 stop_after_switches: int = 0, stop_after_ticks: int = 0,
                 pendsv_priority: int = 0xFF, systick_priority: int = 0x80,
                 svc_priority: int = 0x80, svc_return_bug: bool = False) -> None:
        self.image = image
        self.tick_cycles = tick_cycles
        self.scheduler = scheduler
        self.use_mpu = use_mpu
        self.unprivileged = unprivileged
        self.tasks: List[Task] = []
        self.current: Optional[Task] = None
        self.ticks = 0
        self.switches = 0
        self.started = False
        self.stop_requested = False
        self.stop_reason = ""
        self.stop_after_switches = stop_after_switches
        self.stop_after_ticks = stop_after_ticks
        self.mutexes: Dict[int, Optional[str]] = {}
        self.machine: Any = None
        self.last_switch_cycles = 0
        self.rr_index = 0
        self.pendsv_priority = pendsv_priority
        self.systick_priority = systick_priority
        self.svc_priority = svc_priority
        self.svc_return_bug = svc_return_bug
        # The software frame is whatever PendSV pushes; the initial frames must match.
        self.sw_frame = tuple(_parse_reglist(pendsv_save))
        # ---- SRAM layout: [guard][stack] per task, low addresses first ----
        order = tasks + ([{"name": "idle", "entry": "idle_task", "priority": 255}] if idle else [])
        for spec in order:
            words = int(spec.get("stack_words", stack_words))
            guard = image.reserve_sram(f"guard_{spec['name']}", GUARD_BYTES)
            base = image.reserve_sram(f"stack_{spec['name']}", 4 * words)
            task = Task(name=spec["name"], entry=spec["entry"], stack_base=base, stack_words=words,
                        priority=int(spec.get("priority", 1)), guard_base=guard, arg=int(spec.get("arg", 0)))
            self.tasks.append(task)
        image.add_section("kernel", KERNEL_HANDLERS_SOURCE + pendsv_source(pendsv_save, pendsv_restore))
        image.set_handler(EXC_SYSTICK, "SysTick_Handler")
        image.set_handler(EXC_PENDSV, "PendSV_Handler")
        image.set_handler(EXC_SVCALL, "SVC_Handler")
        for number in (EXC_HARDFAULT, EXC_MEMMANAGE, EXC_BUSFAULT, EXC_USAGEFAULT):
            image.set_handler(number, "Fault_Handler")

    # ------------------------------------------------------------- attach
    def attach(self, machine) -> None:
        self.machine = machine
        machine.kernel = self
        cpu = machine.cpu
        cpu.kcalls["kernel:init"] = self.k_init
        cpu.kcalls["kernel:tick"] = self.k_tick
        cpu.kcalls["sched:switch"] = self.k_switch
        cpu.kcalls["svc:dispatch"] = self.k_svc
        cpu.kcalls["kernel:fault"] = self.k_fault
        cpu.context_fn = self.context
        # Rebase task addresses now that the image is linked.
        for task in self.tasks:
            base, _ = machine.image.reserve[f"stack_{task.name}"]
            task.stack_base = base
            task.guard_base, _ = machine.image.reserve[f"guard_{task.name}"]

    def context(self) -> str:
        cpu = self.machine.cpu
        if cpu.in_handler:
            number = cpu.ipsr
            if number in (EXC_PENDSV, EXC_SVCALL):
                return f"kernel:{exc_name(number)}"
            if number in (EXC_HARDFAULT, EXC_MEMMANAGE, EXC_BUSFAULT, EXC_USAGEFAULT):
                return f"kernel:{exc_name(number)}"
            return f"ISR:{exc_name(number)}"
        if self.started and self.current is not None:
            return self.current.name
        if self.machine.boot.steps and self.machine.boot.steps[-1]["step"] == "main_entered":
            return "main"
        return "boot"

    def layout(self) -> Dict[str, Any]:
        """Static description of a suspended task's stack, for the viewer and ``r2s stack``."""
        return {"sw_frame": list(self.sw_frame), "hw_frame": list(FRAME_WORDS),
                "watermark_pattern": f"0x{WATERMARK:08X}", "guard_bytes": GUARD_BYTES,
                "tasks": {t.name: {"stack_base": f"0x{t.stack_base:08X}", "stack_top": f"0x{t.stack_top:08X}",
                                   "guard_base": f"0x{t.guard_base:08X}"} for t in self.tasks}}

    def stack_snapshot(self, task: "Task") -> Dict[str, Any]:
        """The words a suspended task has on its stack, from its saved PSP up."""
        mem = self.machine.memory
        n = len(self.sw_frame) + len(FRAME_WORDS) + 1
        words = []
        for i in range(n):
            addr = task.psp + 4 * i
            if addr >= task.stack_top:
                break
            value = mem.peek(addr)
            words.append(f"0x{value:08X}" if isinstance(value, int) else "code")
        return {"psp": f"0x{task.psp:08X}", "state": task.state, "words": words}

    def stacks(self) -> Dict[str, Any]:
        return {t.name: self.stack_snapshot(t) for t in self.tasks}

    # ------------------------------------------------------ kernel init
    def k_init(self, cpu: Cpu) -> None:
        """``kcall "kernel:init"`` from main: build stacks, frames, MPU, SysTick."""
        m = self.machine
        for task in self.tasks:
            for addr in range(task.stack_base, task.stack_top, 4):
                m.memory.poke(addr, WATERMARK)
            # Build the initial frames so the first switch to this task "returns" into it.
            sp = task.stack_top
            frame = {"r0": task.arg, "r1": 0, "r2": 0, "r3": 0, "r12": 0,
                     "lr": m.image.symbols.get("task_exit", 0xFFFFFFFE) | 1,
                     "pc": m.image.symbols[task.entry], "xpsr": XPSR_THUMB}
            for reg in reversed(FRAME_WORDS):
                sp -= 4
                m.memory.poke(sp, frame[reg])
            for reg in reversed(self.sw_frame):
                sp -= 4
                m.memory.poke(sp, 0)
            task.psp = sp
            task.state = "READY"
            m.trace.emit("kernel", "task_state", f"task {task.name} created: stack 0x{task.stack_base:08X}.."
                         f"0x{task.stack_top:08X} ({4 * task.stack_words} B) filled with 0x{WATERMARK:08X}, "
                         f"initial frame built, psp=0x{sp:08X}, entry={task.entry}",
                         frm=None, to="READY", task=task.name, tcb=task.to_dict())
        if self.use_mpu:
            mpu = m.mpu
            mpu.regions.clear()
            img = m.image
            globals_size = max(32, ((img.bss_end - img.data_start) + 31) & ~31)
            mpu.add_region(MpuRegion("flash_code", mm.FLASH_BASE, mm.FLASH_SIZE, "ro", "ro"))
            mpu.add_region(MpuRegion("globals", img.data_start, globals_size, "rw", "rw"))
            mpu.add_region(MpuRegion("periph", mm.PERIPH_BASE, mm.PERIPH_SIZE, "rw", "rw"))
            for task in self.tasks:
                # Simplified "no access" guard. Real Armv8-M: AP = RO privileged-only + XN
                # (any unprivileged access or any write faults; privileged reads succeed).
                mpu.add_region(MpuRegion(f"guard_{task.name}", task.guard_base, GUARD_BYTES, "none", "none", guard=True))
            # last region: reprogrammed at every switch to cover the running task's stack
            mpu.add_region(MpuRegion("current_stack", 0, 0, "rw", "rw", enabled=False))
            mpu.enabled = True
            mpu.privdefena = True
            m.trace.emit("mpu", "mpu", f"MPU enabled with {len(mpu.regions)} regions (no overlaps): flash ro, "
                         f"globals rw, peripherals rw, one no-access guard below each task stack, and a region for "
                         f"the running task's stack; everything else is privileged-only via PRIVDEFENA",
                         regions=[r.to_dict() for r in mpu.regions])
        else:
            m.trace.emit("mpu", "mpu", "MPU left disabled: nothing stops a task from writing anywhere in SRAM")
        # SHCSR: enable the configurable fault handlers so faults do not all become HardFault
        m.nvic.memmanage_enabled = True
        m.nvic.busfault_enabled = True
        m.nvic.usagefault_enabled = True
        m.nvic.set_priority(EXC_SYSTICK, self.systick_priority)
        m.nvic.set_priority(EXC_PENDSV, self.pendsv_priority)
        m.nvic.set_priority(EXC_SVCALL, self.svc_priority)
        m.systick.reload = self.tick_cycles - 1
        m.systick.current = 0
        m.systick.enable = True
        m.systick.tickint = True
        pend = m.nvic.get_priority(EXC_PENDSV)
        warn = "" if pend >= max(m.nvic.priority.values() or [0]) else \
            "  WARNING: PendSV is NOT the lowest priority, so a switch can run inside another handler"
        m.trace.emit("kernel", "kernel", f"SysTick programmed: RVR={self.tick_cycles - 1} -> one tick every "
                     f"{self.tick_cycles} cycles ({m.trace.cycles_to_us(self.tick_cycles)} us); SHPR3: PendSV "
                     f"priority 0x{self.pendsv_priority:02X} (reads back 0x{pend:02X}), SysTick "
                     f"0x{m.nvic.get_priority(EXC_SYSTICK):02X}; SVCall 0x{m.nvic.get_priority(EXC_SVCALL):02X}"
                     + warn, pendsv_priority=pend)

    # -------------------------------------------------------------- tick
    def k_tick(self, cpu: Cpu) -> None:
        self.ticks += 1
        woke = []
        for task in self.tasks:
            if task.state == "BLOCKED" and task.sleep_ticks > 0:
                task.sleep_ticks -= 1
                if task.sleep_ticks == 0:
                    task.state = "READY"
                    woke.append(task.name)
        for name in woke:
            self.machine.trace.emit("kernel", "task_state", f"task {name} sleep expired -> READY",
                                    frm="BLOCKED", to="READY", task=name)
        if self.stop_after_ticks and self.ticks >= self.stop_after_ticks:
            self.stop_requested = True
            self.stop_reason = f"kernel: stop after {self.ticks} ticks"
        if self.started:
            self.machine.memory.write(mm.SCB_ICSR, 1 << 28)  # PENDSVSET

    # ------------------------------------------------------------ scheduling
    def pick_next(self) -> Task:
        ready = [t for t in self.tasks if t.state in ("READY", "RUNNING")]
        if not ready:
            raise RuntimeError("no runnable task (even idle is gone)")
        # The idle task only runs when nothing else can.
        if len(ready) > 1 and any(t.name == "idle" for t in ready):
            ready = [t for t in ready if t.name != "idle"]
        if self.scheduler == "priority":
            best = min(t.priority for t in ready)
            ready = [t for t in ready if t.priority == best]
        # round robin: the task after the current one in creation order
        names = [t.name for t in self.tasks]
        start = names.index(self.current.name) if self.current else -1
        for offset in range(1, len(self.tasks) + 1):
            candidate = self.tasks[(start + offset) % len(self.tasks)]
            if candidate in ready:
                return candidate
        return ready[0]

    def _program_mpu_for(self, task: Task) -> None:
        if self.use_mpu:
            mpu = self.machine.mpu
            mpu.set_region(len(mpu.regions) - 1, task.stack_base, 4 * task.stack_words)

    def k_switch(self, cpu: Cpu) -> None:
        """``kcall "sched:switch"`` inside PendSV: r0 holds the old PSP (after the
        software frame push); returns the new PSP in r0."""
        m = self.machine
        old = self.current
        now = cpu.cycles
        if old is not None:
            old.psp = cpu.regs["r0"]
            old.run_cycles += now - self.last_switch_cycles
            if old.state == "RUNNING":
                old.state = "READY"
        nxt = self.pick_next()
        nxt.state = "RUNNING"
        nxt.switches_in += 1
        self.current = nxt
        self.switches += 1
        self.last_switch_cycles = now
        self._program_mpu_for(nxt)
        cpu.regs["r0"] = nxt.psp
        regs = ", ".join(self.sw_frame)
        where = "" if cpu.lr == EXC_RETURN_THREAD_PSP else \
            f"  WARNING: PendSV is running with EXC_RETURN=0x{cpu.lr:08X}, i.e. on top of another handler"
        m.trace.emit("kernel", "context_switch",
                     f"PendSV: saved {{{regs}}} of {old.name if old else '-'} at psp=0x{old.psp if old else 0:08X}; "
                     f"next={nxt.name} (psp=0x{nxt.psp:08X}); MPU stack region -> {nxt.name}{where}",
                     frm=old.name if old else None, to=nxt.name, old_psp=f"0x{old.psp:08X}" if old else None,
                     new_psp=f"0x{nxt.psp:08X}", switches=self.switches, tick=self.ticks,
                     exc_return=f"0x{cpu.lr:08X}", watermarks=self.watermarks(), stacks=self.stacks())
        if self.stop_after_switches and self.switches >= self.stop_after_switches:
            self.stop_requested = True
            self.stop_reason = f"kernel: stop after {self.switches} context switches"

    # ----------------------------------------------------------------- svc
    def _frame_addr(self, cpu: Cpu) -> int:
        # The frame is on the stack that was active when svc executed:
        # EXC_RETURN bit 2 = 1 means PSP (tst lr, #4 in a real handler).
        return cpu.psp if cpu.lr & 4 else cpu.msp

    def k_svc(self, cpu: Cpu) -> None:
        m = self.machine
        frame = self._frame_addr(cpu)
        stacked_pc = m.memory.peek(frame + 24)
        svc_instr = m.memory.peek((stacked_pc - 4) & 0xFFFFFFFF)   # the svc itself (PC - 2 on Thumb)
        if isinstance(svc_instr, Instruction) and svc_instr.op == "svc":
            number = int(svc_instr.args[0])
        else:
            number = cpu.last_svc or 0
        r0 = m.memory.peek(frame)        # stacked r0 = first argument
        name = SYSCALLS.get(number, f"unknown({number})")
        caller = self.current.name if self.current else "main"
        ret = 0
        if number == 0:
            if self.started:
                ret = 0xFFFFFFFF
            else:
                self.started = True
                first = self.pick_next()
                first.state = "RUNNING"
                first.switches_in += 1
                self.current = first
                self.last_switch_cycles = cpu.cycles
                # restore the software frame by hand (what the first-switch code does)
                sp = first.psp
                for reg in self.sw_frame:
                    cpu.regs[reg] = m.memory.peek(sp)
                    sp += 4
                cpu.psp = sp
                cpu.control = (2 | (1 if self.unprivileged else 0))
                cpu.lr = EXC_RETURN_THREAD_PSP
                self._program_mpu_for(first)
                m.trace.emit("kernel", "syscall", f"svc start: scheduler started, first task {first.name}; "
                             f"CONTROL={cpu.control} ({'unprivileged' if self.unprivileged else 'privileged'} thread on PSP), "
                             f"LR=0xFFFFFFFD so the return pops {first.name}'s frame from PSP",
                             frm="main", to=first.name, svc=number, name=name)
                m.trace.emit("kernel", "context_switch", f"first switch main -> {first.name}", frm="main",
                             to=first.name, new_psp=f"0x{first.psp:08X}", switches=self.switches, tick=self.ticks,
                             watermarks=self.watermarks(), stacks=self.stacks())
                return
        elif number == 1:
            m.memory.write(mm.SCB_ICSR, 1 << 28)
        elif number == 2:
            self.current.state = "BLOCKED"
            self.current.sleep_ticks = max(1, r0)
            m.trace.emit("kernel", "task_state", f"task {caller} sleeps {r0} tick(s) -> BLOCKED",
                         frm="RUNNING", to="BLOCKED", task=caller, ticks=r0)
            m.memory.write(mm.SCB_ICSR, 1 << 28)
        elif number == 3:
            m.memory.write(mm.UART_DR, r0 & 0xFF)
            ret = 1
        elif number == 4:
            self.current.state = "DEAD"
            self.current.kill_reason = "exit"
            m.trace.emit("kernel", "task_state", f"task {caller} called exit -> DEAD",
                         frm="RUNNING", to="DEAD", task=caller)
            m.memory.write(mm.SCB_ICSR, 1 << 28)
        elif number == 5:
            owner = self.mutexes.get(r0)
            if owner is None or owner == caller:
                self.mutexes[r0] = caller
                ret = 1
            else:
                self.current.state = "BLOCKED"
                self.current.sleep_ticks = 1  # retry next tick (simple polling mutex)
                m.trace.emit("kernel", "task_state", f"task {caller} blocked on mutex {r0} held by {owner}",
                             frm="RUNNING", to="BLOCKED", task=caller, mutex=r0, owner=owner)
                m.memory.write(mm.SCB_ICSR, 1 << 28)
                ret = 0
        elif number == 6:
            if self.mutexes.get(r0) == caller:
                self.mutexes[r0] = None
                ret = 1
        else:
            ret = 0xFFFFFFFF
        if self.svc_return_bug:
            # BUG (P17): the result goes into the live r0, which the exception
            # return overwrites with the stacked r0 (= the argument).
            cpu.regs["r0"] = ret & 0xFFFFFFFF
            where = "the LIVE r0 (lost: exception return restores the stacked r0)"
        else:
            m.memory.poke(frame, ret & 0xFFFFFFFF)  # return value goes into the stacked r0
            where = f"the stacked r0 at 0x{frame:08X}"
        m.trace.emit("kernel", "syscall", f"svc #{number} ({name}) from {caller}: number read from the svc "
                     f"instruction at stacked PC-4=0x{(stacked_pc - 4) & 0xFFFFFFFF:08X}, argument r0=0x{r0:08X} "
                     f"from the frame; handled in privileged handler mode; return value {ret} written to {where}",
                     frm=caller, to="kernel:SVCall", svc=number, name=name, arg=r0, ret=ret,
                     frame_at=f"0x{frame:08X}", return_to="live_r0" if self.svc_return_bug else "stacked_r0")

    # --------------------------------------------------------------- faults
    def k_fault(self, cpu: Cpu) -> None:
        m = self.machine
        number = cpu.ipsr
        # A fault may be followed by a stacking error on the way into the handler
        # (the task's SP already sits in the guard). Report the original fault.
        record = None
        for candidate in reversed(cpu.faults):
            record = candidate
            if not candidate.stacking_failed:
                break
        # EXC_RETURN 0xFFFFFFFD (thread, PSP) means a task was running: it is the culprit.
        culprit = self.current if (self.started and cpu.lr == EXC_RETURN_THREAD_PSP) else None
        if culprit is None or record is None:
            m.trace.emit("kernel", "kernel", f"{exc_name(number)} in the kernel or before the scheduler started: "
                         f"print the fault record and halt", frm=self.context(), to="HALT",
                         fault=record.to_dict() if record else None)
            cpu.halted = True
            cpu.halt_reason = f"fault handler: {exc_name(number)} outside a task ({record.reason if record else ''})"
            return
        culprit.state = "DEAD"
        culprit.kill_reason = f"{record.kind}: {record.reason}"
        # Like a real kernel: log the record, then clear CFSR/HFSR (write 1s)
        # so the next fault is reported with only its own bits.
        m.memory.write(mm.SCB_CFSR, cpu.cfsr)
        m.memory.write(mm.SCB_HFSR, cpu.hfsr)
        culprit.run_cycles += cpu.cycles - self.last_switch_cycles
        m.trace.emit("kernel", "task_state",
                     f"{record.kind} caused by task {culprit.name} at PC=0x{record.pc:08X} "
                     f"({record.instruction}); kernel kills the task, other tasks keep running",
                     frm="RUNNING", to="DEAD", task=culprit.name, fault=record.to_dict())
        nxt = self.pick_next()
        nxt.state = "RUNNING"
        nxt.switches_in += 1
        self.current = nxt
        self.switches += 1
        self.last_switch_cycles = cpu.cycles
        sp = nxt.psp
        for reg in self.sw_frame:
            cpu.regs[reg] = m.memory.peek(sp)
            sp += 4
        cpu.psp = sp
        cpu.lr = EXC_RETURN_THREAD_PSP
        self._program_mpu_for(nxt)
        m.trace.emit("kernel", "context_switch", f"fault handler switches {culprit.name} (dead) -> {nxt.name}",
                     frm=culprit.name, to=nxt.name, new_psp=f"0x{nxt.psp:08X}", switches=self.switches,
                     tick=self.ticks, watermarks=self.watermarks(), stacks=self.stacks())

    # ---------------------------------------------------------------- stats
    def watermark(self, task: Task) -> Dict[str, Any]:
        """Deepest word ever written: the first non-pattern word from the bottom."""
        used = 0
        for i, addr in enumerate(range(task.stack_base, task.stack_top, 4)):
            if self.machine.memory.peek(addr) != WATERMARK:
                used = task.stack_words - i
                break
        return {"used_bytes": 4 * used, "size_bytes": 4 * task.stack_words,
                "percent": round(100 * used / task.stack_words, 1)}

    def watermarks(self) -> Dict[str, Any]:
        return {t.name: self.watermark(t) for t in self.tasks}

    def stats(self) -> Dict[str, Any]:
        m = self.machine
        cycles = m.cpu.cycles
        hz = m.config.cpu_hz
        if self.current is not None and self.started:
            self.current.run_cycles += cycles - self.last_switch_cycles
            self.last_switch_cycles = cycles
        total = max(1, cycles)
        per_task = {}
        for t in self.tasks:
            per_task[t.name] = {**t.to_dict(), "run_percent": round(100 * t.run_cycles / total, 1),
                                "watermark": self.watermark(t)}
        seconds = cycles / hz
        idle = per_task.get("idle", {}).get("run_cycles", 0)
        kernel_cycles = sum(v for k, v in m.cpu.ctx_cycles.items() if k.startswith("kernel:"))
        return {
            "scheduler": self.scheduler, "started": self.started, "ticks": self.ticks,
            "switches": self.switches,
            "switches_per_second": round(self.switches / seconds) if seconds else 0,
            "tick_cycles": self.tick_cycles,
            "idle_percent": round(100 * idle / total, 1),
            "kernel_percent": round(100 * kernel_cycles / total, 1),
            "tasks": per_task,
            "mpu": self.use_mpu, "unprivileged": self.unprivileged,
            "pendsv_priority": f"0x{m.nvic.get_priority(EXC_PENDSV):02X}",
            "layout": self.layout(),
        }
