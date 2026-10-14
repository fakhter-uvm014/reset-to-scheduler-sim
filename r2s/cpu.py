"""The CPU core: fetch/decode/execute, privilege, banked stack pointers,
exception entry/return with the hardware-stacked frame, and faults.

What is modelled (in shape and in the order things happen, not in encoding)
--------------------------------------------------------------------------
**Exception entry** -- what the hardware does before the first handler
instruction runs::

      higher addresses
      +----------+
      | (pad)    |  <- only if SP was not 8-byte aligned; xPSR bit 9 records it
      |   xPSR   |  <- frame + 28    flags, Thumb bit (24), interrupted IPSR
      |    PC    |  <- frame + 24    return address (the interrupted instruction)
      |    LR    |  <- frame + 20
      |   r12    |  <- frame + 16
      |    r3    |  <- frame + 12
      |    r2    |  <- frame + 8
      |    r1    |  <- frame + 4
      |    r0    |  <- frame + 0     <- SP after entry points here
      +----------+
      lower addresses

   then LR = EXC_RETURN, IPSR = exception number, PC = vector[number] with
   bit 0 cleared (bit 0 must be 1: it sets the Thumb bit), mode = handler
   (always privileged, always MSP). The frame goes on the stack that was in
   use when the exception arrived (MSP or PSP).

**Exception return** happens when a handler executes ``bx lr`` (or
``pop {..., pc}``) with an EXC_RETURN value::

      0xFFFFFFF1  return to handler mode, frame on MSP
      0xFFFFFFF9  return to thread mode,  frame on MSP
      0xFFFFFFFD  return to thread mode,  frame on PSP

   These are the classic values. ARMv8-M defines more bits (S, DCRS, FType,
   ES). They are valid in the Secure state with no FPU context: the RP2350
   implements the Security Extension and bare-metal code runs Secure, so
   this is what such a program sees. Non-secure code, or an Armv8-M core
   without the Security Extension, sees 0xFFFFFFB0 / 0xFFFFFFB8 / 0xFFFFFFBC
   instead. Those values and FPU frames (0xFFFFFFE1/E9/ED) are not modelled.

   The core pops the eight words (plus the pad word if xPSR bit 9 is set).
   If another exception is pending that may pre-empt the context being
   returned to, the core instead **tail-chains**: it skips the pop/push pair,
   keeps the frame where it is, loads LR with the same EXC_RETURN and enters
   the next handler directly.

**Privilege**: handler mode is always privileged. Thread mode is privileged
unless CONTROL.nPRIV (bit 0) is set. CONTROL.SPSEL (bit 1) selects PSP as the
thread-mode stack pointer. The register name ``sp`` always means "the
currently selected stack pointer". Unprivileged ``cpsid``/``cpsie``/``msr``
are silently ignored, exactly as on the real core (a trace event says so).

**Thumb state**: the core tracks xPSR.T. It is set from bit 0 of a vector
table entry, of a ``bx``/``pop {pc}`` target and of a stacked xPSR on return.
Executing with T=0 raises a UsageFault INVSTATE, which is what a vector table
entry without the Thumb bit produces on real silicon.

**Faults** set sticky bits in CFSR (MMFSR | BFSR | UFSR), HFSR, MMFAR and
BFAR exactly where a fault decoder reads them (0xE000ED28 ...). A
configurable fault (MemManage, BusFault, UsageFault) escalates to HardFault
(HFSR.FORCED) when its handler is disabled in SHCSR or when its priority
cannot pre-empt the current execution priority (inside a more urgent
handler, or with PRIMASK set). A fault while HardFault is active locks the
core up. Each fault also becomes a :class:`FaultRecord`: what a student's
fault handler prints after the crash.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import memory as mm
from .isa import PRIVILEGED_OPS, Instruction, Literal, MemOperand, RegList, Symbol
from .isa_profiles import IsaProfile
from .memory import Memory, MemoryFault
from .nvic import (EXC_BUSFAULT, EXC_HARDFAULT, EXC_MEMMANAGE, EXC_PENDSV, EXC_SVCALL,
                   EXC_SYSTICK, EXC_USAGEFAULT, Nvic, exc_name)

EXC_RETURN_HANDLER_MSP = 0xFFFFFFF1
EXC_RETURN_THREAD_MSP = 0xFFFFFFF9
EXC_RETURN_THREAD_PSP = 0xFFFFFFFD
VALID_EXC_RETURN = (EXC_RETURN_HANDLER_MSP, EXC_RETURN_THREAD_MSP, EXC_RETURN_THREAD_PSP)
XPSR_THUMB = 1 << 24
XPSR_ALIGN_PAD = 1 << 9       # stacked xPSR bit 9: a pad word was inserted for 8-byte alignment
FRAME_WORDS = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")
FRAME_SIZE = 4 * len(FRAME_WORDS)
LOCKUP_PC = 0xEFFFFFFE        # ARMv8-M lockup address (ARMv7-M cores use 0xFFFFFFFE)

# ---------------------------------------------------------------------------
# Fault status registers: bit -> (register, bit position in CFSR/HFSR, meaning)
# CFSR = UFSR[31:16] | BFSR[15:8] | MMFSR[7:0]
# ---------------------------------------------------------------------------
FAULT_BITS: Dict[str, Tuple[str, int, str]] = {
    "IACCVIOL": ("MMFSR", 0, "instruction fetch from a location the MPU (or an XN region) does not allow; MMFAR not valid"),
    "DACCVIOL": ("MMFSR", 1, "data access violated the MPU permissions; MMFAR holds the address"),
    "MUNSTKERR": ("MMFSR", 3, "MPU fault while unstacking on exception return"),
    "MSTKERR": ("MMFSR", 4, "MPU fault while stacking the exception frame on entry (the stack itself is the problem)"),
    "MMARVALID": ("MMFSR", 7, "MMFAR holds a valid faulting address"),
    "IBUSERR": ("BFSR", 8, "bus error on an instruction fetch (unmapped or erroring memory); BFAR not valid"),
    "PRECISERR": ("BFSR", 9, "precise data bus error: the stacked PC is the faulting load/store and BFAR holds the address"),
    "IMPRECISERR": ("BFSR", 10, "imprecise data bus error (buffered write); the stacked PC is somewhere after the culprit"),
    "UNSTKERR": ("BFSR", 11, "bus error while unstacking on exception return"),
    "STKERR": ("BFSR", 12, "bus error while stacking the exception frame on entry (SP points at invalid memory)"),
    "BFARVALID": ("BFSR", 15, "BFAR holds a valid faulting address"),
    "UNDEFINSTR": ("UFSR", 16, "undefined instruction: the PC reached data or garbage"),
    "INVSTATE": ("UFSR", 17, "invalid state: executed with the Thumb bit clear (vector or branch target without bit 0)"),
    "INVPC": ("UFSR", 18, "invalid PC load on exception return (bad EXC_RETURN, or a corrupted stacked frame)"),
    "NOCP": ("UFSR", 19, "coprocessor (FPU) access while it is disabled"),
    "STKOF": ("UFSR", 20, "stack limit (MSPLIM/PSPLIM) exceeded"),
    "UNALIGNED": ("UFSR", 24, "unaligned access that the core does not allow"),
    "DIVBYZERO": ("UFSR", 25, "integer divide by zero with CCR.DIV_0_TRP set"),
}
HFSR_BITS: Dict[str, Tuple[int, str]] = {
    "VECTTBL": (1, "bus fault while reading the vector table"),
    "FORCED": (30, "a configurable fault was escalated to HardFault (its handler is disabled or "
                   "its priority could not pre-empt); look at CFSR for the original cause"),
    "DEBUGEVT": (31, "debug event (breakpoint) with the debugger detached"),
}
_ADDRESS_FLAGS = {"DACCVIOL": "MMARVALID", "PRECISERR": "BFARVALID"}


def decode_cfsr(cfsr: int, hfsr: int = 0) -> List[Dict[str, Any]]:
    """Return one entry per set bit: register, bit, name and what it means."""
    out: List[Dict[str, Any]] = []
    for name, (reg, bit, meaning) in FAULT_BITS.items():
        if cfsr & (1 << bit):
            out.append({"register": reg, "bit": bit, "name": name, "meaning": meaning})
    for name, (bit, meaning) in HFSR_BITS.items():
        if hfsr & (1 << bit):
            out.append({"register": "HFSR", "bit": bit, "name": name, "meaning": meaning})
    return out


def decode_exc_return(value: int) -> str:
    return {
        EXC_RETURN_HANDLER_MSP: "return to handler mode, frame on MSP (this handler pre-empted another one)",
        EXC_RETURN_THREAD_MSP: "return to thread mode, frame on MSP (interrupted main / privileged code on MSP)",
        EXC_RETURN_THREAD_PSP: "return to thread mode, frame on PSP (interrupted a task)",
    }.get(value, "not a supported EXC_RETURN value")


class CpuHalt(Exception):
    """Raised internally when the ``halt`` instruction runs."""


class UsageError(Exception):
    """An instruction cannot execute; becomes a UsageFault with ``flag``."""

    def __init__(self, reason: str, flag: str = "UNDEFINSTR") -> None:
        super().__init__(reason)
        self.reason = reason
        self.flag = flag


PrivilegeViolation = UsageError  # older name kept for readers of earlier notes


class SvcEscalation(Exception):
    """``svc`` executed where SVCall cannot pre-empt: becomes a HardFault."""


@dataclass
class FaultRecord:
    """What a fault decoder would print after the crash."""

    kind: str                 # exception taken: HardFault | BusFault | MemManage | UsageFault
    reason: str               # "<FLAG>: <meaning>; <detail>"
    address: Optional[int]    # faulting data address (MMFAR/BFAR when valid) or None
    pc: int                   # address of the faulting instruction (= stacked PC)
    instruction: str
    context: str
    privileged: bool
    sp_name: str
    sp_value: int
    stacked_frame: Optional[Dict[str, int]]   # None when stacking itself failed
    cfsr: Dict[str, List[str]]                # bits set by THIS fault, grouped by register
    flag: str = ""
    detail: str = ""
    cfsr_value: int = 0       # full sticky CFSR right after this fault
    hfsr_value: int = 0
    mmfar: Optional[int] = None
    bfar: Optional[int] = None
    exc_return: Optional[int] = None
    escalated_from: Optional[str] = None
    stacking_failed: bool = False
    t_cycles: int = 0

    def to_dict(self) -> Dict[str, Any]:
        frame = None
        if self.stacked_frame is not None:
            frame = {k: f"0x{v:08X}" for k, v in self.stacked_frame.items()}
        return {
            "kind": self.kind,
            "flag": self.flag,
            "reason": self.reason,
            "detail": self.detail,
            "address": f"0x{self.address:08X}" if self.address is not None else None,
            "pc": f"0x{self.pc:08X}",
            "instruction": self.instruction,
            "context": self.context,
            "privileged": self.privileged,
            "sp": f"{self.sp_name}=0x{self.sp_value:08X}",
            "stacked_frame": frame,
            "exc_return": f"0x{self.exc_return:08X}" if self.exc_return is not None else None,
            "cfsr": self.cfsr,
            "cfsr_value": f"0x{self.cfsr_value:08X}",
            "hfsr_value": f"0x{self.hfsr_value:08X}",
            "mmfar": f"0x{self.mmfar:08X}" if self.mmfar is not None else None,
            "bfar": f"0x{self.bfar:08X}" if self.bfar is not None else None,
            "decoded": decode_cfsr(self.cfsr_value, self.hfsr_value),
            "escalated_from": self.escalated_from,
            "stacking_failed": self.stacking_failed,
            "t_cycles": self.t_cycles,
        }


class Cpu:
    """The core. Register names follow :mod:`r2s.isa`."""

    def __init__(self, memory: Memory, nvic: Nvic, trace, profile: IsaProfile,
                 symbols: Optional[Dict[str, int]] = None) -> None:
        self.memory = memory
        self.nvic = nvic
        self.trace = trace
        self.profile = profile
        self.symbols: Dict[str, int] = symbols or {}
        self.regs: Dict[str, int] = {f"r{i}": 0 for i in range(13)}
        self.msp = 0
        self.psp = 0
        self.lr = 0
        self.pc = 0
        self.flags = {"N": False, "Z": False, "C": False, "V": False}
        self.thumb = True        # xPSR.T (EPSR)
        self.control = 0         # bit0 nPRIV, bit1 SPSEL
        self.ipsr = 0            # exception number while in handler mode
        self.cycles = 0          # DWT CYCCNT
        self.instret = 0         # instructions retired (RISC-V minstret flavour)
        self.code_bytes_executed = 0
        self.halted = False
        self.halt_reason = ""
        self.lockup = False
        self.faults: List[FaultRecord] = []
        # fault status registers (sticky until written with 1s)
        self.cfsr = 0
        self.hfsr = 0
        self.mmfar = 0
        self.bfar = 0
        self.kcalls: Dict[str, Callable[["Cpu"], None]] = {}
        self.context_fn: Callable[[], str] = lambda: "thread"
        self.ctx_cycles: Dict[str, int] = {}
        self.ctx_instret: Dict[str, int] = {}
        self.last_svc: Optional[int] = None
        self.vtor = mm.FLASH_BASE
        self.executed_addresses: Dict[int, int] = {}  # pc -> count (code size analysis)
        self.machine: Any = None
        self.exception_entries = 0
        self.tail_chains = 0
        self.wfi_sleeps = 0
        self.primask_since: Optional[int] = None
        self.critical_sections = 0
        self.critical_max = 0
        self.ignored_privileged_ops = 0
        self._charged = False
        self.attach_registers()

    # ------------------------------------------------------------- registers
    @property
    def in_handler(self) -> bool:
        return self.ipsr != 0

    @property
    def privileged(self) -> bool:
        return self.in_handler or not (self.control & 1)

    @property
    def sp_name(self) -> str:
        if not self.in_handler and (self.control & 2):
            return "psp"
        return "msp"

    @property
    def sp(self) -> int:
        return self.psp if self.sp_name == "psp" else self.msp

    @sp.setter
    def sp(self, value: int) -> None:
        if self.sp_name == "psp":
            self.psp = value & 0xFFFFFFFF
        else:
            self.msp = value & 0xFFFFFFFF

    @property
    def xpsr(self) -> int:
        value = (XPSR_THUMB if self.thumb else 0) | (self.ipsr & 0x1FF)
        for bit, name in ((31, "N"), (30, "Z"), (29, "C"), (28, "V")):
            if self.flags[name]:
                value |= 1 << bit
        return value

    def set_xpsr(self, value: int) -> None:
        self.flags = {"N": bool(value >> 31 & 1), "Z": bool(value >> 30 & 1),
                      "C": bool(value >> 29 & 1), "V": bool(value >> 28 & 1)}
        self.thumb = bool(value & XPSR_THUMB)
        self.ipsr = value & 0x1FF

    def get(self, name: str) -> int:
        if name == "sp":
            return self.sp
        if name == "lr":
            return self.lr
        if name == "pc":
            return self.pc
        return self.regs[name]

    def set(self, name: str, value: int) -> None:
        value &= 0xFFFFFFFF
        if name == "sp":
            self.sp = value
        elif name == "lr":
            self.lr = value
        elif name == "pc":
            self.pc = value
        else:
            self.regs[name] = value

    def snapshot(self) -> Dict[str, Any]:
        """Register dump (hex strings) as a debugger would show it."""
        snap: Dict[str, Any] = {k: f"0x{v:08X}" for k, v in self.regs.items()}
        snap.update({"sp": f"0x{self.sp:08X}", "msp": f"0x{self.msp:08X}", "psp": f"0x{self.psp:08X}",
                     "lr": f"0x{self.lr:08X}", "pc": f"0x{self.pc:08X}",
                     "xpsr": f"0x{self.xpsr:08X}", "control": self.control,
                     "primask": int(self.nvic.primask), "mode": "handler" if self.in_handler else "thread",
                     "privileged": self.privileged, "sp_sel": self.sp_name})
        return snap

    def attach_registers(self) -> None:
        """DWT CYCCNT, SCB ICSR/VTOR and the fault status registers as MMIO."""
        self.memory.attach_device(mm.DWT_CTRL, 8, self._read_reg, self._write_reg)
        self.memory.attach_device(mm.SCB_ICSR, 8, self._read_reg, self._write_reg)
        self.memory.attach_device(mm.SCB_CFSR, 0x14, self._read_reg, self._write_reg)
        self.memory.attach_device(mm.DEMCR, 4, self._read_reg, self._write_reg)

    def _read_reg(self, address: int) -> int:
        if address == mm.DWT_CYCCNT:
            return self.cycles & 0xFFFFFFFF
        if address == mm.DWT_CTRL:
            return 1          # CYCCNTENA: the model's counter always runs
        if address == mm.DEMCR:
            return 1 << 24    # TRCENA
        if address == mm.SCB_VTOR:
            return self.vtor
        if address == mm.SCB_ICSR:
            return (self.ipsr & 0x1FF) | (int(self.nvic.is_pending(EXC_PENDSV)) << 28) | \
                   (int(self.nvic.is_pending(EXC_SYSTICK)) << 26)
        if address == mm.SCB_CFSR:
            return self.cfsr
        if address == mm.SCB_HFSR:
            return self.hfsr
        if address == mm.SCB_MMFAR:
            return self.mmfar
        if address == mm.SCB_BFAR:
            return self.bfar
        return 0

    def _write_reg(self, address: int, value: int) -> None:
        if address == mm.DWT_CYCCNT:
            self.cycles = value
        elif address == mm.SCB_VTOR:
            self.vtor = value & ~0x7F
        elif address == mm.SCB_ICSR:
            if value & (1 << 28):
                self.nvic.set_pending(EXC_PENDSV, "PENDSVSET written to ICSR (kernel requests a context switch)")
            if value & (1 << 27):
                self.nvic.clear_pending(EXC_PENDSV)
            if value & (1 << 26):
                self.nvic.set_pending(EXC_SYSTICK, "PENDSTSET written to ICSR")
        elif address == mm.SCB_CFSR:
            self.cfsr &= ~value        # write 1 to clear
        elif address == mm.SCB_HFSR:
            self.hfsr &= ~value

    # --------------------------------------------------------------- helpers
    def context(self) -> str:
        return self.context_fn()

    def resolve(self, operand: Any) -> int:
        if isinstance(operand, int):
            return operand & 0xFFFFFFFF
        if isinstance(operand, Literal):
            return self.resolve(operand.value)
        if isinstance(operand, Symbol):
            if operand.name not in self.symbols:
                raise UsageError(f"undefined symbol '{operand.name}' (link error)")
            return self.symbols[operand.name]
        if isinstance(operand, str):
            return self.get(operand)
        raise UsageError(f"bad operand {operand!r}")

    def _charge(self, cycles: int, instret: int = 0, size: int = 0) -> None:
        ctx = self.context()
        self.cycles += cycles
        self.instret += instret
        self.code_bytes_executed += size
        self.ctx_cycles[ctx] = self.ctx_cycles.get(ctx, 0) + cycles
        self.ctx_instret[ctx] = self.ctx_instret.get(ctx, 0) + instret

    def _set_flags(self, result: int, a: int, b: int, sub: bool) -> None:
        r = result & 0xFFFFFFFF
        a &= 0xFFFFFFFF
        b &= 0xFFFFFFFF
        self.flags["N"] = bool(r >> 31)
        self.flags["Z"] = r == 0
        if sub:
            self.flags["C"] = a >= b                      # carry = NOT borrow (ARM convention)
            self.flags["V"] = ((a ^ b) & (a ^ r) & 0x80000000) != 0
        else:
            self.flags["C"] = (a + b) > 0xFFFFFFFF
            self.flags["V"] = ((~(a ^ b)) & (a ^ r) & 0x80000000) != 0

    def _cond(self, op: str) -> bool:
        f = self.flags
        return {
            "beq": f["Z"], "bne": not f["Z"],
            "blt": f["N"] != f["V"], "bge": f["N"] == f["V"],
            "bgt": (not f["Z"]) and f["N"] == f["V"], "ble": f["Z"] or f["N"] != f["V"],
        }[op]

    def _instr_text(self, pc: int) -> str:
        word = self.memory.peek(pc)
        if isinstance(word, Instruction):
            return str(word)
        if isinstance(word, int):
            return f"<data 0x{word:08X}>"
        return "?"

    def _emit(self, kind: str, frm: Optional[str], to: Optional[str], reason: str, **details: Any):
        details.setdefault("pc", f"0x{self.pc:08X}")
        return self.trace.emit("cpu", kind, reason, frm=frm, to=to, **details)

    # ----------------------------------------------------------- exceptions
    def _branch_interworking(self, target: int) -> None:
        """BX / POP {pc} semantics: bit 0 selects the Thumb state."""
        self.thumb = bool(target & 1)
        self.pc = target & ~1 & 0xFFFFFFFF

    def _read_vector(self, number: int) -> Optional[int]:
        """Vector fetch. Returns None when the table itself is unreadable."""
        address = (self.vtor + 4 * number) & 0xFFFFFFFF
        if self.memory.region_for(address) is None:
            return None
        word = self.memory.peek(address)
        return word if isinstance(word, int) else 0

    def enter_exception(self, number: int, tail_chain: bool = False,
                        from_ctx: Optional[str] = None,
                        exc_return: Optional[int] = None) -> Dict[str, Any]:
        """Take exception ``number`` now: stack the frame (unless tail-chaining),
        set LR/IPSR, and jump to the vector.

        Returns ``{"frame_at", "frame", "stacking_error"}`` so fault records can
        show the frame that was really pushed.
        """
        name = exc_name(number)
        frm = from_ctx if from_ctx is not None else self.context()
        info: Dict[str, Any] = {"frame_at": None, "frame": None, "stacking_error": None}
        self.nvic.clear_pending(number)
        stack_name = self.sp_name
        if not tail_chain:
            sp_before = getattr(self, stack_name)
            pad = 4 if sp_before % 8 else 0       # ARMv8-M always keeps the frame 8-byte aligned
            frame_at = (sp_before - pad - FRAME_SIZE) & 0xFFFFFFFF
            frame_values = [self.regs["r0"], self.regs["r1"], self.regs["r2"], self.regs["r3"],
                            self.regs["r12"], self.lr, self.pc, self.xpsr | (XPSR_ALIGN_PAD if pad else 0)]
            stacking_fault: Optional[MemoryFault] = None
            latency = 0
            try:
                # The model writes from the top (xPSR) down to r0; the hardware
                # order is not architecturally defined, only the final layout is.
                for i in reversed(range(len(FRAME_WORDS))):
                    latency += self.memory.write(frame_at + 4 * i, frame_values[i], self.privileged)
            except MemoryFault as fault:
                stacking_fault = fault
            # The SP is moved down by the frame size even when stacking failed.
            setattr(self, stack_name, frame_at)
            info["frame_at"] = frame_at
            exc_return = (EXC_RETURN_HANDLER_MSP if self.in_handler else
                          EXC_RETURN_THREAD_PSP if stack_name == "psp" else EXC_RETURN_THREAD_MSP)
            if stacking_fault is not None:
                info["stacking_error"] = stacking_fault.reason
                self.lr = exc_return
                target = self._stacking_failed(number, stacking_fault, stack_name, frame_at)
                if target is None:
                    return info
                number, name = target, exc_name(target)
            else:
                info["frame"] = dict(zip(FRAME_WORDS, frame_values))
            self.lr = exc_return
        else:
            # Tail-chain: the frame of the context underneath stays where it is
            # and LR gets the same EXC_RETURN that describes it.
            if exc_return is not None:
                self.lr = exc_return
        self.nvic.active.append(number)
        self.ipsr = number
        if tail_chain:
            self._charge(self.profile.tail_chain_cycles)
            self.tail_chains += 1
        else:
            self._charge(self.profile.exception_entry_cycles)
        self.exception_entries += 1
        vector = self._read_vector(number)
        details: Dict[str, Any] = {
            "exception": number, "name": name,
            "handler": f"0x{(vector or 0) & ~1:08X}", "vector": f"0x{(vector or 0):08X}",
            "exc_return": f"0x{self.lr:08X}", "priority": self.nvic.get_priority(number),
            "stack": stack_name if not tail_chain else ("psp" if self.lr == EXC_RETURN_THREAD_PSP else "msp"),
        }
        if info["frame_at"] is not None:
            details["frame_at"] = f"0x{info['frame_at']:08X}"
            details["stacked_frame"] = ({n: f"0x{v:08X}" for n, v in info["frame"].items()}
                                        if info["frame"] else None)
            if info["frame"] and info["frame"]["xpsr"] & XPSR_ALIGN_PAD:
                details["aligned_pad"] = True
            if info["stacking_error"]:
                details["stacking_error"] = info["stacking_error"]
        if vector is None:
            details["vector_error"] = f"vector table read at 0x{(self.vtor + 4 * number) & 0xFFFFFFFF:08X} failed"
        self.pc = (vector or 0) & ~1 & 0xFFFFFFFF
        self.thumb = bool((vector or 0) & 1)
        if tail_chain:
            reason = (f"{name} tail-chained: the frame underneath stays on the stack, no pop/push; "
                      f"LR=0x{self.lr:08X} again")
        else:
            pad_txt = " (plus a pad word for 8-byte alignment)" if details.get("aligned_pad") else ""
            reason = (f"{name} taken: hardware pushed r0-r3, r12, lr, pc, xPSR on {stack_name}{pad_txt}, "
                      f"LR=EXC_RETURN 0x{self.lr:08X}, PC=vector[{number}]")
            if info["stacking_error"]:
                reason = f"{name} taken after a stacking error (frame lost), LR=0x{self.lr:08X}"
        if vector is not None and not (vector & 1):
            reason += f"; vector 0x{vector:08X} has bit 0 clear, so the Thumb bit is 0"
        self._emit("tail_chain" if tail_chain else "exception_entry", frm, f"ISR:{name}", reason, **details)
        if vector is None:
            self._vector_fetch_failed(number)
        return info

    def _vector_fetch_failed(self, number: int) -> None:
        """The vector table entry could not be read: HardFault with HFSR.VECTTBL."""
        self.hfsr |= 1 << HFSR_BITS["VECTTBL"][0]
        if number == EXC_HARDFAULT or EXC_HARDFAULT in self.nvic.active[:-1]:
            self._lockup(f"vector table read for {exc_name(number)} failed while entering or inside HardFault")
            return
        self.nvic.active.pop()
        record = self._record("HardFault", "VECTTBL", f"vector table entry for {exc_name(number)} at "
                              f"VTOR=0x{self.vtor:08X} is unreadable", None, self.pc, escalated=None)
        self._emit("fault", self.context(), "ISR:HardFault", record.reason, fault=record.to_dict(),
                   fault_index=len(self.faults) - 1)
        self.enter_exception(EXC_HARDFAULT, tail_chain=True, exc_return=self.lr)

    def _stacking_failed(self, number: int, fault: MemoryFault, stack_name: str, frame_at: int) -> Optional[int]:
        """The frame could not be pushed (bad SP or MPU guard hit).

        Returns the exception number to enter instead (the fault handler),
        or ``None`` when the core locks up (a stacking error while entering
        HardFault, or while HardFault is active, cannot be handled).
        """
        if fault.kind == "MemManage":
            flag, target = "MSTKERR", (EXC_MEMMANAGE if self.nvic.memmanage_enabled else EXC_HARDFAULT)
        else:
            flag, target = "STKERR", (EXC_BUSFAULT if self.nvic.busfault_enabled else EXC_HARDFAULT)
        escalated = None
        if target != EXC_HARDFAULT and self.nvic.get_priority(target) >= self.nvic.execution_priority():
            target = EXC_HARDFAULT
        if target == EXC_HARDFAULT:
            escalated = "MemManage" if fault.kind == "MemManage" else "BusFault"
            self.hfsr |= 1 << HFSR_BITS["FORCED"][0]
        self.cfsr |= 1 << FAULT_BITS[flag][1]
        record = FaultRecord(
            kind=exc_name(target), flag=flag,
            reason=f"{flag}: {FAULT_BITS[flag][2]}; stacking error while entering {exc_name(number)}: {fault.reason}",
            detail=f"stacking error while entering {exc_name(number)}: {fault.reason}",
            address=fault.address, pc=self.pc, instruction=self._instr_text(self.pc),
            context=self.context(), privileged=self.privileged, sp_name=stack_name,
            sp_value=frame_at, stacked_frame=None, cfsr={FAULT_BITS[flag][0]: [flag]},
            cfsr_value=self.cfsr, hfsr_value=self.hfsr, exc_return=self.lr,
            escalated_from=escalated, stacking_failed=True, t_cycles=self.cycles)
        if escalated:
            record.cfsr["HFSR"] = ["FORCED"]
        self.faults.append(record)
        self._emit("fault", self.context(), f"ISR:{exc_name(target)}",
                   f"stacking error: cannot push the exception frame for {exc_name(number)} below "
                   f"{stack_name}=0x{(frame_at + FRAME_SIZE) & 0xFFFFFFFF:08X} ({fault.reason}); {flag} set, SP still moved "
                   f"to 0x{frame_at:08X}, frame lost", fault=record.to_dict(), fault_index=len(self.faults) - 1)
        if number == EXC_HARDFAULT or EXC_HARDFAULT in self.nvic.active:
            self._lockup(f"stacking failed while entering {exc_name(number)} (fault during HardFault entry)")
            return None
        if target != number and number not in (EXC_MEMMANAGE, EXC_BUSFAULT, EXC_USAGEFAULT, EXC_HARDFAULT):
            # an interrupt whose entry failed stays pending and is taken after the fault handler
            self.nvic.set_pending(number, f"{exc_name(number)} still pending: its entry hit a stacking fault", unit="cpu")
        return target

    def exception_return(self, exc_return: int) -> None:
        """``bx lr`` / ``pop {pc}`` with an EXC_RETURN value: pop the frame or tail-chain."""
        returning = self.nvic.active[-1]
        name = exc_name(returning)
        to_thread = exc_return in (EXC_RETURN_THREAD_MSP, EXC_RETURN_THREAD_PSP)
        if exc_return not in VALID_EXC_RETURN or (to_thread and len(self.nvic.active) > 1) or \
                (exc_return == EXC_RETURN_HANDLER_MSP and len(self.nvic.active) < 2):
            why = ("not a supported EXC_RETURN value" if exc_return not in VALID_EXC_RETURN else
                   "returns to thread mode while another exception is still active" if to_thread else
                   "returns to handler mode but no other exception is active")
            raise UsageError(f"INVPC: EXC_RETURN 0x{exc_return:08X} {why}", "INVPC")
        # Would another exception be taken as soon as we return? Then tail-chain.
        next_number = self.nvic.next_exception(self.nvic.priority_after_return())
        if next_number is not None:
            self.nvic.active.pop()
            self._emit("exception_return", f"ISR:{name}", f"ISR:{exc_name(next_number)}",
                       f"{name} returning, but {exc_name(next_number)} is pending and may pre-empt the context "
                       f"underneath: tail-chain (no unstack/restack)", exception=returning,
                       exc_return=f"0x{exc_return:08X}", tail_chain_to=next_number)
            self.ipsr = self.nvic.active[-1] if self.nvic.active else 0
            self.enter_exception(next_number, tail_chain=True, from_ctx=f"ISR:{name}", exc_return=exc_return)
            return
        stack_name = "psp" if exc_return == EXC_RETURN_THREAD_PSP else "msp"
        base = getattr(self, stack_name)
        # unstacking uses the privilege of the context being returned to
        unstack_privileged = not (to_thread and (self.control & 1))
        frame: Dict[str, int] = {}
        try:
            for i, reg in enumerate(FRAME_WORDS):
                value, _ = self.memory.read(base + 4 * i, unstack_privileged)
                frame[reg] = value
        except MemoryFault as fault:
            flag = "MUNSTKERR" if fault.kind == "MemManage" else "UNSTKERR"
            raise MemoryFault(fault.kind, fault.address, f"unstacking error on return from {name}: {fault.reason}",
                              flag=flag)
        self.nvic.active.pop()
        new_sp = base + FRAME_SIZE + (4 if frame["xpsr"] & XPSR_ALIGN_PAD else 0)
        setattr(self, stack_name, new_sp & 0xFFFFFFFF)
        for reg in ("r0", "r1", "r2", "r3", "r12"):
            self.regs[reg] = frame[reg]
        self.lr = frame["lr"]
        self.pc = frame["pc"] & ~1
        self.set_xpsr(frame["xpsr"] & ~XPSR_ALIGN_PAD)
        stacked_ipsr = frame["xpsr"] & 0x1FF
        if to_thread:
            if stack_name == "psp":
                self.control |= 2
            else:
                self.control &= ~2
        self._charge(self.profile.exception_return_cycles)
        if (to_thread and stacked_ipsr != 0) or (not to_thread and stacked_ipsr == 0):
            # Integrity check: the popped xPSR must agree with the mode we return
            # to. A mismatch means the frame was overwritten (stack overflow from
            # a neighbour, wrong frame size...): UsageFault INVPC.
            self.ipsr = 0 if to_thread else (self.nvic.active[-1] if self.nvic.active else 0)
            self.raise_fault("UsageFault",
                             f"exception return popped a corrupted frame from {stack_name} "
                             f"(stacked xPSR=0x{frame['xpsr']:08X} has IPSR={stacked_ipsr}, "
                             f"stacked PC=0x{frame['pc']:08X}); the stack of the resumed context was overwritten",
                             None, frame["pc"] & ~1, flag="INVPC")
            return
        self.ipsr = stacked_ipsr if not to_thread else 0
        self._emit("exception_return", f"ISR:{name}", self.context(),
                   f"{name} returned: popped r0-r3, r12, lr, pc, xPSR from {stack_name}, "
                   f"back to {'handler' if self.in_handler else 'thread'} mode "
                   f"({'privileged' if self.privileged else 'unprivileged'})",
                   exception=returning, exc_return=f"0x{exc_return:08X}", stack=stack_name,
                   popped_frame={n: f"0x{v:08X}" for n, v in frame.items()})

    # ---------------------------------------------------------------- faults
    def _record(self, kind: str, flag: str, detail: str, address: Optional[int], pc: int,
                escalated: Optional[str]) -> FaultRecord:
        reg_name = FAULT_BITS[flag][0] if flag in FAULT_BITS else "HFSR"
        meaning = FAULT_BITS[flag][2] if flag in FAULT_BITS else HFSR_BITS.get(flag, (0, flag))[1]
        bits = {reg_name: [flag]}
        if flag in _ADDRESS_FLAGS and address is not None:
            bits[reg_name].append(_ADDRESS_FLAGS[flag])
        if escalated:
            bits["HFSR"] = ["FORCED"]
        text = self._instr_text(pc)
        if flag == "INVSTATE":
            text = f"(never decoded: xPSR.T=0) {text}"
        record = FaultRecord(
            kind=kind, flag=flag, reason=f"{flag}: {meaning}; {detail}", detail=detail,
            address=address, pc=pc, instruction=text, context=self.context(),
            privileged=self.privileged, sp_name=self.sp_name, sp_value=self.sp, stacked_frame=None,
            cfsr=bits, cfsr_value=self.cfsr, hfsr_value=self.hfsr,
            mmfar=self.mmfar if self.cfsr & (1 << 7) else None,
            bfar=self.bfar if self.cfsr & (1 << 15) else None,
            escalated_from=escalated, t_cycles=self.cycles)
        self.faults.append(record)
        return record

    def raise_fault(self, kind: str, reason: str, address: Optional[int], pc: int,
                    flag: Optional[str] = None, fetch: bool = False) -> None:
        """Turn a detected fault into the right exception (with escalation).

        ``kind`` is the fault class (MemManage, BusFault, UsageFault, or
        HardFault for faults that only exist as HardFault), ``flag`` the CFSR
        or HFSR bit name. The stacked PC is ``pc`` (the faulting instruction).
        """
        if flag is None:
            flag = {"MemManage": "IACCVIOL" if fetch else "DACCVIOL",
                    "BusFault": "IBUSERR" if fetch else "PRECISERR",
                    "UsageFault": "UNDEFINSTR", "HardFault": "FORCED"}[kind]
        if kind == "MemManage":
            number = EXC_MEMMANAGE if self.nvic.memmanage_enabled else EXC_HARDFAULT
        elif kind == "BusFault":
            number = EXC_BUSFAULT if self.nvic.busfault_enabled else EXC_HARDFAULT
        elif kind == "UsageFault":
            number = EXC_USAGEFAULT if self.nvic.usagefault_enabled else EXC_HARDFAULT
        else:
            number = EXC_HARDFAULT
        why_escalated = "its handler is disabled in SHCSR"
        if number != EXC_HARDFAULT and self.nvic.get_priority(number) >= self.nvic.execution_priority():
            number = EXC_HARDFAULT
            why_escalated = ("its priority cannot pre-empt the current execution priority "
                             f"({'PRIMASK set' if self.nvic.primask else 'inside a more urgent handler'})")
        escalated = kind if (number == EXC_HARDFAULT and kind != "HardFault") else None
        # set the sticky status bits
        if flag in FAULT_BITS:
            self.cfsr |= 1 << FAULT_BITS[flag][1]
            if flag == "DACCVIOL" and address is not None:
                self.cfsr |= 1 << 7
                self.mmfar = address
            elif flag == "PRECISERR" and address is not None:
                self.cfsr |= 1 << 15
                self.bfar = address
        elif flag in HFSR_BITS:
            self.hfsr |= 1 << HFSR_BITS[flag][0]
        if escalated or kind == "HardFault":
            self.hfsr |= 1 << HFSR_BITS["FORCED"][0]
        self.pc = pc  # the stacked PC (and the debugger's PC) point at the faulting instruction
        if self.nvic.execution_priority() < 0:
            # already in HardFault (priority -1) or NMI: nothing can pre-empt -> lockup
            self._record("HardFault", flag, reason, address if flag in _ADDRESS_FLAGS else None, pc, escalated)
            self._lockup(f"{kind} ({flag}) while {exc_name(self.nvic.active[-1]) if self.nvic.active else 'HardFault'} "
                         f"is active: {reason}")
            return
        record = self._record(exc_name(number), flag, reason, address if flag in _ADDRESS_FLAGS else None,
                              pc, escalated)
        self._emit("fault", self.context(), f"ISR:{exc_name(number)}",
                   f"{kind} {flag} at PC=0x{pc:08X} ({record.instruction}): {reason}"
                   + (f" -> escalated to HardFault ({why_escalated})" if escalated else ""),
                   fault=record.to_dict(), fault_index=len(self.faults) - 1, pc=f"0x{pc:08X}")
        info = self.enter_exception(number)
        record.exc_return = self.lr
        record.stacked_frame = info.get("frame")
        if info.get("stacking_error"):
            record.stacking_failed = True

    def _lockup(self, reason: str) -> None:
        self.lockup = True
        self.halted = True
        self.halt_reason = f"LOCKUP: {reason}"
        self._emit("lockup", self.context(), "LOCKUP",
                   f"core locked up: {reason}. On real hardware the core stops fetching with "
                   f"PC=0x{LOCKUP_PC:08X} (ARMv8-M) until reset, and DHCSR.S_LOCKUP reads 1 in the debugger")

    # ------------------------------------------------------------- execution
    def step(self) -> None:
        """Run one instruction (taking a pending exception first if allowed)."""
        if self.halted:
            return
        number = self.nvic.next_exception()
        if number is not None:
            self.enter_exception(number)
            if self.halted:
                return
        pc = self.pc
        try:
            if not self.thumb:
                raise UsageError(f"execution with xPSR.T=0 at 0x{pc:08X}: the address that was loaded into "
                                 f"the PC had bit 0 clear (a vector table entry or branch target without "
                                 f"the Thumb bit)", "INVSTATE")
            try:
                instr, latency = self.memory.fetch(pc, self.privileged)
            except MemoryFault as fault:
                fault.fetch = True
                raise
            if not isinstance(instr, Instruction):
                raise UsageError(f"undefined instruction: word at 0x{pc:08X} is data 0x{instr:08X}, not code "
                                 f"(the PC jumped into data or garbage)", "UNDEFINSTR")
            self.executed_addresses[pc] = self.executed_addresses.get(pc, 0) + 1
            self.pc = (pc + 4) & 0xFFFFFFFF
            self._charged = False
            taken, extra = self.execute(instr)
            if not self._charged:
                cyc, ret, size = self.profile.cost(instr, taken)
                self._charge(cyc + latency + extra, ret, size)
        except MemoryFault as fault:
            self.raise_fault(fault.kind, fault.reason, fault.address, pc, flag=fault.flag, fetch=fault.fetch)
        except UsageError as err:
            self.raise_fault("UsageFault", err.reason, None, pc, flag=err.flag)
        except SvcEscalation:
            self.raise_fault("HardFault", "svc executed while SVCall could not pre-empt the current execution "
                             f"priority ({'PRIMASK set' if self.nvic.primask else 'inside a handler of equal or higher priority'}); "
                             "an SVC that cannot be taken at once escalates to HardFault", None, pc, flag="FORCED")
        except CpuHalt:
            pass

    def _ignored(self, op: str, what: str) -> None:
        self.ignored_privileged_ops += 1
        self._emit("privilege", self.context(), None,
                   f"'{op}' ignored: {what} in unprivileged thread mode has no effect (no fault, no "
                   f"warning on real hardware either)", op=op)

    def execute(self, instr: Instruction) -> Tuple[bool, int]:
        """Execute one decoded instruction. Returns ``(branch_taken, extra_cycles)``."""
        op, args = instr.op, instr.args
        extra = 0
        if op in PRIVILEGED_OPS and not self.privileged:
            # Real Cortex-M: CPS and MSR to system registers are ignored when
            # unprivileged. The model does the same, and emits an event.
            self._ignored(op, "changing PRIMASK" if op in ("cpsid", "cpsie") else f"writing {args[0]}")
            return False, 0
        if op == "mov":
            self.set(args[0], self.resolve(args[1]))
        elif op in ("add", "adds", "sub", "subs"):
            a, b = self.resolve(args[1]), self.resolve(args[2])
            result = (a - b) if op.startswith("sub") else (a + b)
            if op.endswith("s"):
                self._set_flags(result, a, b, op.startswith("sub"))
            self.set(args[0], result)
        elif op == "mul":
            self.set(args[0], self.resolve(args[1]) * self.resolve(args[2]))
        elif op == "and":
            self.set(args[0], self.resolve(args[1]) & self.resolve(args[2]))
        elif op == "orr":
            self.set(args[0], self.resolve(args[1]) | self.resolve(args[2]))
        elif op == "eor":
            self.set(args[0], self.resolve(args[1]) ^ self.resolve(args[2]))
        elif op == "lsl":
            self.set(args[0], self.resolve(args[1]) << (self.resolve(args[2]) & 31))
        elif op == "lsr":
            self.set(args[0], (self.resolve(args[1]) & 0xFFFFFFFF) >> (self.resolve(args[2]) & 31))
        elif op == "cmp":
            a, b = self.resolve(args[0]), self.resolve(args[1])
            self._set_flags(a - b, a, b, True)
        elif op == "ldr":
            if isinstance(args[1], Literal):
                # literal-pool load: charged as a load, does not touch the bus model
                self.set(args[0], self.resolve(args[1]))
            else:
                mem: MemOperand = args[1]
                address = (self.get(mem.base) + mem.offset) & 0xFFFFFFFF
                value, extra = self.memory.read(address, self.privileged)
                self.set(args[0], value)
        elif op == "str":
            mem = args[1]
            address = (self.get(mem.base) + mem.offset) & 0xFFFFFFFF
            extra = self.memory.write(address, self.get(args[0]), self.privileged)
        elif op in ("push", "stmdb"):
            base = "sp" if op == "push" else args[0]
            regs: RegList = args[-1]
            # Full-descending store: the lowest register goes to the lowest
            # address. The writes are issued in ascending address order, as
            # the Cortex-M33 typically does, so a push that runs into a guard
            # or unmapped memory faults on the LOWEST address of the range.
            # If a store faults, the base register is NOT written back (the
            # instruction is abandoned and restarts after the handler).
            low = (self.get(base) - 4 * len(regs.regs)) & 0xFFFFFFFF
            for i, reg in enumerate(regs.regs):
                extra += self.memory.write((low + 4 * i) & 0xFFFFFFFF, self.get(reg), self.privileged)
            self.set(base, low)
        elif op in ("pop", "ldmia"):
            base = "sp" if op == "pop" else args[0]
            regs = args[-1]
            address = self.get(base)
            values = []
            for reg in regs.regs:
                value, lat = self.memory.read(address, self.privileged)
                extra += lat
                address = (address + 4) & 0xFFFFFFFF
                values.append((reg, value))
            self.set(base, address)
            for reg, value in values:
                if reg == "pc":
                    if value >= 0xFFFFFFF0 and self.in_handler:
                        self._charge_now(instr, extra)   # the pop itself, then the return sequence
                        self.exception_return(value)
                    else:
                        self._branch_interworking(value)
                    return True, extra
                self.set(reg, value)
        elif op == "b":
            self.pc = self.resolve(args[0])
            return True, extra
        elif op in ("beq", "bne", "blt", "bge", "bgt", "ble"):
            if self._cond(op):
                self.pc = self.resolve(args[0])
                return True, extra
        elif op == "bl":
            self.lr = self.pc | 1  # return address with the Thumb bit, as on the real core
            self.pc = self.resolve(args[0])
            return True, extra
        elif op == "bx":
            target = self.resolve(args[0])
            if target >= 0xFFFFFFF0 and self.in_handler:
                self._charge_now(instr, extra)       # the bx itself, then the return sequence
                self.exception_return(target)
            else:
                # In thread mode an EXC_RETURN value is just an address in the
                # system region: the next fetch faults.
                self._branch_interworking(target)
            return True, extra
        elif op == "svc":
            number = self.resolve(args[0])
            self.last_svc = number
            self._emit("syscall", self.context(), "ISR:SVCall",
                       f"svc #{number} from {'privileged' if self.privileged else 'unprivileged'} "
                       f"{'handler' if self.in_handler else 'thread'} (r0=0x{self.regs['r0']:08X})",
                       svc=number, r0=f"0x{self.regs['r0']:08X}", r1=f"0x{self.regs['r1']:08X}")
            if self.nvic.get_priority(EXC_SVCALL) >= self.nvic.execution_priority():
                # SVC must be taken immediately; if it cannot pre-empt, it escalates.
                raise SvcEscalation()
            # SVC is synchronous: it is taken at once (no separate pend event).
            self._charge_now(instr, 0, taken=False)    # the svc itself is charged to the caller
            self.enter_exception(EXC_SVCALL)
            return True, 0
        elif op == "cpsid":
            if not self.nvic.primask:
                self.nvic.primask = True
                self.primask_since = self.cycles
        elif op == "cpsie":
            if self.nvic.primask:
                self.nvic.primask = False
                self._end_critical_section()
        elif op == "mrs":
            src = args[1]
            value = {"msp": self.msp, "psp": self.psp, "control": self.control,
                     "primask": int(self.nvic.primask), "xpsr": self.xpsr, "ipsr": self.ipsr}[src]
            self.set(args[0], value)
        elif op == "msr":
            dst, value = args[0], self.get(args[1])
            if dst == "msp":
                self.msp = value & ~3
            elif dst == "psp":
                self.psp = value & ~3
            elif dst == "control":
                old = self.control
                self.control = value & 3
                self._emit("privilege", None, None,
                           f"CONTROL={value & 3}: nPRIV={value & 1} SPSEL={(value >> 1) & 1} "
                           f"(thread mode now {'unprivileged' if value & 1 else 'privileged'}, "
                           f"SP={'PSP' if value & 2 else 'MSP'})", old=old, new=value & 3)
            elif dst == "primask":
                was = self.nvic.primask
                self.nvic.primask = bool(value & 1)
                if self.nvic.primask and not was:
                    self.primask_since = self.cycles
                elif was and not self.nvic.primask:
                    self._end_critical_section()
        elif op in ("nop", "dsb", "isb"):
            pass
        elif op == "wfi":
            self.wfi_sleeps += 1
            if self.machine is not None:
                extra = self.machine.sleep_until_event()
        elif op == "halt":
            self.halted = True
            self.halt_reason = "halt instruction"
            self._emit("halt", self.context(), "HALT", "halt instruction executed (end of program)")
            raise CpuHalt()
        elif op == "mark":
            self._emit("marker", None, None, f"marker: {args[0]}", text=args[0])
        elif op == "kcall":
            name = args[0]
            if name not in self.kcalls:
                raise UsageError(f"kcall '{name}' is not provided by the model (configuration error)")
            self.kcalls[name](self)
        elif op == "gpio_set":
            extra = self.memory.write(mm.GPIO_OUT_SET, 1 << self.resolve(args[0]), self.privileged)
        elif op == "gpio_clr":
            extra = self.memory.write(mm.GPIO_OUT_CLR, 1 << self.resolve(args[0]), self.privileged)
        elif op == "uart_putc":
            extra = self.memory.write(mm.UART_DR, self.get(args[0]) & 0xFF, self.privileged)
        elif op == "uart_puts":
            for ch in args[0]:
                extra += self.memory.write(mm.UART_DR, ord(ch), self.privileged)
        else:
            raise UsageError(f"unknown op {op}")
        return False, extra

    def _charge_now(self, instr: Instruction, extra: int, taken: bool = True) -> None:
        """Charge an instruction before an exception sequence starts, so the
        trace timestamps of entry/return events are in the right order."""
        cyc, ret, size = self.profile.cost(instr, taken)
        self._charge(cyc + extra, ret, size)
        self._charged = True

    def _end_critical_section(self) -> None:
        """PRIMASK went back to 0. Count the masked stretch and, if an interrupt
        had to wait for it, say so in the trace (that wait is interrupt latency)."""
        if self.primask_since is None:
            return
        length = self.cycles - self.primask_since
        self.primask_since = None
        self.critical_sections += 1
        self.critical_max = max(self.critical_max, length)
        waiting = [n for n in self.nvic.pending if self.nvic.is_enabled(n)]
        if waiting:
            names = ", ".join(exc_name(n) for n in waiting)
            waited = max(self.cycles - self.nvic.pending[n].pend_cycles for n in waiting)
            self._emit("critical_section", self.context(), None,
                       f"cpsie i after {length} cycles with PRIMASK=1; {names} was pending for {waited} cycles "
                       f"and is taken now (this wait is part of its interrupt latency)",
                       length=length, waited=waited, blocked=[exc_name(n) for n in waiting])
