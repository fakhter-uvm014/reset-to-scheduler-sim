"""The instruction subset the simulator executes, and its tiny assembler.

The model executes a **subset of ARM Thumb-2 at the assembly level**: the
mnemonics, register names, operand forms and flag rules are the ones a
student reads in ``arm-none-eabi-objdump -d`` output, so what is learned here
transfers to the board. It is a behavioral model, not a Cortex-M emulator:

* every instruction occupies one 4-byte word (real Thumb-2 mixes 16- and
  32-bit encodings), so "the instruction before PC" is ``PC - 4`` here and
  ``PC - 2`` or ``PC - 4`` on the board,
* there is no binary encoding (``encoding`` is a synthetic hash) and
  immediates are not range-checked (real Thumb-2 needs ``movw``/``movt`` or a
  literal-pool load for large constants),
* only ``cmp``, ``adds`` and ``subs`` set the flags (as in Thumb-2, where the
  ``s`` suffix chooses flag setting),
* the cycle cost of each instruction comes from a *profile* table
  (:mod:`r2s.isa_profiles`), not from a pipeline model,
* a few pseudo-ops exist purely to make traces readable
  (``mark``, ``kcall``, ``gpio_set``, ``uart_puts`` ...). They are listed
  separately below and are never confused with real instructions.

Registers
---------
``r0``-``r12`` general purpose, ``sp`` (banked: ``msp`` or ``psp``, selected
by CONTROL.SPSEL exactly like Cortex-M), ``lr`` link register, ``pc``,
``xpsr`` (flags N Z C V in bits 31-28, the Thumb bit T in bit 24, the
exception number in bits 8-0).

Instruction table (the central table of this repo; keep it readable)
---------------------------------------------------------------------
    mnemonic           operands            meaning
    -----------------  ------------------  ------------------------------------
    mov  rd, op2       reg | #imm          rd = op2
    ldr  rd, =sym      symbol | number     literal-pool load of an address or
                                           constant (GNU as pseudo-instruction)
    add/sub rd,rn,op2                      rd = rn +/- op2
    adds/subs ...                          same, and set N Z C V flags
    mul  rd, rn, rm                        rd = rn * rm (low 32 bits)
    and/orr/eor rd,rn,op2                  bitwise
    lsl/lsr rd, rn, op2                    shifts
    cmp  rn, op2                           set flags from rn - op2
    ldr  rd, [rn, #off]                    rd = mem32[rn + off]
    str  rs, [rn, #off]                    mem32[rn + off] = rs
    ldmia rn!, {list}                      load multiple, rn += 4*n
    stmdb rn!, {list}                      store multiple (full descending), rn -= 4*n
    push {list} / pop {list}               stmdb sp! / ldmia sp!; pop {pc} returns
    b    label                             branch
    beq/bne/blt/bge/bgt/ble label          conditional branch on flags
    bl   label                             lr = return address | 1 (Thumb bit), branch
    bx   reg                               branch to reg; bit 0 must be 1 (Thumb);
                                           an EXC_RETURN value performs an
                                           exception return
    svc  #n                                supervisor call -> SVCall exception
    cpsid i / cpsie i                      set / clear PRIMASK (ignored when unprivileged)
    mrs  rd, msp|psp|control|primask|xpsr  read special register
    msr  msp|psp|control|primask, rn       write special register (ignored when unprivileged)
    nop / dsb / isb                        no effect in the model (barriers are free)
    wfi                                    sleep until an exception is pending
    halt                                   stop the simulation (stands in for a
                                           breakpoint at the end of a test program)
    ---- pseudo-ops (NOT Thumb-2; cost 0 unless stated) ----
    mark "text"                            emit a trace event (like a GPIO probe)
    kcall "name"                           call a Python routine that stands in for
                                           C code (kernel, startup checks, printf)
    gpio_set #pin / gpio_clr #pin          write GPIO_OUT_SET/CLR (cost: one str)
    uart_putc rn                           write UART_DR (cost: one str)
    uart_puts "text"                       write a whole string (cost: one str per char)

Author: Farzana Akhter
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

REGISTER_NAMES = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]
REGISTER_ALIASES = {"r13": "sp", "r14": "lr", "r15": "pc", "ip": "r12", "fp": "r11"}
SPECIAL_REGISTERS = {"msp", "psp", "control", "primask", "xpsr", "ipsr"}

BRANCH_OPS = {"b", "beq", "bne", "blt", "bge", "bgt", "ble", "bl", "bx"}
COND_BRANCH_OPS = {"beq", "bne", "blt", "bge", "bgt", "ble"}
ALU_OPS = {"mov", "add", "sub", "adds", "subs", "mul", "and", "orr", "eor", "lsl", "lsr", "cmp"}
MEM_OPS = {"ldr", "str", "ldmia", "stmdb", "push", "pop"}
SYSTEM_OPS = {"svc", "cpsid", "cpsie", "mrs", "msr", "nop", "dsb", "isb", "wfi", "halt"}
PSEUDO_OPS = {"mark", "kcall", "gpio_set", "gpio_clr", "uart_putc", "uart_puts"}
PRIVILEGED_OPS = {"cpsid", "cpsie", "msr"}
ALL_OPS = BRANCH_OPS | ALU_OPS | MEM_OPS | SYSTEM_OPS | PSEUDO_OPS


class AssemblyError(Exception):
    """A program could not be assembled (bad mnemonic, unknown label...)."""


@dataclass(frozen=True)
class MemOperand:
    """``[rn, #off]`` addressing: base register plus a constant offset."""

    base: str
    offset: int = 0

    def __str__(self) -> str:
        return f"[{self.base}, #{self.offset}]" if self.offset else f"[{self.base}]"


@dataclass(frozen=True)
class RegList:
    """``{r4-r11, lr}`` register list for push/pop/ldmia/stmdb (ordered low to high)."""

    regs: Tuple[str, ...]

    def __str__(self) -> str:
        return "{" + ", ".join(self.regs) + "}"

    def __len__(self) -> int:
        return len(self.regs)


@dataclass(frozen=True)
class Literal:
    """``=value`` in ``ldr rd, =value``: a constant or symbol address loaded
    from the literal pool placed after the function (GNU as syntax)."""

    value: Union[int, "Symbol"]

    def __str__(self) -> str:
        return f"={self.value}" if isinstance(self.value, Symbol) else f"=0x{self.value:X}"


@dataclass(frozen=True)
class Symbol:
    """``=name``: the address of a label or data symbol, resolved at link time."""

    name: str

    def __str__(self) -> str:
        return f"={self.name}"


Operand = Union[str, int, MemOperand, RegList, Symbol, Literal]


@dataclass
class Instruction:
    """One assembled instruction placed in memory."""

    op: str
    args: Tuple[Operand, ...] = ()
    label: Optional[str] = None     # label defined at this address, if any
    text: str = ""                  # original source text for listings/traces
    address: int = 0                # assigned by the linker
    encoding: int = 0               # synthetic; only so data reads of code are defined
    line: int = 0                   # source line number
    comment: str = ""               # source comment, shown by the annotated listing

    def __str__(self) -> str:
        return self.text or (self.op + " " + ", ".join(str(a) for a in self.args))


@dataclass
class Program:
    """An assembled sequence of instructions plus its label table.

    ``labels`` maps label -> word index; the linker converts indexes into
    absolute addresses when the program is placed at a base address.
    """

    name: str
    instructions: List[Instruction] = field(default_factory=list)
    labels: Dict[str, int] = field(default_factory=dict)

    @property
    def size_words(self) -> int:
        return len(self.instructions)

    def address_of(self, label: str, base: int) -> int:
        if label not in self.labels:
            raise AssemblyError(f"{self.name}: unknown label '{label}'")
        return base + 4 * self.labels[label]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
_LABEL_RE = re.compile(r"^([A-Za-z_.$][\w.$]*):\s*(.*)$")
_STRING_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"$')


def normalize_register(token: str) -> str:
    token = token.strip().lower()
    token = REGISTER_ALIASES.get(token, token)
    if token in REGISTER_NAMES:
        return token
    raise AssemblyError(f"not a register: '{token}'")


def _parse_int(token: str) -> int:
    token = token.strip().lstrip("#")
    try:
        return int(token, 0)
    except ValueError as exc:
        raise AssemblyError(f"bad immediate '{token}'") from exc


def _parse_reglist(text: str) -> RegList:
    inner = text.strip()[1:-1]
    regs: List[str] = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-")
            lo_n = int(normalize_register(lo)[1:])
            hi_n = int(normalize_register(hi)[1:])
            regs.extend(f"r{i}" for i in range(lo_n, hi_n + 1))
        else:
            regs.append(normalize_register(part))
    # Hardware stores register lists lowest-numbered at the lowest address.
    order = {name: i for i, name in enumerate(REGISTER_NAMES)}
    regs = sorted(set(regs), key=lambda r: order[r])
    return RegList(tuple(regs))


def _parse_operand(token: str) -> Operand:
    token = token.strip()
    if not token:
        raise AssemblyError("empty operand")
    if token.startswith("{"):
        return _parse_reglist(token)
    if token.startswith("["):
        inner = token[1:-1]
        parts = [p.strip() for p in inner.split(",")]
        base = normalize_register(parts[0])
        offset = _parse_int(parts[1]) if len(parts) > 1 else 0
        return MemOperand(base, offset)
    if token.startswith("="):
        rest = token[1:].strip()
        if re.match(r"^-?(0x[0-9A-Fa-f]+|\d+)$", rest):
            return Literal(_parse_int(rest) & 0xFFFFFFFF)
        return Literal(Symbol(rest))
    if token.startswith("#") or re.match(r"^-?(0x[0-9A-Fa-f]+|\d+)$", token):
        return _parse_int(token)
    m = _STRING_RE.match(token)
    if m:
        return m.group(1).encode("utf-8").decode("unicode_escape")
    low = token.lower()
    if low in SPECIAL_REGISTERS:
        return low
    if low in REGISTER_ALIASES or low in REGISTER_NAMES:
        return normalize_register(low)
    if low == "i":  # cpsid i / cpsie i
        return "i"
    # otherwise a label reference (branch target)
    if re.match(r"^[A-Za-z_.$][\w.$]*$", token):
        return Symbol(token)
    raise AssemblyError(f"cannot parse operand '{token}'")


def _split_operands(text: str) -> List[str]:
    """Split on commas that are not inside {}, [] or quotes."""
    out: List[str] = []
    depth = 0
    quote = False
    cur = ""
    for ch in text:
        if ch == '"' and (not cur or cur[-1] != "\\"):
            quote = not quote
        if not quote:
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
                continue
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _synthetic_encoding(op: str, args: Tuple[Operand, ...]) -> int:
    h = 0
    for ch in op + "|" + "|".join(str(a) for a in args):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def assemble(text: str, name: str = "program") -> Program:
    """Assemble teaching-ISA source text into a :class:`Program`.

    Comments start with ``;`` or ``@`` or ``//``. Labels end with ``:``.
    Example::

        main:
            mov  r0, #0          ; counter
        loop:
            add  r0, r0, #1
            cmp  r0, #10
            bne  loop
            halt
    """
    program = Program(name=name)
    pending_labels: List[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        code, comment = _split_comment(raw)
        line = code.strip()
        if not line:
            continue
        while True:
            m = _LABEL_RE.match(line)
            if not m:
                break
            label = m.group(1)
            if label in program.labels or label in pending_labels:
                raise AssemblyError(f"{name}:{lineno}: duplicate label '{label}'")
            pending_labels.append(label)
            line = m.group(2).strip()
        if not line:
            continue
        parts = line.split(None, 1)
        op = parts[0].lower()
        if op not in ALL_OPS:
            raise AssemblyError(f"{name}:{lineno}: unknown mnemonic '{op}'")
        args: Tuple[Operand, ...] = ()
        text = op
        if len(parts) > 1:
            operand_text = parts[1]
            # "ldmia r0!, {..}" -> drop the writeback '!' (always writeback here)
            operand_text = operand_text.replace("!", "")
            args = tuple(_parse_operand(t) for t in _split_operands(operand_text))
            # canonical text: one space after the mnemonic, ", " between operands
            text = f"{op} " + ", ".join(t.strip() for t in _split_operands(parts[1].strip()))
        _validate(op, args, name, lineno)
        instr = Instruction(op=op, args=args, text=text, line=lineno, comment=comment.strip(),
                            encoding=_synthetic_encoding(op, args))
        if pending_labels:
            instr.label = pending_labels[0]
            for label in pending_labels:
                program.labels[label] = len(program.instructions)
            pending_labels = []
        program.instructions.append(instr)
    if pending_labels:
        # labels at the very end: attach to an implicit nop so they resolve
        instr = Instruction(op="nop", text="nop", label=pending_labels[0])
        for label in pending_labels:
            program.labels[label] = len(program.instructions)
        program.instructions.append(instr)
    return program


def _validate(op: str, args: Tuple[Operand, ...], name: str, lineno: int) -> None:
    n = len(args)
    expect = {
        "mov": 2, "cmp": 2, "add": 3, "sub": 3, "adds": 3, "subs": 3, "mul": 3,
        "and": 3, "orr": 3, "eor": 3, "lsl": 3, "lsr": 3,
        "ldr": 2, "str": 2, "ldmia": 2, "stmdb": 2, "push": 1, "pop": 1,
        "b": 1, "beq": 1, "bne": 1, "blt": 1, "bge": 1, "bgt": 1, "ble": 1,
        "bl": 1, "bx": 1, "svc": 1, "cpsid": 1, "cpsie": 1, "mrs": 2, "msr": 2,
        "nop": 0, "dsb": 0, "isb": 0, "wfi": 0, "halt": 0,
        "mark": 1, "kcall": 1, "gpio_set": 1, "gpio_clr": 1, "uart_putc": 1, "uart_puts": 1,
    }
    if op in expect and n != expect[op]:
        raise AssemblyError(f"{name}:{lineno}: '{op}' expects {expect[op]} operand(s), got {n}")
    if op == "ldr" and not isinstance(args[1], (MemOperand, Literal)):
        raise AssemblyError(f"{name}:{lineno}: 'ldr' needs a [rn, #off] or =value operand")
    if op == "str" and not isinstance(args[1], MemOperand):
        raise AssemblyError(f"{name}:{lineno}: 'str' needs a [rn, #off] operand")
    if op != "ldr" and any(isinstance(a, Literal) for a in args):
        raise AssemblyError(f"{name}:{lineno}: '=value' is only valid in 'ldr rd, =value'")
    if op in ("push", "pop") and not isinstance(args[0], RegList):
        raise AssemblyError(f"{name}:{lineno}: '{op}' needs a {{register list}}")


def _split_comment(raw: str) -> Tuple[str, str]:
    """Split a source line into code and comment (``;``, ``@`` or ``//``),
    ignoring comment characters inside double quotes."""
    quote = False
    for i, ch in enumerate(raw):
        if ch == '"' and (i == 0 or raw[i - 1] != "\\"):
            quote = not quote
        elif not quote:
            if ch in ";@":
                return raw[:i], raw[i + 1:]
            if raw.startswith("//", i):
                return raw[:i], raw[i + 2:]
    return raw, ""


def disassemble(program: Program, base: int = 0, comments: bool = False) -> str:
    """Return a listing like ``objdump -d`` (addresses, labels, text).

    With ``comments=True`` the source comment of each line is appended, which
    is what ``r2s disasm`` prints as an annotated listing.
    """
    lines = []
    for i, instr in enumerate(program.instructions):
        addr = base + 4 * i
        labels = [k for k, v in program.labels.items() if v == i]
        for label in labels:
            lines.append(f"{addr:08x} <{label}>:")
        text = f"    {addr:08x}:    {instr.text}"
        if comments and instr.comment:
            text = f"{text:<52} ; {instr.comment}"
        lines.append(text)
    return "\n".join(lines)


def is_register(x: Any) -> bool:
    return isinstance(x, str) and x in REGISTER_NAMES
