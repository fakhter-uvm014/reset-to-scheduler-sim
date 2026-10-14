"""Memory map, bus faults and a simple XIP-cache latency model.

The map follows the shape of the RP2350 (Pico 2) address space:

    0x1000_0000  FLASH   execute-in-place (XIP) flash, read-only, slow,
                         fronted by a small cache
    0x2000_0000  SRAM    fast read/write memory (code, .data, .bss, stacks)
    0x4000_0000  PERIPH  memory-mapped peripherals (GPIO, UART, TIMER)
    0xE000_0000  PPB     "private peripheral bus": SysTick, NVIC, SCB, MPU,
                         DWT (cycle counter)

Rules this module models
------------------------
* Any access to an address that no region covers is a **BusFault**
  (PRECISERR for data, IBUSERR for an instruction fetch).
* An instruction fetch from an execute-never part of the default memory map
  (peripherals 0x4xxxxxxx-0x5xxxxxxx, device and system space from
  0xA0000000 up) is a **MemManage IACCVIOL**, even with the MPU off.
* A write to FLASH is a BusFault (flash is read-only at run time).
* Unprivileged code may not touch the System Control Space (SysTick, NVIC,
  SCB, MPU registers in the PPB). On a real Cortex-M this is also a precise
  BusFault, which is how a task that pokes SysTick gets caught.
* A word access to an address that is not a multiple of 4 is a UsageFault
  UNALIGNED. (Real Cortex-M33 allows unaligned LDR/STR to normal memory unless
  CCR.UNALIGN_TRP is set, but always faults unaligned LDM/STM/PUSH/POP; the
  model only has word accesses, so it always faults.)
* Every access has a cost in cycles that depends on the region. SRAM is the
  baseline (0 extra cycles). Flash costs extra on an XIP-cache miss only, so
  a tight loop that fits in the cache runs almost as fast from flash as from
  SRAM after the first iteration (this is the point of scenario P10).

The storage is word-addressed (4 bytes). This is a simplification: the
teaching ISA has no byte or half-word accesses. A memory word may hold an
``int`` *or* an :class:`r2s.isa.Instruction` object (code), which lets the
CPU fetch instructions through the same bus and latency model as data.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


class MemoryFault(Exception):
    """Raised by the bus when an access is not allowed.

    ``kind`` is ``"BusFault"``, ``"MemManage"`` or ``"UsageFault"``;
    ``address`` is the faulting address, ``reason`` a human-readable
    explanation and ``fetch`` tells an instruction fetch from a data access
    (the fault status bit differs: IACCVIOL/IBUSERR vs DACCVIOL/PRECISERR).
    """

    def __init__(self, kind: str, address: int, reason: str, fetch: bool = False,
                 flag: Optional[str] = None) -> None:
        super().__init__(f"{kind} at 0x{address:08X}: {reason}")
        self.kind = kind
        self.address = address
        self.reason = reason
        self.fetch = fetch
        self.flag = flag


@dataclass
class Region:
    """One contiguous address range with permissions and a latency."""

    name: str
    base: int
    size: int
    readable: bool = True
    writable: bool = True
    executable: bool = True
    privileged_only: bool = False
    latency: int = 0  # extra cycles per access (added to the instruction cost)

    @property
    def end(self) -> int:
        return self.base + self.size

    def contains(self, address: int) -> bool:
        return self.base <= address < self.end


@dataclass
class XipCache:
    """A tiny direct-mapped instruction/data cache in front of flash.

    Real RP2350 has a 16 KB XIP cache. The model keeps the idea, not the
    exact geometry: ``lines`` lines of ``line_words`` words each, direct
    mapped by address. A hit costs nothing extra; a miss costs
    ``miss_penalty`` cycles and fills the line.
    """

    lines: int = 512
    line_words: int = 8
    miss_penalty: int = 20
    enabled: bool = True
    tags: Dict[int, int] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def access(self, address: int) -> int:
        """Return the extra cycles for accessing ``address`` through the cache."""
        if not self.enabled:
            self.misses += 1
            return self.miss_penalty
        line_addr = address // (4 * self.line_words)
        index = line_addr % self.lines
        if self.tags.get(index) == line_addr:
            self.hits += 1
            return 0
        self.tags[index] = line_addr
        self.misses += 1
        return self.miss_penalty

    def flush(self) -> None:
        self.tags.clear()


# Default region geometry (sizes are the real RP2350 ones, rounded).
FLASH_BASE = 0x1000_0000
FLASH_SIZE = 4 * 1024 * 1024
SRAM_BASE = 0x2000_0000
SRAM_SIZE = 520 * 1024
PERIPH_BASE = 0x4000_0000
PERIPH_SIZE = 0x1000_0000
PPB_BASE = 0xE000_0000
PPB_SIZE = 0x0010_0000

# Well-known register addresses used by the models and the programs.
# GPIO/UART/TIMER offsets are simplified (the real RP2350 splits GPIO between
# SIO and IO_BANK0); the PPB addresses (SysTick, NVIC, SCB, MPU, DWT) are the
# architectural ARMv8-M ones and match what GDB shows on the board.
GPIO_BASE = 0x4000_0000
GPIO_OUT = GPIO_BASE + 0x00        # write: pin levels (bit per pin)
GPIO_OUT_SET = GPIO_BASE + 0x04    # write: set bits
GPIO_OUT_CLR = GPIO_BASE + 0x08    # write: clear bits
GPIO_IN = GPIO_BASE + 0x0C         # read: input pins (buttons)
GPIO_INTR = GPIO_BASE + 0x10       # read: pending edge flags; write 1 to clear
UART_BASE = 0x4003_4000
UART_DR = UART_BASE + 0x00         # write: transmit a character
UART_FR = UART_BASE + 0x18         # read: flags (bit 5 TXFF, bit 7 TXFE)
TIMER_BASE = 0x4005_4000
TIMER_TIMERAWL = TIMER_BASE + 0x28  # read: free-running microsecond counter
TIMER_ALARM0 = TIMER_BASE + 0x10    # write: alarm time in microseconds
TIMER_INTR = TIMER_BASE + 0x34      # read: pending alarms; write 1 to clear

SYST_CSR = 0xE000_E010
SYST_RVR = 0xE000_E014
SYST_CVR = 0xE000_E018
NVIC_ISER = 0xE000_E100   # set-enable (bit per IRQ)
NVIC_ICER = 0xE000_E180   # clear-enable
NVIC_ISPR = 0xE000_E200   # set-pending
NVIC_ICPR = 0xE000_E280   # clear-pending
NVIC_IPR0 = 0xE000_E400   # byte per IRQ: IRQn priority is byte (n % 4) of word n // 4
SCB_ICSR = 0xE000_ED04    # bit 28: PENDSVSET, bit 26: PENDSTSET
SCB_VTOR = 0xE000_ED08
SCB_SHPR1 = 0xE000_ED18   # MemManage / BusFault / UsageFault priorities (bytes 0-2)
SCB_SHPR2 = 0xE000_ED1C   # SVCall priority (byte 3)
SCB_SHPR3 = 0xE000_ED20   # PendSV (byte 2) and SysTick (byte 3) priorities
SCB_SHCSR = 0xE000_ED24   # bits 16/17/18 enable MemManage/BusFault/UsageFault
SCB_CFSR = 0xE000_ED28
SCB_HFSR = 0xE000_ED2C
SCB_MMFAR = 0xE000_ED34
SCB_BFAR = 0xE000_ED38
MPU_CTRL = 0xE000_ED94
DWT_CTRL = 0xE000_1000
DWT_CYCCNT = 0xE000_1004
DEMCR = 0xE000_EDFC       # bit 24 TRCENA must be set before DWT counts (real hardware)


def default_map_xn(address: int) -> bool:
    """True for the execute-never parts of the ARMv8-M default memory map:
    Peripheral 0x40000000-0x5FFFFFFF, Device 0xA0000000-0xDFFFFFFF and System
    0xE0000000-0xFFFFFFFF. (Code, SRAM and RAM regions are executable.)"""
    address &= 0xFFFFFFFF
    return 0x4000_0000 <= address < 0x6000_0000 or address >= 0xA000_0000


class Memory:
    """The system bus: regions, storage, devices and the MPU hook.

    Devices (peripherals, SysTick, NVIC...) register a *register handler*
    for an address range; reads and writes inside that range are delegated
    to the device instead of the plain storage.
    """

    def __init__(self, xip_cache: Optional[XipCache] = None) -> None:
        self.regions = [
            Region("FLASH", FLASH_BASE, FLASH_SIZE, writable=False, latency=0),
            Region("SRAM", SRAM_BASE, SRAM_SIZE, latency=0),
            Region("PERIPH", PERIPH_BASE, PERIPH_SIZE, executable=False, latency=2),
            Region("PPB", PPB_BASE, PPB_SIZE, executable=False,
                   privileged_only=True, latency=1),
        ]
        self.xip_cache = xip_cache if xip_cache is not None else XipCache()
        self.storage: Dict[int, Any] = {}
        # (base, end, read_fn, write_fn) tuples for device registers
        self.devices: list = []
        # the MPU is attached by the machine; None means "no MPU"
        self.mpu: Any = None
        # per-region access counters, useful for the P10 flash-vs-SRAM report
        self.access_counts: Dict[str, int] = {r.name: 0 for r in self.regions}
        self.fault_hook: Optional[Callable[[MemoryFault], None]] = None

    # ------------------------------------------------------------------ setup
    def attach_device(self, base: int, size: int, read_fn, write_fn) -> None:
        self.devices.append((base, base + size, read_fn, write_fn))

    def region_for(self, address: int) -> Optional[Region]:
        for region in self.regions:
            if region.contains(address):
                return region
        return None

    def region_name(self, address: int) -> str:
        region = self.region_for(address)
        return region.name if region else "UNMAPPED"

    # -------------------------------------------------------------- bus checks
    def _check(self, address: int, write: bool, fetch: bool, privileged: bool) -> Region:
        if address % 4 != 0:
            raise MemoryFault("UsageFault", address,
                              "unaligned word access (the model only has 4-byte word accesses)",
                              fetch=fetch, flag="UNALIGNED")
        if fetch and default_map_xn(address):
            # Architectural rule: the Peripheral, Device and System parts of the
            # default memory map are execute-never. Fetching there is a
            # MemManage IACCVIOL even with the MPU disabled, not a bus error.
            raise MemoryFault("MemManage", address,
                              f"instruction fetch from an execute-never (XN) region of the default memory "
                              f"map (0x{address:08X})", fetch=True, flag="IACCVIOL")
        region = self.region_for(address)
        if region is None:
            raise MemoryFault("BusFault", address,
                              "address is not in any memory region (unmapped)", fetch=fetch)
        if write and not region.writable:
            raise MemoryFault("BusFault", address,
                              f"write to read-only region {region.name}", fetch=fetch)
        if fetch and not region.executable:
            raise MemoryFault("MemManage", address,
                              f"instruction fetch from execute-never region {region.name}", fetch=True,
                              flag="IACCVIOL")
        if region.privileged_only and not privileged:
            raise MemoryFault("BusFault", address,
                              f"unprivileged access to {region.name} (SysTick/NVIC/SCB/MPU registers "
                              f"are privileged only)", fetch=fetch)
        if self.mpu is not None:
            # The MPU may raise MemManage; it sees the same access attributes.
            self.mpu.check(address, privileged=privileged, write=write, fetch=fetch)
        return region

    def _latency(self, region: Region, address: int) -> int:
        cycles = region.latency
        if region.name == "FLASH":
            cycles += self.xip_cache.access(address)
        return cycles

    # ---------------------------------------------------------------- accesses
    def read(self, address: int, privileged: bool = True) -> tuple:
        """Return ``(value, extra_cycles)`` for a data read."""
        region = self._check(address, write=False, fetch=False, privileged=privileged)
        self.access_counts[region.name] += 1
        for base, end, read_fn, _ in self.devices:
            if base <= address < end:
                return read_fn(address), self._latency(region, address)
        value = self.storage.get(address, 0)
        if not isinstance(value, int):
            # Reading a code word as data: return its synthetic encoding.
            value = getattr(value, "encoding", 0)
        return value, self._latency(region, address)

    def write(self, address: int, value: Any, privileged: bool = True) -> int:
        """Store ``value`` and return the extra cycles for the access."""
        region = self._check(address, write=True, fetch=False, privileged=privileged)
        self.access_counts[region.name] += 1
        for base, end, _, write_fn in self.devices:
            if base <= address < end:
                write_fn(address, value & 0xFFFFFFFF)
                return self._latency(region, address)
        self.storage[address] = value & 0xFFFFFFFF if isinstance(value, int) else value
        return self._latency(region, address)

    def fetch(self, address: int, privileged: bool = True) -> tuple:
        """Return ``(instruction_or_word, extra_cycles)`` for an instruction fetch."""
        region = self._check(address, write=False, fetch=True, privileged=privileged)
        self.access_counts[region.name] += 1
        return self.storage.get(address, 0), self._latency(region, address)

    # --------------------------------------------------- loader-side helpers
    def load_word(self, address: int, value: Any) -> None:
        """Place a word without bus checks or cost (used by the image loader,
        i.e. the "programmer" writing flash before power-on)."""
        self.storage[address] = value

    def peek(self, address: int) -> Any:
        """Read without side effects, checks or cost (debugger view)."""
        for base, end, read_fn, _ in self.devices:
            if base <= address < end:
                return read_fn(address)
        return self.storage.get(address, 0)

    def poke(self, address: int, value: Any) -> None:
        """Write without checks (debugger / fault injection)."""
        self.storage[address] = value
