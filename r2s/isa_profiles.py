"""ISA cost profiles: ``arm_like`` and ``riscv_like``.

The RP2350 has both Cortex-M33 (ARM) and Hazard3 (RISC-V) cores, and the
course asks students to run the same benchmark on both and compare cycles,
instructions retired and code size. This simulator runs one Thumb-2 style
instruction subset, so the comparison is modelled as two **illustrative cost
tables** applied to the same program. ``riscv_like`` models base RV32IMC
*without* Zcmp: base RV32IMC has no load/store-multiple, while Hazard3 adds
the Zcmp extension (``cm.push`` / ``cm.popret``) that a compiler can use for
prologues and epilogues, so real Hazard3 code can be shorter than this
profile suggests.

* ``cycles``   - cycles charged per instruction (before bus latency),
* ``instret``  - how many *retired instructions* one teaching instruction
                 stands for (RV32IMC without Zcmp has no store-multiple, so
                 one ``push {r4-r11}`` expands to 8 stores; RISC-V has
                 compare-and-branch instructions, so ``cmp`` + ``bne`` is one
                 ``bne rs1, rs2, label``),
* ``size``     - bytes of code the instruction would occupy (Thumb-2 mixes
                 2- and 4-byte encodings; RISC-V uses 4 bytes, or 2 with the
                 C extension for the simplest ops).

* ``kcall_cycles`` - what the C code that a ``kcall`` stands in for would
                 cost (scheduler bookkeeping, SVC dispatch...). Without it the
                 SysTick handler would look free and the context-switch cost
                 would be too small.

THESE NUMBERS ARE ILLUSTRATIVE. They are chosen so the *shape* of the result
matches what students usually see on the board (RISC-V retires a different
number of instructions for the same C code, Thumb-2 code is denser), not to
predict real numbers. Replace the tables with measured ones if you have them.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from .isa import Instruction, Literal, RegList


@dataclass
class IsaProfile:
    """Cost table for one ISA flavour."""

    name: str
    description: str
    cycles: Dict[str, int]
    size: Dict[str, int]
    instret: Dict[str, int]
    branch_taken_penalty: int = 1       # extra cycles when a branch is taken (pipeline flush)
    multi_reg_per_reg_cycles: int = 1   # push/pop/ldmia/stmdb: cycles per register
    multi_reg_expands: bool = False     # True: each register is its own retired instruction
    compare_and_branch: bool = False    # True: the ISA has compare-and-branch instructions, so
                                        # cmp + conditional branch is one retired instruction
    exception_entry_cycles: int = 12    # Cortex-M: 12 cycles to stack the frame and fetch the vector
    exception_return_cycles: int = 10
    tail_chain_cycles: int = 6          # entering the next handler without unstack/restack
    kcall_cycles: Dict[str, int] = field(default_factory=dict)
    literal_bytes: int = 6              # code bytes for "ldr rd, =value" (instruction + pool word)
    literal_instret: int = 1            # retired instructions for "ldr rd, =value"
    notes: Dict[str, str] = field(default_factory=dict)

    def cost(self, instr: Instruction, taken: bool = False) -> Tuple[int, int, int]:
        """Return ``(cycles, instret, size_bytes)`` for one executed instruction."""
        op = instr.op
        cyc = self.cycles.get(op, 1)
        ret = self.instret.get(op, 1)
        size = self.size.get(op, 4)
        if op in ("push", "pop", "ldmia", "stmdb"):
            regs = next((a for a in instr.args if isinstance(a, RegList)), RegList(()))
            n = len(regs)
            cyc = 1 + self.multi_reg_per_reg_cycles * n
            if self.multi_reg_expands:
                ret = n
                size = 4 * n
        if op == "uart_puts":
            n = len(instr.args[0]) if instr.args and isinstance(instr.args[0], str) else 1
            cyc = n * self.cycles.get("str", 2)
            ret = n * (2 if self.multi_reg_expands else 1)
            size = n * 4
        if op == "ldr" and len(instr.args) > 1 and isinstance(instr.args[1], Literal):
            # Thumb-2: ldr rd, [pc, #imm] (2 bytes) + a 4-byte literal-pool word.
            # RISC-V: lui + addi (two 4-byte instructions, two retired).
            size = self.literal_bytes
            ret = self.literal_instret
        if op == "kcall":
            cyc = self.kcall_cycles.get(str(instr.args[0]), 0) if instr.args else 0
            ret = cyc   # roughly one instruction per cycle for the stand-in C code
        if taken and op in ("b", "beq", "bne", "blt", "bge", "bgt", "ble", "bl", "bx"):
            cyc += self.branch_taken_penalty
        if self.compare_and_branch and op in ("beq", "bne", "blt", "bge", "bgt", "ble"):
            # RISC-V branches compare two registers: cmp + bne is one instruction
            ret = 0
        return cyc, ret, size


_ARM_CYCLES = {
    "mov": 1, "add": 1, "sub": 1, "adds": 1, "subs": 1, "mul": 1, "and": 1, "orr": 1,
    "eor": 1, "lsl": 1, "lsr": 1, "cmp": 1,
    "ldr": 2, "str": 2,
    "b": 1, "beq": 1, "bne": 1, "blt": 1, "bge": 1, "bgt": 1, "ble": 1, "bl": 1, "bx": 1,
    "svc": 1, "cpsid": 1, "cpsie": 1, "mrs": 1, "msr": 1, "nop": 1, "dsb": 1, "isb": 1,
    "wfi": 1, "halt": 0,
    "mark": 0, "kcall": 0, "gpio_set": 2, "gpio_clr": 2, "uart_putc": 2, "uart_puts": 2,
}
_ARM_SIZE = {  # Thumb-2: most simple ops have a 16-bit encoding
    "mov": 2, "add": 2, "sub": 2, "adds": 2, "subs": 2, "mul": 2, "and": 2, "orr": 2,
    "eor": 2, "lsl": 2, "lsr": 2, "cmp": 2, "ldr": 2, "str": 2, "push": 2, "pop": 2,
    "ldmia": 4, "stmdb": 4, "b": 2, "beq": 2, "bne": 2, "blt": 2, "bge": 2, "bgt": 2,
    "ble": 2, "bl": 4, "bx": 2, "svc": 2, "cpsid": 2, "cpsie": 2, "mrs": 4, "msr": 4,
    "nop": 2, "dsb": 4, "isb": 4, "wfi": 2, "halt": 2,
    "mark": 0, "kcall": 0, "gpio_set": 4, "gpio_clr": 4, "uart_putc": 4, "uart_puts": 4,
}
_RISCV_CYCLES = dict(_ARM_CYCLES)
_RISCV_CYCLES.update({"mul": 3, "ldr": 2, "str": 2, "bl": 1, "mov": 1})
_RISCV_SIZE = {k: 4 for k in _ARM_SIZE}
_RISCV_SIZE.update({  # the C extension gives 2-byte forms for the simplest ops
    "mov": 2, "add": 2, "sub": 2, "adds": 2, "subs": 2, "and": 2, "orr": 2,
    "lsl": 2, "lsr": 2, "ldr": 2, "str": 2, "b": 2, "nop": 2, "bx": 2,
    "mark": 0, "kcall": 0,
})
_RISCV_INSTRET = {k: 1 for k in _ARM_CYCLES}
# "mov rd, =symbol" on RISC-V is lui + addi (two instructions); on Thumb-2 it is
# one movw/movt pair too, but the Thumb-2 literal pool form is one ldr.
_RISCV_INSTRET.update({"mov": 1, "cpsid": 1, "cpsie": 1, "mark": 0, "kcall": 0})
_ARM_INSTRET = {k: 1 for k in _ARM_CYCLES}
_ARM_INSTRET.update({"mark": 0, "kcall": 0})
# Cycles charged for the C code each kcall stands in for (illustrative; a
# small kernel's tick bookkeeping and scheduler take a few dozen cycles).
# Startup checks, printing and kernel:init are free so boot timing stays
# about the startup loops themselves.
KCALL_CYCLES = {"kernel:tick": 20, "sched:switch": 24, "svc:dispatch": 16, "kernel:fault": 60}

PROFILES: Dict[str, IsaProfile] = {
    "arm_like": IsaProfile(
        name="arm_like",
        description="Thumb-2 flavoured costs: load/store multiple, 16-bit encodings for simple ops, "
                    "separate cmp and conditional branch.",
        cycles=_ARM_CYCLES, size=_ARM_SIZE, instret=_ARM_INSTRET,
        branch_taken_penalty=1, multi_reg_per_reg_cycles=1,
        multi_reg_expands=False, compare_and_branch=False,
        exception_entry_cycles=12, exception_return_cycles=10, tail_chain_cycles=6,
        kcall_cycles=KCALL_CYCLES,
        notes={"source": "illustrative; shaped after Cortex-M33 timing descriptions"},
    ),
    "riscv_like": IsaProfile(
        name="riscv_like",
        description="RV32IMC flavoured costs (without Zcmp): no load/store multiple (one instruction per "
                    "register), compare-and-branch instructions, 4-byte base encodings with some 2-byte "
                    "compressed forms.",
        cycles=_RISCV_CYCLES, size=_RISCV_SIZE, instret=_RISCV_INSTRET,
        branch_taken_penalty=2, multi_reg_per_reg_cycles=1,
        multi_reg_expands=True, compare_and_branch=True,
        exception_entry_cycles=4,   # RISC-V traps do not stack registers in hardware...
        exception_return_cycles=4,  # ...so entry is fast but the handler saves them itself
        tail_chain_cycles=4,
        kcall_cycles=KCALL_CYCLES, literal_bytes=8, literal_instret=2,
        notes={"source": "illustrative; shaped after Hazard3 (RV32IMC) descriptions"},
    ),
}


def get_profile(name: str) -> IsaProfile:
    if name not in PROFILES:
        raise KeyError(f"unknown ISA profile '{name}'; choose from {sorted(PROFILES)}")
    return PROFILES[name]
