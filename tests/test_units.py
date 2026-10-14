"""Unit tests for the building blocks: assembler, instruction execution and
flags, exception entry/return frame layout, tail-chaining, fault records and
escalation, MPU, NVIC priority packing, SVC, boot, determinism.

Each docstring gives Test ID / Purpose / Stimulus / Expected result.

Author: Farzana Akhter
"""

from __future__ import annotations

import json

import pytest

from r2s import memory as mm
from r2s.boot import DEFAULT_HANDLER_SOURCE, Image, reset_handler_source
from r2s.cpu import (EXC_RETURN_HANDLER_MSP, EXC_RETURN_THREAD_MSP, EXC_RETURN_THREAD_PSP, FRAME_WORDS,
                     XPSR_ALIGN_PAD, decode_cfsr)
from r2s.engine import Machine, MachineConfig, Stimulus
from r2s.isa import AssemblyError, Literal, assemble, disassemble
from r2s.isa_profiles import get_profile
from r2s.memory import Memory, MemoryFault
from r2s.mpu import Mpu, MpuRegion
from r2s.nvic import (EXC_HARDFAULT, EXC_MEMMANAGE, EXC_PENDSV, EXC_RESET, EXC_SVCALL, EXC_SYSTICK,
                      EXC_USAGEFAULT, IRQ_BASE, THREAD_PRIORITY, Nvic, exc_name)
from r2s.programs import build_blink, build_kernel
from r2s.scenarios import SCENARIO_DIR, find_scenario, load_scenario, run_scenario, save_result
from conftest import assert_sequence


# ---------------------------------------------------------------- helpers
def small_machine(source: str, handlers=None, max_cycles=20000, **cfg) -> Machine:
    """A machine with the standard startup code + the given main source."""
    image = Image()
    image.add_section("startup", reset_handler_source())
    image.add_section("default_handler", DEFAULT_HANDLER_SOURCE)
    image.set_handler(EXC_RESET, "Reset_Handler")
    image.add_section("main", source)
    for number, label in (handlers or {}).items():
        image.set_handler(number, label)
    m = Machine(image, MachineConfig(max_cycles=max_cycles, **cfg))
    m.power_on()
    return m


def run_main(source: str, **kw) -> Machine:
    m = small_machine(source, **kw)
    m.run()
    return m


# ------------------------------------------------------------ assembler
def test_assembler_parses_operand_forms():
    """Test ID: U01
    Purpose: the assembler handles register lists, memory operands, literal loads and comments.
    Stimulus: a four-line program with push, ldr [rn, #off], ldr =label and b.
    Expected result: canonical text, ordered register list, Literal operand, comment kept, labels resolved.
    """
    p = assemble("""
    start:
        push {r7, r4-r6, lr}        ; prologue
        ldr  r0, [r1, #8]
        ldr  r2, =start
        b    start
    """)
    assert p.size_words == 4
    assert p.instructions[0].args[0].regs == ("r4", "r5", "r6", "r7", "lr")
    assert p.instructions[0].comment == "prologue"
    assert p.instructions[1].args[1].base == "r1" and p.instructions[1].args[1].offset == 8
    assert isinstance(p.instructions[2].args[1], Literal)
    assert p.instructions[1].text == "ldr r0, [r1, #8]"
    listing = disassemble(p, 0x10000100, comments=True)
    assert "10000100 <start>:" in listing and "; prologue" in listing


def test_assembler_rejects_bad_input():
    """Test ID: U02
    Purpose: typos and non-Thumb syntax are caught at assembly/link time, not at run time.
    Stimulus: unknown mnemonic, '=value' outside ldr, wrong operand count, undefined symbol at link.
    Expected result: AssemblyError each time.
    """
    for bad in ("movx r0, #1", "mov r0, =start", "add r0, r1", "str r0, =x"):
        with pytest.raises(AssemblyError):
            assemble(bad)
    image = Image()
    image.add_section("startup", reset_handler_source())
    image.add_section("default_handler", DEFAULT_HANDLER_SOURCE)
    image.set_handler(EXC_RESET, "Reset_Handler")
    image.add_section("main", "main:\n ldr r0, =no_such_symbol\n halt")
    with pytest.raises(AssemblyError, match="undefined reference"):
        Machine(image, MachineConfig())


# ------------------------------------------------------ execute and flags
@pytest.mark.parametrize("a,b,op,n,z,c,v", [
    (5, 5, "subs", 0, 1, 1, 0),                    # equal: Z=1, no borrow -> C=1
    (3, 5, "subs", 1, 0, 0, 0),                    # borrow -> C=0, negative
    (0x80000000, 1, "subs", 0, 0, 1, 1),           # INT_MIN - 1 overflows
    (0xFFFFFFFF, 1, "adds", 0, 1, 1, 0),           # carry out, result 0
    (0x7FFFFFFF, 1, "adds", 1, 0, 0, 1),           # INT_MAX + 1 overflows
])
def test_flags_match_arm_rules(a, b, op, n, z, c, v):
    """Test ID: U03
    Purpose: adds/subs set N Z C V like Thumb-2 (C is NOT borrow for subtraction).
    Stimulus: boundary operand pairs.
    Expected result: the flag values in the parameter table.
    """
    m = run_main(f"main:\n ldr r0, ={a}\n ldr r1, ={b}\n {op} r2, r0, r1\n halt")
    f = m.cpu.flags
    assert (int(f["N"]), int(f["Z"]), int(f["C"]), int(f["V"])) == (n, z, c, v)


def test_signed_conditional_branches_and_alu():
    """Test ID: U04
    Purpose: blt/bge use N != V (signed), and the ALU ops compute the right values.
    Stimulus: compare -1 with 1, then a few ALU operations.
    Expected result: -1 < 1 taken as signed; mul/eor/lsl/lsr/orr/and results correct.
    """
    m = run_main("""
    main:
        ldr  r0, =0xFFFFFFFF
        cmp  r0, #1
        blt  less
        mov  r7, #0
        b    alu
    less:
        mov  r7, #1
    alu:
        mov  r1, #6
        mov  r2, #7
        mul  r3, r1, r2
        eor  r4, r3, #0xFF
        lsl  r5, r1, #4
        lsr  r6, r5, #2
        orr  r8, r1, #0x100
        and  r9, r8, #0xF0F
        halt
    """)
    r = m.cpu.regs
    assert r["r7"] == 1
    assert (r["r3"], r["r4"], r["r5"], r["r6"], r["r8"], r["r9"]) == (42, 42 ^ 0xFF, 96, 24, 0x106, 0x106)


def test_push_pop_order_and_bl_thumb_bit():
    """Test ID: U05
    Purpose: push stores the lowest register at the lowest address; bl sets LR = return | 1;
             pop {pc} returns through the Thumb bit.
    Stimulus: a call to a function that pushes {r4, r5, lr} and pops {r4, r5, pc}.
    Expected result: stack words in register order, LR odd, execution continues after the call.
    """
    seen = {}

    def peek(cpu):
        seen["sp"] = cpu.msp
        seen["words"] = [cpu.memory.peek(cpu.msp + 4 * i) for i in range(3)]
        seen["lr"] = cpu.lr
    m = small_machine("""
    main:
        mov  r4, #0x44
        mov  r5, #0x55
        bl   func
        mov  r9, #9
        halt
    func:
        push {r4, r5, lr}
        kcall "test:peek"
        pop  {r4, r5, pc}
    """)
    m.cpu.kcalls["test:peek"] = peek
    m.run()
    assert seen["words"][:2] == [0x44, 0x55]
    assert seen["lr"] & 1 and seen["words"][2] == seen["lr"]
    assert m.cpu.regs["r9"] == 9 and m.cpu.thumb


# ------------------------------------------------------------ memory / bus
def test_bus_faults_and_ppb_privilege():
    """Test ID: U06
    Purpose: the bus faults on unmapped addresses and flash writes; the PPB is privileged only;
             unaligned word accesses are UsageFault UNALIGNED.
    Stimulus: direct Memory accesses.
    Expected result: BusFault / BusFault / BusFault (unprivileged SysTick) / UsageFault.
    """
    mem = Memory()
    with pytest.raises(MemoryFault) as exc:
        mem.read(0x0000_0000)
    assert exc.value.kind == "BusFault"
    with pytest.raises(MemoryFault):
        mem.write(mm.FLASH_BASE, 1)
    mem.read(mm.SYST_CSR, privileged=True)
    with pytest.raises(MemoryFault) as exc:
        mem.read(mm.SYST_CSR, privileged=False)
    assert exc.value.kind == "BusFault"
    with pytest.raises(MemoryFault) as exc:
        mem.read(mm.SRAM_BASE + 2)
    assert exc.value.kind == "UsageFault" and exc.value.flag == "UNALIGNED"


def test_xip_cache_hit_miss_costs():
    """Test ID: U07
    Purpose: the first flash access to a cache line misses, the next one hits.
    Stimulus: two reads of the same flash word.
    Expected result: miss penalty then 0 extra cycles; counters 1 miss, 1 hit.
    """
    mem = Memory()
    mem.load_word(mm.FLASH_BASE + 0x100, 7)
    _, first = mem.read(mm.FLASH_BASE + 0x100)
    _, second = mem.read(mm.FLASH_BASE + 0x100)
    assert first == mem.xip_cache.miss_penalty and second == 0
    assert mem.xip_cache.misses == 1 and mem.xip_cache.hits == 1


# ------------------------------------------------------------ exceptions
def test_exception_frame_layout_matches_cortex_m():
    """Test ID: U08
    Purpose: entry pushes r0-r3, r12, lr, pc, xPSR with r0 at the lowest address, sets LR to
             EXC_RETURN and IPSR; return restores every register and the SP.
    Stimulus: svc from main with known values in r0-r3, r12 and r4.
    Expected result: frame order r0..xPSR, stacked PC = instruction after svc, Thumb bit set,
                     LR = 0xFFFFFFF9, SP back to the top after return.
    """
    seen = {}

    def inspect(cpu):
        seen.update(sp=cpu.msp, lr=cpu.lr, mode=(cpu.in_handler, cpu.privileged, cpu.ipsr),
                    frame=[cpu.memory.peek(cpu.msp + 4 * i) for i in range(8)])
    m = small_machine("""
    main:
        mov  r0, #0x11
        mov  r1, #0x22
        mov  r2, #0x33
        mov  r3, #0x44
        mov  r12, #0x55
        mov  r4, #0x99
        svc  #7
        halt
    SVC_Handler:
        kcall "test:inspect"
        bx   lr
    """, {EXC_SVCALL: "SVC_Handler"})
    m.cpu.kcalls["test:inspect"] = inspect
    s = m.run()
    assert s["faults"] == []
    r0, r1, r2, r3, r12, lr, pc, xpsr = seen["frame"]
    assert (r0, r1, r2, r3, r12) == (0x11, 0x22, 0x33, 0x44, 0x55)
    assert xpsr & (1 << 24) and not xpsr & XPSR_ALIGN_PAD
    assert pc == m.image.symbols["main"] + 4 * 7
    assert seen["lr"] == EXC_RETURN_THREAD_MSP
    assert seen["mode"] == (True, True, EXC_SVCALL)
    assert seen["sp"] == m.image.initial_sp - 32
    assert m.cpu.regs["r4"] == 0x99 and m.cpu.regs["r12"] == 0x55 and m.cpu.msp == m.image.initial_sp
    entry = [e for e in m.trace.events if e.kind == "exception_entry"][0]
    assert list(entry.details["stacked_frame"].keys()) == list(FRAME_WORDS)


def test_frame_alignment_padding():
    """Test ID: U09
    Purpose: ARMv8-M keeps the exception frame 8-byte aligned: a misaligned SP gets a pad word,
             recorded in stacked xPSR bit 9 and removed again on return.
    Stimulus: push one register (SP misaligned by 4), then svc.
    Expected result: frame at SP-4-32, xPSR bit 9 set, SP restored exactly after return.
    """
    seen = {}

    def inspect(cpu):
        seen.update(frame_at=cpu.msp, xpsr=cpu.memory.peek(cpu.msp + 28))
    m = small_machine("""
    main:
        push {r4}
        mrs  r5, msp
        svc  #1
        mrs  r6, msp
        halt
    SVC_Handler:
        kcall "test:inspect"
        bx   lr
    """, {EXC_SVCALL: "SVC_Handler"})
    m.cpu.kcalls["test:inspect"] = inspect
    m.run()
    sp_before = m.cpu.regs["r5"]
    assert sp_before % 8 == 4
    assert seen["frame_at"] == sp_before - 4 - 32
    assert seen["xpsr"] & XPSR_ALIGN_PAD
    assert m.cpu.regs["r6"] == sp_before


def test_tail_chain_when_another_exception_pending():
    """Test ID: U10
    Purpose: a handler returning while another exception is pending tail-chains: no unstack/restack,
             LR reloaded with the same EXC_RETURN, and the next handler starts 6 cycles later.
    Stimulus: pend IRQ0 and IRQ1 (equal priority) at once from main through NVIC_ISPR.
    Expected result: IRQ0 entry, tail_chain into IRQ1 from IRQ0's context, one exception return,
                     one frame push in total.
    """
    m = run_main("""
    main:
        ldr  r0, =NVIC_ISER
        mov  r1, #3
        str  r1, [r0]
        ldr  r0, =NVIC_ISPR
        mov  r1, #3
        str  r1, [r0]          ; pend IRQ0 and IRQ1 at once
        nop
        halt
    H0:
        mark "h0"
        bx   lr
    H1:
        mark "h1"
        bx   lr
    """, handlers={IRQ_BASE + 0: "H0", IRQ_BASE + 1: "H1"})
    s = m.summary()
    assert s["faults"] == [] and s["tail_chains"] == 1
    irq0, irq1 = f"ISR:{exc_name(IRQ_BASE)}", f"ISR:{exc_name(IRQ_BASE + 1)}"
    ev = m.trace.as_dicts()
    assert assert_sequence(ev, [f"exception_entry:{irq0}", "marker", f"exception_return:{irq1}",
                                f"tail_chain:{irq1}", "marker", "exception_return:main"])
    tail = next(e for e in ev if e["kind"] == "tail_chain")
    ret = next(e for e in ev if e["kind"] == "exception_return")
    assert tail["from"] == irq0 and tail["details"]["exc_return"] == f"0x{EXC_RETURN_THREAD_MSP:08X}"
    assert "stacked_frame" not in tail["details"]
    assert tail["t_cycles"] - ret["t_cycles"] == get_profile("arm_like").tail_chain_cycles
    assert len([e for e in ev if e["kind"] == "exception_return" and "popped_frame" in e["details"]]) == 1


def test_preemption_uses_handler_exc_return():
    """Test ID: U11
    Purpose: a more urgent exception that pre-empts a handler gets EXC_RETURN 0xFFFFFFF1 and its
             frame on the MSP holds the interrupted handler's state.
    Stimulus: IRQ1 (priority 0x80) running when IRQ0 (priority 0x40) is pended from inside it.
    Expected result: nested entry from ISR context with LR=0xFFFFFFF1, return to handler mode.
    """
    m = run_main("""
    main:
        ldr  r0, =NVIC_IPR0
        ldr  r1, =0x8040       ; IRQ0 = 0x40 (byte 0), IRQ1 = 0x80 (byte 1)
        str  r1, [r0]
        ldr  r0, =NVIC_ISER
        mov  r1, #3
        str  r1, [r0]
        ldr  r0, =NVIC_ISPR
        mov  r1, #2
        str  r1, [r0]          ; pend IRQ1 only
        halt
    H1:
        ldr  r0, =NVIC_ISPR
        mov  r1, #1
        str  r1, [r0]          ; pend the more urgent IRQ0 from inside IRQ1
        nop
        bx   lr
    H0:
        bx   lr
    """, handlers={IRQ_BASE + 0: "H0", IRQ_BASE + 1: "H1"})
    ev = m.trace.as_dicts()
    nested = [e for e in ev if e["kind"] == "exception_entry" and e["details"]["exception"] == IRQ_BASE]
    assert nested and nested[0]["from"] == f"ISR:{exc_name(IRQ_BASE + 1)}"
    assert nested[0]["details"]["exc_return"] == f"0x{EXC_RETURN_HANDLER_MSP:08X}"
    back = [e for e in ev if e["kind"] == "exception_return" and e["details"]["exception"] == IRQ_BASE]
    assert "handler mode" in back[0]["reason"]


# ----------------------------------------------------------------- faults
def test_fault_record_has_stacked_pc_and_reason():
    """Test ID: U12
    Purpose: a data access fault records the faulting address, the stacked PC of the faulting
             instruction and a reason derived from the CFSR bits it set.
    Stimulus: ldr from an unmapped address with BusFault disabled (reset state).
    Expected result: HardFault escalated from BusFault; PRECISERR + BFARVALID; BFAR = address;
                     stacked PC = faulting ldr; CFSR=0x00008200, HFSR.FORCED.
    """
    m = run_main("""
    main:
        ldr  r0, =0x12345678
        ldr  r1, [r0]
        halt
    Fault_Handler:
        halt
    """, handlers={EXC_HARDFAULT: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["kind"] == "HardFault" and f["escalated_from"] == "BusFault"
    assert f["flag"] == "PRECISERR" and f["reason"].startswith("PRECISERR:")
    assert f["address"] == "0x12345678" and f["bfar"] == "0x12345678"
    assert f["instruction"] == "ldr r1, [r0]"
    assert f["stacked_frame"]["pc"] == f["pc"] == f"0x{m.image.symbols['main'] + 4:08X}"
    assert f["cfsr"] == {"BFSR": ["PRECISERR", "BFARVALID"], "HFSR": ["FORCED"]}
    assert f["cfsr_value"] == "0x00008200" and f["hfsr_value"] == "0x40000000"
    assert [b["name"] for b in f["decoded"]] == ["PRECISERR", "BFARVALID", "FORCED"]
    assert m.cpu.memory.peek(mm.SCB_CFSR) == 0x8200


def test_fetch_fault_is_ibuserr_and_cfsr_is_write_one_to_clear():
    """Test ID: U13
    Purpose: an instruction fetch from unmapped memory is IBUSERR (BFAR not valid); CFSR bits are
             sticky and cleared by writing 1s.
    Stimulus: bx to an odd unmapped address; the fault handler clears CFSR.
    Expected result: IBUSERR, no BFARVALID, stacked PC = the bad target; CFSR reads 0 afterwards.
    """
    m = run_main("""
    main:
        ldr  r0, =0x30000001
        bx   r0
    Fault_Handler:
        ldr  r0, =SCB_CFSR
        ldr  r1, [r0]
        str  r1, [r0]          ; write the set bits back: write-1-to-clear
        ldr  r2, [r0]
        halt
    """, handlers={EXC_HARDFAULT: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["flag"] == "IBUSERR" and f["bfar"] is None and f["pc"] == "0x30000000"
    assert m.cpu.regs["r1"] == 1 << 8 and m.cpu.regs["r2"] == 0


def test_fetch_from_execute_never_region_is_iaccviol():
    """Test ID: U13b
    Purpose: the Peripheral and System parts of the default memory map are execute-never: a fetch
             there is MemManage IACCVIOL (MMFAR not valid) even with the MPU off, not a bus error.
    Stimulus: bx to 0x40000001 (peripheral space) with MemManage disabled (reset state).
    Expected result: HardFault escalated from MemManage, flag IACCVIOL, no MMFAR, CFSR=0x00000001.
    """
    m = run_main("""
    main:
        ldr  r0, =0x40000001
        bx   r0
    Fault_Handler:
        halt
    """, handlers={EXC_HARDFAULT: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["flag"] == "IACCVIOL" and f["escalated_from"] == "MemManage"
    assert f["mmfar"] is None and f["cfsr_value"] == "0x00000001" and f["pc"] == "0x40000000"


def test_branch_without_thumb_bit_is_invstate():
    """Test ID: U14
    Purpose: bx to an even address clears xPSR.T; the next instruction faults with UsageFault INVSTATE.
    Stimulus: bx to the (even) address of a label.
    Expected result: INVSTATE escalated to HardFault, stacked PC = target, stacked xPSR.T = 0.
    """
    m = run_main("""
    main:
        ldr  r0, =target
        bx   r0
    target:
        halt
    Fault_Handler:
        halt
    """, handlers={EXC_HARDFAULT: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["flag"] == "INVSTATE" and f["escalated_from"] == "UsageFault"
    assert int(f["stacked_frame"]["pc"], 16) == m.image.symbols["target"]
    assert int(f["stacked_frame"]["xpsr"], 16) & (1 << 24) == 0


def test_escalation_rules_and_lockup():
    """Test ID: U15
    Purpose: an enabled MemManage still escalates when PRIMASK blocks it; a fault inside HardFault
             locks the core up.
    Stimulus: enable MemManage in SHCSR, set PRIMASK, read a no-access MPU region; HardFault handler
              then reads an unmapped address.
    Expected result: HardFault (FORCED) from MemManage DACCVIOL with MMFAR, then a lockup.
    """
    m = small_machine("""
    main:
        cpsid i
        ldr  r0, =0x20001000
        ldr  r1, [r0]
        halt
    Fault_Handler:
        ldr  r0, =0x0
        ldr  r1, [r0]          ; a fault in HardFault: lockup
        halt
    """, {EXC_HARDFAULT: "Fault_Handler"})
    m.nvic.memmanage_enabled = True
    m.mpu.enabled = True
    m.mpu.add_region(MpuRegion("forbidden", 0x2000_1000, 32, "none", "none"))
    s = m.run()
    first = s["faults"][0]
    assert first["kind"] == "HardFault" and first["escalated_from"] == "MemManage"
    assert first["flag"] == "DACCVIOL" and first["mmfar"] == "0x20001000"
    assert s["lockup"] is True and "LOCKUP" in s["stop_reason"]


def test_svc_with_primask_set_escalates_to_hardfault():
    """Test ID: U16
    Purpose: SVC must be taken immediately; with PRIMASK=1 it cannot, so it becomes a HardFault.
    Stimulus: cpsid i then svc #0.
    Expected result: one HardFault with HFSR.FORCED and the stacked PC at the svc instruction.
    """
    m = run_main("""
    main:
        cpsid i
        svc  #0
        halt
    Fault_Handler:
        halt
    """, handlers={EXC_HARDFAULT: "Fault_Handler", EXC_SVCALL: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["kind"] == "HardFault" and f["flag"] == "FORCED"
    assert f["pc"] == f"0x{m.image.symbols['main'] + 4:08X}"


def test_unprivileged_cpsid_is_ignored():
    """Test ID: U17
    Purpose: cpsid/msr from unprivileged thread mode have no effect (as on the real core) and the
             trace says so.
    Stimulus: drop privilege with msr control, then cpsid i and msr control, 0.
    Expected result: PRIMASK stays 0, CONTROL stays 1, two 'ignored' privilege events, no fault.
    """
    m = run_main("""
    main:
        mov  r0, #1
        msr  control, r0       ; nPRIV = 1
        cpsid i
        mov  r0, #0
        msr  control, r0       ; trying to get privilege back: ignored
        halt
    """)
    assert m.nvic.primask is False and m.cpu.control == 1
    ignored = [e for e in m.trace.events if e.kind == "privilege" and "ignored" in e.reason]
    assert len(ignored) == 2 and m.summary()["faults"] == []


def test_bad_exc_return_is_invpc():
    """Test ID: U18
    Purpose: returning from a handler with an invalid EXC_RETURN is a UsageFault INVPC.
    Stimulus: the SVC handler overwrites LR with 0xFFFFFFF5 and executes bx lr.
    Expected result: INVPC recorded, escalated to HardFault (UsageFault disabled).
    """
    m = run_main("""
    main:
        svc  #0
        halt
    SVC_Handler:
        ldr  r0, =0xFFFFFFF5
        mov  lr, r0
        bx   lr
    Fault_Handler:
        halt
    """, handlers={EXC_SVCALL: "SVC_Handler", EXC_HARDFAULT: "Fault_Handler"})
    f = m.summary()["faults"][0]
    assert f["flag"] == "INVPC" and f["kind"] == "HardFault"


def test_decode_cfsr_bit_positions():
    """Test ID: U19
    Purpose: CFSR/HFSR bit positions follow the architecture (MMFSR 7:0, BFSR 15:8, UFSR 31:16).
    Stimulus: decode a few known register values.
    Expected result: the right names.
    """
    names = lambda c, h=0: [b["name"] for b in decode_cfsr(c, h)]  # noqa: E731
    assert names(0x82) == ["DACCVIOL", "MMARVALID"]
    assert names(0x1200) == ["PRECISERR", "STKERR"]
    assert names(0x00020000, 0x40000000) == ["INVSTATE", "FORCED"]
    assert names(0x00040000) == ["INVPC"]


# ------------------------------------------------------------------ nvic
def test_nvic_priority_ordering_and_pending():
    """Test ID: U20
    Purpose: lower number wins; equal priority -> lower exception number; same level cannot pre-empt;
             PRIMASK blocks configurable exceptions but not HardFault.
    Stimulus: direct Nvic manipulation.
    Expected result: the ordering in the assertions.
    """
    n = Nvic()
    n.enable_irq(1), n.enable_irq(2)
    n.set_priority(IRQ_BASE + 1, 0x80)
    n.set_priority(IRQ_BASE + 2, 0x40)
    n.set_pending(IRQ_BASE + 1)
    n.set_pending(IRQ_BASE + 2)
    assert n.execution_priority() == THREAD_PRIORITY
    assert n.next_exception() == IRQ_BASE + 2
    n.set_priority(IRQ_BASE + 2, 0x80)
    assert n.next_exception() == IRQ_BASE + 1
    n.active.append(IRQ_BASE + 1)
    n.pending.pop(IRQ_BASE + 1)
    assert n.next_exception() is None
    n.set_priority(IRQ_BASE + 2, 0x10)
    assert n.next_exception() == IRQ_BASE + 2
    n.primask = True
    assert n.next_exception() is None
    n.set_pending(EXC_HARDFAULT)
    assert n.next_exception() == EXC_HARDFAULT


def test_nvic_ipr_is_byte_packed_with_four_priority_bits():
    """Test ID: U21
    Purpose: NVIC_IPR holds one priority byte per IRQ (a word write sets four) and only the top
             4 bits are implemented; SHPR3 holds PendSV (byte 2) and SysTick (byte 3).
    Stimulus: word writes to NVIC_IPR5 and SHPR3 through the bus.
    Expected result: IRQ20..23 = 0x10, 0x40, 0x80, 0xF0; 0x41 reads back 0x40; PendSV 0xF0, SysTick 0x80.
    """
    m = small_machine("main:\n halt")
    m.memory.write(mm.NVIC_IPR0 + 20, 0xFF804110)
    pr = [m.nvic.get_priority(IRQ_BASE + i) for i in (20, 21, 22, 23)]
    assert pr == [0x10, 0x40, 0x80, 0xF0]
    assert m.memory.read(mm.NVIC_IPR0 + 20)[0] == 0xF0804010
    m.memory.write(mm.SCB_SHPR3, 0x80FF0000)
    assert m.nvic.get_priority(EXC_PENDSV) == 0xF0 and m.nvic.get_priority(EXC_SYSTICK) == 0x80


# ------------------------------------------------------------------- mpu
def test_mpu_region_rules():
    """Test ID: U22
    Purpose: ARMv8-M MPU rules: 32-byte granularity, guard regions, read-only regions, background
             map only for privileged code with PRIVDEFENA, and overlap is a fault.
    Stimulus: direct Mpu checks.
    Expected result: the faults and passes in the assertions.
    """
    mpu = Mpu()
    mpu.enabled = True
    with pytest.raises(ValueError):
        mpu.add_region(MpuRegion("odd", 0x2000_0004, 32))
    mpu.add_region(MpuRegion("code", 0x1000_0000, 0x1000, "ro", "ro"))
    mpu.add_region(MpuRegion("guard", 0x2000_0000, 32, "none", "none", guard=True))
    mpu.add_region(MpuRegion("stack", 0x2000_0020, 256, "rw", "rw"))
    mpu.check(0x2000_0040, privileged=False, write=True, fetch=False)
    with pytest.raises(MemoryFault) as exc:
        mpu.check(0x2000_0010, privileged=False, write=True, fetch=False)
    assert exc.value.kind == "MemManage" and "stack guard" in exc.value.reason
    with pytest.raises(MemoryFault):
        mpu.check(0x1000_0004, privileged=False, write=True, fetch=False)
    mpu.check(0x1000_0004, privileged=False, write=False, fetch=True)
    mpu.check(0x2001_0000, privileged=True, write=True, fetch=False)
    with pytest.raises(MemoryFault):
        mpu.check(0x2001_0000, privileged=False, write=False, fetch=False)
    mpu.add_region(MpuRegion("overlap", 0x2000_0020, 32, "rw", "rw"))
    with pytest.raises(MemoryFault, match="more than one"):
        mpu.check(0x2000_0020, privileged=True, write=False, fetch=False)


# ------------------------------------------------------------------ boot
def test_boot_steps_in_order_and_linker_layout():
    """Test ID: U23
    Purpose: link() places .data/.bss/.text where the docs say, vector entries carry the Thumb bit,
             and the boot monitor emits the six steps in order.
    Stimulus: the blink program, healthy.
    Expected result: layout addresses, odd vectors, six ok steps, .data copied and .bss zeroed.
    """
    image = Image()
    build_blink(image, {})
    m = Machine(image, MachineConfig(max_cycles=20000))
    m.power_on()
    m.run()
    img = m.image
    assert img.vector_base == mm.FLASH_BASE and img.section("startup").base == mm.FLASH_BASE + 0x100
    assert img.data_start == mm.SRAM_BASE and img.bss_start == img.data_end
    assert img.data_load > img.section("main").base
    assert m.memory.peek(mm.FLASH_BASE + 4) == img.symbols["Reset_Handler"] | 1
    assert [b["step"] for b in m.boot.steps] == ["power_on", "vector_table", "reset_handler",
                                                 "data_copied", "bss_zeroed", "main_entered"]
    assert all(b["ok"] for b in m.boot.steps)
    assert m.memory.peek(img.symbols["greeting_len"]) == 7 and m.memory.peek(img.symbols["counter"]) == 1


def test_boot_reset_vector_into_data_is_undefinstr():
    """Test ID: U24
    Purpose: a reset vector that points at data (with the Thumb bit) is not special-cased: the
             first fetch finds no instruction and raises UNDEFINSTR, escalated to HardFault.
    Stimulus: blink program with bad_reset_vector = 0x20000001 (SRAM garbage).
    Expected result: vector_table step flagged; HardFault from UsageFault UNDEFINSTR at 0x20000000.
    """
    image = Image()
    build_blink(image, {})
    m = Machine(image, MachineConfig(max_cycles=2000), faults=[{"type": "bad_reset_vector", "value": 0x2000_0001}])
    m.power_on()
    s = m.run()
    step = next(b for b in s["boot_steps"] if b["step"] == "vector_table")
    assert not step["ok"] and any("does not point at code" in p for p in step["problems"])
    assert s["faults"][0]["flag"] == "UNDEFINSTR" and s["faults"][0]["pc"] == "0x20000000"


# --------------------------------------------------------- kernel / svc
def _kernel(options, max_cycles=60000):
    image = Image()
    kernel = build_kernel(image, options)
    m = Machine(image, MachineConfig(max_cycles=max_cycles))
    kernel.attach(m)
    m.power_on()
    return m, m.run()


def test_context_switch_preserves_callee_saved_registers():
    """Test ID: U25
    Purpose: a full r4-r11 PendSV frame keeps every task's registers across A->B->C->A.
    Stimulus: kernel with the r8-checking task A, 12 ticks.
    Expected result: no corruption; suspended stack = 8 software words then the hardware frame.
    """
    m, s = _kernel({"task_set": "r8_check", "stop_after_ticks": 12})
    assert s["faults"] == [] and s["globals"]["corruptions"] == 0
    sw = [e for e in m.trace.events if e.kind == "context_switch" and e.frm == "A"][0]
    snap = sw.details["stacks"]["A"]          # A's stack as saved by this switch
    assert snap["psp"] == sw.details["old_psp"]
    frame = [int(w, 16) for w in snap["words"][:16]]
    assert frame[4] >= 0x1000                 # r8 is word 4 of the software frame
    assert frame[15] & (1 << 24)              # xPSR with the Thumb bit tops the hardware frame
    assert frame[14] == m.image.symbols["spin"] or frame[14] >= m.image.symbols["taskA"]  # stacked pc in A's code


def test_context_switch_missing_register_is_detected():
    """Test ID: U26
    Purpose: dropping r8 from the PendSV frame leaks B's r8 into A (the P08 mechanism).
    Stimulus: kernel with the r8-checking task A and a 7-register PendSV list.
    Expected result: no fault, corruptions >= 1.
    """
    regs = ", ".join(f"r{i}" for i in range(4, 12) if i != 8)
    m, s = _kernel({"task_set": "r8_check", "stop_after_ticks": 12, "pendsv_save": regs, "pendsv_restore": regs})
    assert s["faults"] == [] and s["globals"]["corruptions"] >= 1


def test_svc_start_and_syscall_through_stacked_frame():
    """Test ID: U27
    Purpose: svc #0 drops to unprivileged PSP with EXC_RETURN 0xFFFFFFFD; the SVC handler reads the
             number from the svc instruction and writes the result into the stacked r0.
    Stimulus: kernel run for a few switches.
    Expected result: CONTROL=3, return from SVCall on PSP, write() calls numbered 3 with ret=1 in the frame.
    """
    m, s = _kernel({"stop_after_ticks": 6})
    start = [e for e in m.trace.events if e.kind == "syscall" and e.details.get("name") == "start"][0]
    assert "CONTROL=3" in start.reason
    ret = [e for e in m.trace.events if e.kind == "exception_return" and e.details.get("exception") == EXC_SVCALL][0]
    assert ret.details["exc_return"] == f"0x{EXC_RETURN_THREAD_PSP:08X}" and ret.details["stack"] == "psp"
    writes = [e for e in m.trace.events if e.kind == "syscall" and e.details.get("name") == "write"]
    assert writes and all(e.details["svc"] == 3 and e.details["ret"] == 1 for e in writes)
    assert "stacked PC-4" in writes[0].reason
    assert s["globals"]["svc_errors"] == 0
    assert m.nvic.get_priority(EXC_PENDSV) == 0xF0 and m.mpu.enabled


# ---------------------------------------------------------- determinism
def test_determinism_same_seed_identical_trace_bytes(tmp_path):
    """Test ID: U28
    Purpose: two runs with the same seed produce byte-identical trace files.
    Stimulus: run P02 (seed-dependent garbage) and P06 twice each, write both traces.
    Expected result: identical bytes.
    """
    for ident in ("P02", "P06"):
        sc = load_scenario(find_scenario(ident, SCENARIO_DIR))
        a = save_result(run_scenario(sc), tmp_path / "a").read_bytes()
        b = save_result(run_scenario(sc), tmp_path / "b").read_bytes()
        assert a == b and len(a) > 1000


def test_different_seed_changes_garbage_only():
    """Test ID: U29
    Purpose: the seed changes power-on SRAM contents (the P02 symptom) but not the event structure.
    Stimulus: P02 with seed 1 and seed 99.
    Expected result: same event kinds, different 'counter' value.
    """
    sc = load_scenario(find_scenario("P02", SCENARIO_DIR))
    r1 = run_scenario(sc)
    r2 = run_scenario(sc, seed=99)
    assert [e["kind"] for e in r1["events"]] == [e["kind"] for e in r2["events"]]
    assert r1["summary"]["globals"]["counter"] != r2["summary"]["globals"]["counter"]


def test_trace_register_deltas_replay():
    """Test ID: U30
    Purpose: each event carries only the registers that changed; replaying the deltas gives the
             full register file (what the viewer does).
    Stimulus: run P01 and replay up to the last event.
    Expected result: the first event has all registers; the replayed pc equals the final pc.
    """
    from r2s.trace import replay_registers
    r = run_scenario(load_scenario(find_scenario("P01", SCENARIO_DIR)))
    ev = r["events"]
    assert {"r0", "r12", "msp", "psp", "pc", "xpsr", "control"} <= set(ev[0]["regs"])
    regs = replay_registers(ev, len(ev) - 1)
    assert regs["pc"] == r["summary"]["final_registers"]["pc"]


# --------------------------------------------------------------- profiles
def test_profiles_differ_on_multi_register_and_compare_branch():
    """Test ID: U31
    Purpose: the riscv_like profile (RV32IMC without Zcmp) expands push/pop, counts cmp+branch as
             one compare-and-branch instruction, and needs two instructions
             for a literal address.
    Stimulus: cost one push, one bne and one ldr =sym under both profiles.
    Expected result: instret 1 vs 5 for push, 1 vs 0 for bne, 1 vs 2 for the literal; Thumb denser.
    """
    arm, rv = get_profile("arm_like"), get_profile("riscv_like")
    p = assemble("push {r4-r7, lr}\ncmp r0, #1\nbne x\nx: ldr r0, =x")
    push, _, bne, lit = p.instructions
    assert arm.cost(push)[1] == 1 and rv.cost(push)[1] == 5
    assert arm.cost(bne)[1] == 1 and rv.cost(bne)[1] == 0
    assert arm.cost(lit)[1] == 1 and rv.cost(lit)[1] == 2
    assert arm.cost(push)[2] < rv.cost(push)[2]


def test_wfi_skips_to_next_event():
    """Test ID: U32
    Purpose: wfi advances the clock to the next stimulus instead of spinning.
    Stimulus: wfi with a button press scheduled at cycle 5000.
    Expected result: the run ends a little after cycle 5000 with very few instructions.
    """
    m = small_machine("main:\n wfi\n halt", max_cycles=100000)
    m.add_stimulus(Stimulus(5000, "button_press", {}))
    s = m.run()
    assert 5000 <= s["cycles"] < 5200 and s["instructions_executed"] < 200
