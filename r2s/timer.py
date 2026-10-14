"""The 1 MHz system timer (RP2350 style TIMER block).

``TIMERAWL`` is a free-running microsecond counter derived from the core
cycle count (``cycles // cycles_per_us``). ``ALARM0`` fires IRQ_TIMER0 when
the counter reaches the alarm value; the handler must write 1 to ``INTR``
to clear it (a very common bug is forgetting this: the IRQ re-fires
forever, which looks like a hang).

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import memory as mm
from .nvic import IRQ_BASE, IRQ_TIMER0


@dataclass
class SystemTimer:
    nvic: object
    clock: Callable[[], int]
    cycles_per_us: int = 150
    alarm0: Optional[int] = None
    intr_pending: bool = False
    fired: int = 0
    level_triggered_bug: bool = False  # fault injection: INTR clear ignored

    def attach(self, memory: mm.Memory) -> None:
        memory.attach_device(mm.TIMER_BASE, 0x40, self._read, self._write)

    def now_us(self) -> int:
        return self.clock() // self.cycles_per_us

    def _read(self, address: int) -> int:
        if address == mm.TIMER_TIMERAWL:
            return self.now_us() & 0xFFFFFFFF
        if address == mm.TIMER_INTR:
            return int(self.intr_pending)
        if address == mm.TIMER_ALARM0:
            return self.alarm0 or 0
        return 0

    def _write(self, address: int, value: int) -> None:
        if address == mm.TIMER_ALARM0:
            self.alarm0 = value
        elif address == mm.TIMER_INTR:
            if value & 1 and not self.level_triggered_bug:
                self.intr_pending = False
                # The IRQ line is level sensitive: once the source is cleared the
                # NVIC no longer sees it asserted, so a level re-pend goes away.
                self.nvic.clear_pending(IRQ_BASE + IRQ_TIMER0)

    def advance(self) -> None:
        """Called after each instruction: fire the alarm when the time has come."""
        if self.alarm0 is not None and self.now_us() >= self.alarm0:
            self.alarm0 = None
            self.intr_pending = True
            self.fired += 1
            self.nvic.set_pending(IRQ_BASE + IRQ_TIMER0,
                                  f"TIMER ALARM0 reached {self.now_us()} us", unit="timer")
        elif self.intr_pending and not self.nvic.is_pending(IRQ_BASE + IRQ_TIMER0) \
                and (IRQ_BASE + IRQ_TIMER0) not in self.nvic.active:
            # The interrupt line is level sensitive: if INTR is still set when the
            # handler has returned, the NVIC sees the line asserted and pends again.
            self.nvic.set_pending(IRQ_BASE + IRQ_TIMER0,
                                  "TIMER INTR still set (handler did not clear it)", unit="timer")

    def cycles_until_alarm(self) -> int:
        if self.alarm0 is None:
            return 1 << 62
        remaining_us = self.alarm0 - self.now_us()
        return max(1, remaining_us * self.cycles_per_us)
