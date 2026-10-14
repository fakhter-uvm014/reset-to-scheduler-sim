"""SysTick: the 24-bit down counter every Cortex-M has.

Rules modelled
--------------
* ``SYST_CSR``: bit0 ENABLE, bit1 TICKINT (pend the SysTick exception when
  the counter wraps), bit2 CLKSOURCE (ignored: always the core clock),
  bit16 COUNTFLAG (set when the counter reached 0, cleared on read).
* ``SYST_RVR``: the reload value. The period is ``RVR + 1`` cycles.
* ``SYST_CVR``: the current value; any write clears it to 0 (and the next
  cycle reloads from RVR).
* When the counter counts from 1 to 0 it reloads and, if TICKINT, pends
  exception 15. The kernel uses this as the scheduler tick.

The counter advances in ``advance(cycles)`` steps driven by the CPU.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass

from . import memory as mm
from .nvic import EXC_SYSTICK


@dataclass
class SysTick:
    nvic: object
    enable: bool = False
    tickint: bool = False
    reload: int = 0
    current: int = 0
    countflag: bool = False
    ticks: int = 0  # how many times the counter wrapped (for stats)

    def attach(self, memory: mm.Memory) -> None:
        memory.attach_device(mm.SYST_CSR, 12, self._read, self._write)

    def _read(self, address: int) -> int:
        if address == mm.SYST_CSR:
            value = int(self.enable) | (int(self.tickint) << 1) | (1 << 2) | (int(self.countflag) << 16)
            self.countflag = False
            return value
        if address == mm.SYST_RVR:
            return self.reload
        if address == mm.SYST_CVR:
            return self.current
        return 0

    def _write(self, address: int, value: int) -> None:
        if address == mm.SYST_CSR:
            self.enable = bool(value & 1)
            self.tickint = bool(value & 2)
        elif address == mm.SYST_RVR:
            self.reload = value & 0xFFFFFF
        elif address == mm.SYST_CVR:
            self.current = 0

    def advance(self, cycles: int) -> None:
        """Count down ``cycles`` core clocks, wrapping as many times as needed."""
        if not self.enable or self.reload == 0:
            return
        remaining = cycles
        while remaining > 0:
            if self.current == 0:
                # a zero counter reloads on the next clock (no tick on the reload itself)
                self.current = self.reload
                remaining -= 1
                continue
            if remaining < self.current:
                self.current -= remaining
                remaining = 0
            else:
                remaining -= self.current
                self.current = 0
                self.countflag = True
                self.ticks += 1
                if self.tickint:
                    self.nvic.set_pending(EXC_SYSTICK, "SysTick counter reached zero (tick)", unit="systick")

    def cycles_until_tick(self) -> int:
        """Cycles until the next wrap, used to skip ahead during ``wfi``."""
        if not self.enable or self.reload == 0:
            return 1 << 62
        return self.current if self.current > 0 else self.reload + 1
