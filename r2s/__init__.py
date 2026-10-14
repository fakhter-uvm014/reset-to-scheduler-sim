"""reset-to-scheduler-sim (package ``r2s``).

A behavioral model for learning and demonstration of the "Reset to Scheduler"
course project: taking a Raspberry Pi Pico 2 style microcontroller from
power-on reset to a small preemptive kernel.

This is NOT an ARM Cortex-M instruction emulator. It executes a small
teaching instruction set (see :mod:`r2s.isa`) on top of a memory map,
exception model, interrupt controller, timers and MPU that follow the
*shape* of the real hardware so that the mechanics a student must learn
(vector table, hardware-stacked exception frame, EXC_RETURN, PendSV
context switch, SVC system calls, MPU stack guards, cycle counting) can be
watched step by step in a trace.

Author: Farzana Akhter
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
