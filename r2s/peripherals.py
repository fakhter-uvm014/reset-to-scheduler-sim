"""GPIO and UART: the two peripherals the course drives from the datasheet.

GPIO
----
``GPIO_OUT`` / ``GPIO_OUT_SET`` / ``GPIO_OUT_CLR`` drive the output pins. The
LED (pin 25 on a Pico) and "marker" pins used to cross-check timing on an
oscilloscope are just bits here, but every change is a trace event with the
cycle count, which is exactly what the scope would show.

A pushbutton on pin 14 is an *input*: ``press(pin)`` sets the level, records
an edge in ``GPIO_INTR`` and pends IRQ_GPIO. The handler must write 1 to
``GPIO_INTR`` to acknowledge the edge.

UART
----
``UART_DR`` transmits one character. The model has an infinitely fast
transmitter, so a write never blocks; ``UART_FR`` always reports TX empty.
Everything written is collected in ``output`` so tests can check it, and
each write is a ``uart_tx`` trace event.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import memory as mm
from .nvic import IRQ_BASE, IRQ_GPIO

LED_PIN = 25
BUTTON_PIN = 14
MARKER_PIN = 16


@dataclass
class Gpio:
    nvic: object
    trace: object
    out: int = 0
    inputs: int = 0
    intr: int = 0
    toggles: Dict[int, int] = field(default_factory=dict)
    last_press_cycles: int = 0

    def attach(self, memory: mm.Memory) -> None:
        memory.attach_device(mm.GPIO_BASE, 0x40, self._read, self._write)

    def _read(self, address: int) -> int:
        if address == mm.GPIO_OUT:
            return self.out
        if address == mm.GPIO_IN:
            return self.inputs
        if address == mm.GPIO_INTR:
            return self.intr
        return 0

    def _write(self, address: int, value: int) -> None:
        if address == mm.GPIO_OUT:
            self._set_out(value)
        elif address == mm.GPIO_OUT_SET:
            self._set_out(self.out | value)
        elif address == mm.GPIO_OUT_CLR:
            self._set_out(self.out & ~value)
        elif address == mm.GPIO_INTR:
            self.intr &= ~value  # write-1-to-clear

    def _set_out(self, new: int) -> None:
        changed = (self.out ^ new) & 0xFFFFFFFF
        self.out = new & 0xFFFFFFFF
        pin = 0
        while changed:
            if changed & 1:
                level = (self.out >> pin) & 1
                self.toggles[pin] = self.toggles.get(pin, 0) + 1
                name = {LED_PIN: "LED", MARKER_PIN: "MARKER"}.get(pin, f"GP{pin}")
                self.trace.emit("gpio", "gpio", f"{name} pin {pin} -> {level}",
                                frm=str(1 - level), to=str(level), pin=pin, level=level)
            changed >>= 1
            pin += 1

    def press(self, pin: int = BUTTON_PIN, nominal: Optional[int] = None) -> None:
        """External stimulus: a button edge. Pends IRQ_GPIO.

        ``nominal`` is the cycle the edge really happened; the core only notices
        it once the current instruction completes, which is part of the latency.
        """
        self.inputs |= 1 << pin
        self.intr |= 1 << pin
        self.last_press_cycles = self.trace.clock()
        if nominal is None:
            nominal = self.last_press_cycles
        self.trace.emit("gpio", "gpio", f"button on pin {pin} pressed (edge at cycle {nominal})",
                        frm="0", to="1", pin=pin, level=1, stimulus=True, nominal=nominal)
        self.nvic.set_pending(IRQ_BASE + IRQ_GPIO, f"GPIO edge on pin {pin}", unit="gpio")

    def release(self, pin: int = BUTTON_PIN) -> None:
        self.inputs &= ~(1 << pin)


@dataclass
class Uart:
    trace: object
    output: str = ""
    chars: int = 0
    lines: List[str] = field(default_factory=list)
    _line: str = ""

    def attach(self, memory: mm.Memory) -> None:
        memory.attach_device(mm.UART_BASE, 0x40, self._read, self._write)

    def _read(self, address: int) -> int:
        if address == mm.UART_FR:
            return 1 << 7  # TXFE: transmit FIFO empty
        return 0

    def _write(self, address: int, value: int) -> None:
        if address == mm.UART_DR:
            ch = chr(value & 0xFF)
            self.output += ch
            self.chars += 1
            if ch == "\n":
                self.lines.append(self._line)
                self.trace.emit("uart", "uart_tx", f"UART line: {self._line!r}", text=self._line)
                self._line = ""
            else:
                self._line += ch

    def flush(self) -> None:
        if self._line:
            self.lines.append(self._line)
            self.trace.emit("uart", "uart_tx", f"UART partial line: {self._line!r}", text=self._line)
            self._line = ""
