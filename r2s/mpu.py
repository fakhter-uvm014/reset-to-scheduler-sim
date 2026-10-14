"""Memory Protection Unit: regions, permissions and stack guards.

The ARMv8-M MPU describes each region by a base address and a limit address
(RBAR / RLAR) with a 32-byte granularity, plus attributes. (ARMv7-M used
power-of-two sized, size-aligned regions instead; many older tutorials show
that format.) The model keeps base/limit and the 32-byte granularity, with a
simpler permission vocabulary:

    access = "rw" | "ro" | "none"   separately for privileged and unprivileged

``"none"`` is a documented simplification: the ARMv8-M AP field encodes only
read-write or read-only, each privileged-only or any privilege; there is no
"no access" setting (ARMv7-M had one).

Rules modelled
--------------
* When the MPU is disabled every access follows the default memory map.
* Regions must start and end on a 32-byte boundary (checked in
  :meth:`Mpu.add_region`), as RBAR/RLAR require.
* When enabled, an access that hits **no** region is allowed for privileged
  code if PRIVDEFENA is set (the "background region"), and is a
  **MemManage** fault for unprivileged code (or for privileged code without
  PRIVDEFENA). This is what isolates an unprivileged task: it can only touch
  the regions the kernel opened for it.
* An access that hits **more than one** enabled region is a MemManage fault
  on ARMv8-M (ARMv7-M let the highest-numbered region win). The kernel in
  this repo therefore never overlaps regions.
* An access inside one region is checked against the region permissions for
  the current privilege level; a violation is a MemManage fault. For data
  accesses the faulting address goes to MMFAR (MMARVALID); for instruction
  fetches it does not (IACCVIOL).
* The PPB (SysTick/NVIC/SCB/MPU registers) is not governed by MPU regions;
  privileged code always reaches it, unprivileged code gets a BusFault from
  the bus (see :mod:`r2s.memory`).
* **Stack guard**: a small ``"none"/"none"`` region placed just below each
  task stack. An overflowing task touches the guard first and faults
  *before* corrupting its neighbour, with an unmistakable name in the fault
  record. That is the whole trick behind scenario P07 versus P09.
  Simplification: on real Armv8-M the guard would be AP = read-only,
  privileged only, plus XN. That faults any unprivileged access and any
  write (all a stack overflow needs) while privileged reads still succeed;
  the model's "none" also blocks those privileged reads. The Cortex-M33's
  stack-limit registers PSPLIM/MSPLIM (UsageFault STKOF) are the
  alternative; the model uses the MPU guard because the course does.

The MPU sits in the bus path: :meth:`Mpu.check` is called by
:class:`r2s.memory.Memory` on every access.

Author: Farzana Akhter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import memory as mm

GRANULE = 32


@dataclass
class MpuRegion:
    name: str
    base: int
    size: int
    priv: str = "rw"      # "rw" | "ro" | "none"
    unpriv: str = "none"  # "rw" | "ro" | "none"
    guard: bool = False   # purely informational: marks a stack guard
    enabled: bool = True

    @property
    def end(self) -> int:
        return self.base + self.size

    def contains(self, address: int) -> bool:
        return self.enabled and self.base <= address < self.end

    def to_dict(self) -> dict:
        return {"name": self.name, "base": f"0x{self.base:08X}",
                "limit": f"0x{(self.end - 1) & 0xFFFFFFFF:08X}", "size": self.size,
                "priv": self.priv, "unpriv": self.unpriv, "guard": self.guard, "enabled": self.enabled}


@dataclass
class Mpu:
    trace: object = None
    enabled: bool = False
    privdefena: bool = True
    regions: List[MpuRegion] = field(default_factory=list)
    mmfar: int = 0
    violations: int = 0

    def attach(self, memory: mm.Memory) -> None:
        memory.mpu = self
        memory.attach_device(mm.MPU_CTRL, 4, self._read, self._write)

    def _read(self, address: int) -> int:
        return int(self.enabled) | (int(self.privdefena) << 2)

    def _write(self, address: int, value: int) -> None:
        self.enabled = bool(value & 1)
        self.privdefena = bool(value & 4)
        if self.trace is not None:
            self.trace.emit("mpu", "mpu", f"MPU {'enabled' if self.enabled else 'disabled'} "
                            f"(PRIVDEFENA={int(self.privdefena)}, {len(self.regions)} regions)",
                            regions=[r.to_dict() for r in self.regions])

    @staticmethod
    def validate(region: MpuRegion) -> None:
        if region.enabled and (region.base % GRANULE or region.size % GRANULE or region.size == 0):
            raise ValueError(f"MPU region '{region.name}' must start and end on a {GRANULE}-byte boundary "
                             f"(base=0x{region.base:08X}, size={region.size})")

    def add_region(self, region: MpuRegion) -> None:
        self.validate(region)
        self.regions.append(region)

    def set_region(self, index: int, base: int, size: int) -> None:
        """Reprogram one region (what the kernel does for the running task's stack)."""
        region = self.regions[index]
        region.base, region.size, region.enabled = base, size, True
        self.validate(region)

    def hits(self, address: int) -> List[MpuRegion]:
        return [r for r in self.regions if r.contains(address)]

    def region_for(self, address: int) -> Optional[MpuRegion]:
        hits = self.hits(address)
        return hits[0] if len(hits) == 1 else None

    def check(self, address: int, privileged: bool, write: bool, fetch: bool) -> None:
        if not self.enabled:
            return
        if mm.PPB_BASE <= address < mm.PPB_BASE + mm.PPB_SIZE:
            return  # the PPB is outside MPU control; privilege is checked by the bus
        hits = self.hits(address)
        if len(hits) > 1:
            self._fault(address, fetch, "address matches more than one MPU region "
                        f"({', '.join(r.name for r in hits)}); ARMv8-M treats overlap as a fault")
        if not hits:
            if privileged and self.privdefena:
                return
            self._fault(address, fetch,
                        f"no MPU region covers this address and the access is "
                        f"{'unprivileged' if not privileged else 'privileged without PRIVDEFENA'} "
                        f"(background map not allowed)")
        region = hits[0]
        perm = region.priv if privileged else region.unpriv
        what = "write" if write else ("fetch" if fetch else "read")
        if perm == "none" or (perm == "ro" and write):
            kind = "stack guard" if region.guard else "region"
            self._fault(address, fetch,
                        f"{what} not permitted in {kind} '{region.name}' "
                        f"({'privileged' if privileged else 'unprivileged'} access, perm={perm})")

    def _fault(self, address: int, fetch: bool, reason: str) -> None:
        if not fetch:
            self.mmfar = address   # MMFAR is only valid for data accesses (MMARVALID)
        self.violations += 1
        raise mm.MemoryFault("MemManage", address, reason, fetch=fetch)
