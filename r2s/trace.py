"""Trace events and the JSON trace file format.

Every interesting thing the model does becomes one event::

    {
      "seq": 17,                 # monotonic sequence number
      "t_cycles": 1234,          # DWT-style cycle count when it happened
      "t_us": 8.23,              # same instant in microseconds (cycles / MHz)
      "unit": "cpu",             # who: cpu, nvic, systick, timer, gpio, uart, mpu,
                                 #      boot, kernel, scenario
      "kind": "exception_entry", # what (see KINDS below)
      "from": "taskA",           # context/state before (or None)
      "to": "ISR:SysTick",       # context/state after (or None)
      "ctx": "ISR:SysTick",      # execution context right after the event
      "reason": "SysTick reload reached zero with TICKINT set",
      "regs": {"pc": "0x...", ...},  # registers that CHANGED since the previous
                                 # event (the first event carries all of them);
                                 # replaying the deltas gives the full register
                                 # file at any event (the viewer does this)
      "details": {...}           # free-form, e.g. the stacked frame
    }

Contexts are ``boot`` (Reset_Handler running), ``main``, a task name
(``A``, ``B``, ``C``, ``idle``), ``ISR:<name>`` for interrupt and fault
handlers, and ``kernel:<name>`` for PendSV/SVCall/fault handlers of the
kernel.

The rule from the project conventions: **every event has a human-readable
``reason``** so a student can read the log top to bottom like a story.

The trace file is::

    {
      "scenario": {...the scenario header...},
      "config": {...},
      "events": [...],
      "summary": {...},          # final state, stats, PASS/FAIL checks
      "variants": [...]          # only when a scenario runs several variants
    }

Author: Farzana Akhter
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

KINDS = {
    "scenario": "start of a run (scenario id, variant, injected faults)",
    "boot_step": "one of the six boot milestones, with a pass/fail check",
    "exception_entry": "the core stacked a frame and entered a handler",
    "tail_chain": "the core went from one handler to the next without unstacking",
    "exception_return": "a handler returned (frame popped) or tail-chained",
    "irq_pend": "an exception became pending (tick, button edge, PENDSVSET, svc...)",
    "fault": "a fault was detected: CFSR/HFSR bits, faulting PC, address",
    "lockup": "a fault happened where nothing can handle it; the core stopped",
    "critical_section": "cpsie ended a PRIMASK region while an interrupt was waiting",
    "privilege": "CONTROL changed, or an unprivileged privileged-op was ignored",
    "context_switch": "the kernel switched tasks (saved PSPs, stacks, watermarks)",
    "task_state": "a task was created, blocked, woken or killed",
    "syscall": "an svc instruction (cpu) or its handling (kernel)",
    "kernel": "kernel bookkeeping (SysTick programming, Default_Handler...)",
    "sleep": "wfi: the core slept until the next event",
    "mpu": "MPU configuration",
    "uart_tx": "a full line (or the final partial line) sent on the UART",
    "gpio": "an output pin changed or a button was pressed",
    "marker": "a 'mark' pseudo-op in the program (like a scope probe)",
    "halt": "the program reached its end (halt)",
}


@dataclass
class Event:
    seq: int
    t_cycles: int
    t_us: float
    unit: str
    kind: str
    frm: Optional[str]
    to: Optional[str]
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    ctx: Optional[str] = None
    regs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "t_cycles": self.t_cycles,
            "t_us": self.t_us,
            "unit": self.unit,
            "kind": self.kind,
            "from": self.frm,
            "to": self.to,
            "ctx": self.ctx,
            "reason": self.reason,
            "regs": self.regs,
            "details": self.details,
        }


class Trace:
    """Collects events; ``clock`` returns the current cycle count."""

    def __init__(self, clock: Callable[[], int], cpu_hz: int = 150_000_000) -> None:
        self.clock = clock
        self.cpu_hz = cpu_hz
        self.events: List[Event] = []
        self.listeners: List[Callable[[Event], None]] = []
        self.variant: Optional[str] = None
        # set by the machine: () -> (context name, register snapshot)
        self.state_fn: Optional[Callable[[], Any]] = None
        self._last_regs: Dict[str, Any] = {}

    def cycles_to_us(self, cycles: int) -> float:
        return round(cycles * 1_000_000 / self.cpu_hz, 3)

    def emit(self, unit: str, kind: str, reason: str, frm: Optional[str] = None,
             to: Optional[str] = None, **details: Any) -> Event:
        cycles = self.clock()
        if self.variant is not None:
            details.setdefault("variant", self.variant)
        event = Event(len(self.events), cycles, self.cycles_to_us(cycles),
                      unit, kind, frm, to, reason, details)
        if self.state_fn is not None:
            ctx, regs = self.state_fn()
            event.ctx = ctx
            event.regs = {k: v for k, v in regs.items() if self._last_regs.get(k) != v}
            self._last_regs = regs
        self.events.append(event)
        for listener in self.listeners:
            listener(event)
        return event

    def of_kind(self, *kinds: str) -> List[Event]:
        return [e for e in self.events if e.kind in kinds]

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]


def write_trace(path: Path, payload: Dict[str, Any]) -> None:
    """Write a trace JSON file (``indent=2``, insertion order kept)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)


def format_event(e: Dict[str, Any], width: int = 110) -> str:
    """One event as console text (plain ASCII). The reason is wrapped onto
    aligned continuation lines rather than cut, because it is the part a
    student reads."""
    import textwrap
    frm = e.get("from") or "-"
    to = e.get("to") or "-"
    prefix = (f"{e['t_cycles']:>8} cyc {e['t_us']:>8.2f} us  {e['kind']:<16} "
              f"{frm:>14} -> {to:<16} ")
    reason = " ".join(str(e["reason"]).encode("ascii", "replace").decode("ascii").split())
    room = max(30, width - len(prefix))
    lines = textwrap.wrap(reason, width=room) or [""]
    pad = " " * len(prefix)
    return "\n".join([prefix + lines[0]] + [pad + line for line in lines[1:]])


def replay_registers(events: List[Dict[str, Any]], upto: int) -> Dict[str, Any]:
    """Full register file at event index ``upto`` (inclusive), from the deltas."""
    regs: Dict[str, Any] = {}
    for e in events[: upto + 1]:
        if e.get("details", {}).get("variant") != events[upto].get("details", {}).get("variant"):
            continue
        regs.update(e.get("regs") or {})
    return regs
