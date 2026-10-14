"""One test per scenario. Each docstring states Test ID / Purpose / Stimulus /
Expected result so the test file doubles as the scenario acceptance sheet.

Author: Farzana Akhter
"""

from __future__ import annotations

from conftest import assert_sequence, run_cached, variant


def _checks_pass(result):
    failed = [c for c in result["checks"] if not c["ok"]]
    assert not failed, f"failed checks: {failed}"


def test_P01_clean_boot():
    """Test ID: P01
    Purpose: the healthy reference boot reaches main and the program runs to halt.
    Stimulus: blink program, no faults.
    Expected result: all six boot steps ok, no faults, UART shows boot ok / counter=1 / greeting_len=7.
    """
    r = run_cached("P01")
    _checks_pass(r)
    s = r["summary"]
    steps = [b["step"] for b in s["boot_steps"]]
    assert steps == ["power_on", "vector_table", "reset_handler", "data_copied", "bss_zeroed", "main_entered"]
    assert all(b["ok"] for b in s["boot_steps"])
    assert s["uart_lines"] == ["boot ok", "counter=1", "greeting_len=7"]
    assert assert_sequence(r["events"], ["boot_step:main_entered", "uart_tx", "gpio", "halt"])


def test_P02_missing_bss_zero():
    """Test ID: P02
    Purpose: a missing .bss loop leaves garbage in uninitialised globals without any fault.
    Stimulus: blink program with fault missing_bss_zero.
    Expected result: bss_zeroed step flagged, no faults, counter is not 1, .data value still correct.
    """
    r = run_cached("P02")
    _checks_pass(r)
    s = r["summary"]
    bss = next(b for b in s["boot_steps"] if b["step"] == "bss_zeroed")
    assert bss["ok"] is False and bss["nonzero"]
    assert s["faults"] == []
    assert "greeting_len=7" in s["uart_lines"]
    assert not any(line == "counter=1" for line in s["uart_lines"])


def test_P03_wrong_initial_sp():
    """Test ID: P03
    Purpose: a bad initial MSP produces a HardFault with a stacking error and a lockup.
    Stimulus: blink program with fault wrong_initial_sp (value 0).
    Expected result: vector_table step flagged, first push faults, STKERR in the record, core locked up.
    """
    r = run_cached("P03")
    _checks_pass(r)
    s = r["summary"]
    assert s["lockup"] is True
    assert s["faults"][0]["kind"] == "HardFault"
    assert s["faults"][0]["escalated_from"] == "BusFault"
    assert "STKERR" in s["faults"][-1]["cfsr"]["BFSR"]
    assert s["faults"][-1]["stacked_frame"] is None
    assert assert_sequence(r["events"], ["boot_step:main_entered", "fault", "fault", "lockup"])


def test_P04_interrupt_latency():
    """Test ID: P04
    Purpose: interrupt latency is tight under light load and jittery with cpsid regions.
    Stimulus: 24 button presses, variants light and heavy.
    Expected result: light jitter <= 2 cycles near the 12-cycle entry cost; heavy max and jitter much larger.
    """
    r = run_cached("P04")
    _checks_pass(r)
    light, heavy = variant(r, "light")["latency"], variant(r, "heavy")["latency"]
    assert light["samples"] == 24 and heavy["samples"] == 24
    assert light["min"] >= 12 and light["jitter"] <= 2
    assert heavy["max"] > light["max"] + 50
    assert heavy["jitter"] > 50
    assert len(heavy["histogram"]) > len(light["histogram"])


def test_P05_priority_inversion():
    """Test ID: P05
    Purpose: equal priorities serialise handlers (tail-chain); a higher priority pre-empts.
    Stimulus: slow timer IRQ, button pressed during it; variants inversion and fixed.
    Expected result: inversion has a tail-chain and long button latency; fixed pre-empts in ~12 cycles.
    """
    r = run_cached("P05")
    _checks_pass(r)
    inv, fixed = variant(r, "inversion")["priority"], variant(r, "fixed")["priority"]
    assert inv["tail_chains"] >= 1 and inv["button_preempted_timer"] is False
    assert fixed["button_preempted_timer"] is True
    assert fixed["button_latency"] < 20 < 200 < inv["button_latency"]
    entries_fixed = [e for e in r["events"] if e["kind"] == "exception_entry" and e["details"].get("variant") == "fixed"
                     and e["details"].get("name") == "IRQ_GPIO"]
    assert entries_fixed and entries_fixed[0]["from"] == "ISR:IRQ_TIMER0"


def test_P06_round_robin_kernel():
    """Test ID: P06
    Purpose: three tasks and idle switch on every SysTick through PendSV.
    Stimulus: kernel program, 30 ticks, no faults.
    Expected result: 40+ switches, all tasks alive, alphabet on UART, no faults, watermarks about 25 percent.
    """
    r = run_cached("P06")
    _checks_pass(r)
    k = r["summary"]["kernel"]
    assert k["ticks"] == 30 and k["switches"] >= 40
    assert all(t["state"] != "DEAD" for t in k["tasks"].values())
    for name in ("A", "B", "C"):
        assert 20 <= k["tasks"][name]["watermark"]["percent"] <= 40
    assert r["summary"]["faults"] == []
    switches = [e for e in r["events"] if e["kind"] == "context_switch"]
    assert {e["to"] for e in switches} >= {"A", "B", "C"}


def test_P07_mpu_catches_overflow():
    """Test ID: P07
    Purpose: an MPU guard turns a stack overflow into a MemManage fault that kills only the guilty task.
    Stimulus: kernel program with task_overflows_stack, MPU enabled.
    Expected result: MemManage with MMARVALID in guard_C, C dead at 100 percent watermark, A and B alive, no lockup.
    """
    r = run_cached("P07")
    _checks_pass(r)
    s = r["summary"]
    first = s["faults"][0]
    assert first["kind"] == "MemManage" and "guard_C" in first["reason"]
    assert "MMARVALID" in first["cfsr"]["MMFSR"]
    tasks = s["kernel"]["tasks"]
    assert tasks["C"]["state"] == "DEAD" and tasks["C"]["watermark"]["percent"] == 100
    assert tasks["A"]["state"] != "DEAD" and tasks["B"]["state"] != "DEAD"
    assert s["lockup"] is False


def test_P08_pendsv_forgets_r8():
    """Test ID: P08
    Purpose: a register missing from the PendSV frame corrupts task data without any fault.
    Stimulus: kernel program, task set r8_check, fault pendsv_forgets_register r8.
    Expected result: no faults, switches continue, task A records corruptions after a B->A switch.
    """
    r = run_cached("P08")
    _checks_pass(r)
    s = r["summary"]
    assert s["faults"] == []
    assert s["globals"]["corruptions"] >= 1
    assert assert_sequence(r["events"], ["context_switch:B", "context_switch:A", "marker"])
    assert s["kernel"]["switches"] >= 30
    # the healthy kernel with the same task set never reports a corruption
    from r2s.scenarios import load_scenario, find_scenario, run_scenario, SCENARIO_DIR
    healthy = load_scenario(find_scenario("P08", SCENARIO_DIR))
    healthy["faults"] = []
    hr = run_scenario(healthy)
    assert hr["summary"]["globals"]["corruptions"] == 0


def test_P09_overflow_without_mpu():
    """Test ID: P09
    Purpose: without the MPU the same overflow corrupts neighbours and faults late and elsewhere.
    Stimulus: kernel program with task_overflows_stack and no_mpu.
    Expected result: no MemManage, later BusFault/UsageFault, more than one task dead, C watermark 100 percent.
    """
    r = run_cached("P09")
    _checks_pass(r)
    s = r["summary"]
    kinds = [f["kind"] for f in s["faults"]]
    assert "MemManage" not in kinds
    assert "UsageFault" in kinds or "BusFault" in kinds
    tasks = s["kernel"]["tasks"]
    assert tasks["C"]["watermark"]["percent"] == 100
    assert sum(1 for t in tasks.values() if t["state"] == "DEAD") >= 2
    p07 = run_cached("P07")["summary"]["kernel"]["tasks"]
    assert sum(1 for t in p07.values() if t["state"] == "DEAD") == 1


def test_P10_flash_vs_sram():
    """Test ID: P10
    Purpose: the same loop costs more cycles from uncached flash than from cached flash or SRAM.
    Stimulus: bench program in three variants (flash_cached, flash_uncached, sram).
    Expected result: sram < flash_cached < flash_uncached in cycles; identical instret and code size.
    """
    r = run_cached("P10")
    _checks_pass(r)
    b = {n: variant(r, n)["bench"] for n in ("flash_cached", "flash_uncached", "sram")}
    assert b["sram"]["bench_cycles"] < b["flash_cached"]["bench_cycles"] < b["flash_uncached"]["bench_cycles"]
    assert b["flash_uncached"]["bench_cycles"] > 2 * b["sram"]["bench_cycles"]
    assert len({v["instret"] for v in b.values()}) == 1
    assert len({v["static_code_bytes"] for v in b.values()}) == 1
    assert b["flash_cached"]["xip_misses"] < 20 < b["flash_uncached"]["xip_misses"]


def test_P11_isa_comparison():
    """Test ID: P11
    Purpose: two cost profiles give different cycles/instret/CPI/code size for one program.
    Stimulus: bench program in SRAM under arm_like and riscv_like.
    Expected result: arm_like denser code and fewer cycles; riscv_like fewer retired instructions, higher CPI.
    """
    r = run_cached("P11")
    _checks_pass(r)
    arm, rv = variant(r, "arm_like")["bench"], variant(r, "riscv_like")["bench"]
    assert arm["static_code_bytes"] < rv["static_code_bytes"]
    assert arm["static_instructions"] == rv["static_instructions"]
    assert rv["instret"] < arm["instret"]
    assert rv["cpi"] > arm["cpi"]


def test_P12_svc_syscall_path():
    """Test ID: P12
    Purpose: SVC enters privileged handler mode and returns a value through the stacked r0;
             unprivileged cpsid is ignored and an unprivileged SysTick write is a precise BusFault.
    Stimulus: kernel program with task_privileged_op.
    Expected result: write syscalls return 1, cpsid ignored, BusFault PRECISERR with BFAR=0xE000E010
                     in task C, C dead, A and B alive, no lockup.
    """
    r = run_cached("P12")
    _checks_pass(r)
    s = r["summary"]
    writes = [e for e in r["events"] if e["kind"] == "syscall" and e["details"].get("name") == "write"]
    assert len(writes) >= 3 and all(e["details"]["ret"] == 1 for e in writes)
    assert all(e["details"]["return_to"] == "stacked_r0" for e in writes)
    assert s["ignored_privileged_ops"] >= 1
    fault = s["faults"][0]
    assert fault["kind"] == "BusFault" and fault["flag"] == "PRECISERR"
    assert fault["privileged"] is False and fault["context"] == "C"
    assert fault["bfar"] == "0xE000E010" and fault["instruction"] == "str r1, [r0]"
    assert s["kernel"]["tasks"]["C"]["state"] == "DEAD"
    assert s["kernel"]["tasks"]["A"]["state"] != "DEAD"
    entries = [e for e in r["events"] if e["kind"] == "exception_entry" and e["details"].get("name") == "SVCall"]
    assert entries[0]["details"]["stack"] == "msp" and entries[1]["details"]["stack"] == "psp"
    returns = [e for e in r["events"] if e["kind"] == "exception_return" and "unprivileged" in e["reason"]]
    assert returns


def test_P13_missing_data_copy():
    """Test ID: P13
    Purpose: a missing .data copy leaves an initialised global with power-on garbage, no fault.
    Stimulus: blink program with fault missing_data_copy.
    Expected result: data_copied step flagged, bss_zeroed ok, counter=1 printed, greeting_len != 7.
    """
    r = run_cached("P13")
    _checks_pass(r)
    s = r["summary"]
    data = next(b for b in s["boot_steps"] if b["step"] == "data_copied")
    assert data["ok"] is False and "greeting_len" in data["mismatches"][0]
    assert s["globals"]["greeting_len"] != 7 and s["globals"]["counter"] == 1
    assert s["faults"] == []


def test_P14_vector_thumb_bit():
    """Test ID: P14
    Purpose: a reset vector without the Thumb bit faults on the first instruction (INVSTATE -> HardFault).
    Stimulus: blink program with fault vector_thumb_bit_missing on exception 1 (reset).
    Expected result: one HardFault escalated from UsageFault INVSTATE, stacked PC = Reset_Handler,
                     stacked xPSR.T = 0, CFSR=0x00020000, HFSR=0x40000000, fault printer output on UART.
    """
    r = run_cached("P14")
    _checks_pass(r)
    s = r["summary"]
    f = s["faults"][0]
    reset = int(r["image_map"]["vectors"][1]["value"], 16)
    assert reset & 1 == 0
    assert f["flag"] == "INVSTATE" and f["escalated_from"] == "UsageFault"
    assert f["cfsr_value"] == "0x00020000" and f["hfsr_value"] == "0x40000000"
    assert int(f["stacked_frame"]["pc"], 16) == reset
    assert int(f["stacked_frame"]["xpsr"], 16) & (1 << 24) == 0
    assert any(line.strip() == "UFSR.INVSTATE" for line in s["uart_lines"])


def test_P15_pendsv_priority():
    """Test ID: P15
    Purpose: PendSV must be the lowest priority or the switch can run on top of another handler.
    Stimulus: kernel with a long button ISR (0xC0); variants PendSV 0xFF (correct) and 0x80 (bug).
    Expected result: correct never switches with EXC_RETURN != 0xFFFFFFFD and the ISR sees no corruption;
                     the bug switches with 0xFFFFFFF1 and the ISR detects corrupted registers.
    """
    r = run_cached("P15")
    _checks_pass(r)
    ok, bad = variant(r, "correct"), variant(r, "pendsv_high")
    assert ok["globals"]["isr_corruptions"] == 0 and bad["globals"]["isr_corruptions"] >= 1
    switches = [e for e in r["events"] if e["kind"] == "context_switch" and "exc_return" in e["details"]]
    by_variant = {}
    for e in switches:
        by_variant.setdefault(e["details"]["variant"], set()).add(e["details"]["exc_return"])
    assert by_variant["correct"] == {"0xFFFFFFFD"}
    assert "0xFFFFFFF1" in by_variant["pendsv_high"]
    assert ok["kernel"]["pendsv_priority"] == "0xF0" and bad["kernel"]["pendsv_priority"] == "0x80"


def test_P16_systick_period():
    """Test ID: P16
    Purpose: a timer period is reload x clock period; both can be wrong independently.
    Stimulus: tick program, variants correct, off_by_one (RVR=N), wrong_clock (12 MHz core).
    Expected result: 150000 / 150001 / 150000 cycles per tick and 1000 / 1000 / 12500 us per tick.
    """
    r = run_cached("P16")
    _checks_pass(r)
    t = {n: variant(r, n)["tick"] for n in ("correct", "off_by_one", "wrong_clock")}
    assert (t["correct"]["period_cycles"], t["correct"]["period_us"]) == (150000, 1000)
    assert t["off_by_one"]["reload"] == 150000 and t["off_by_one"]["period_cycles"] == 150001
    assert t["wrong_clock"]["period_cycles"] == 150000 and t["wrong_clock"]["period_us"] == 12500
    assert "10 ticks: us=125000" in variant(r, "wrong_clock")["uart_lines"]


def test_P17_svc_return_value():
    """Test ID: P17
    Purpose: an SVC result written to the live r0 is lost; the caller sees the stacked r0 (its argument).
    Stimulus: kernel program with fault svc_return_in_live_r0.
    Expected result: no faults, output still printed, every write() seen as failed by task B.
    """
    r = run_cached("P17")
    _checks_pass(r)
    s = r["summary"]
    writes = [e for e in r["events"] if e["kind"] == "syscall" and e["details"].get("name") == "write"]
    assert writes and all(e["details"]["return_to"] == "live_r0" for e in writes)
    letters = [w for w in writes if w["from"] == "B" and w["details"]["arg"] != 10]  # B ignores newline results
    assert len(letters) - 1 <= s["globals"]["svc_errors"] <= len(letters)
    # the exception return restores the argument, not the result
    ret = next(e for e in r["events"] if e["kind"] == "exception_return" and e["details"].get("exception") == 11
               and e["details"]["popped_frame"]["r0"] != "0x00000000")
    assert ret["details"]["popped_frame"]["r0"] == "0x00000041"
    assert run_cached("P06")["summary"]["globals"]["svc_errors"] == 0


def test_P18_irq_never_handled():
    """Test ID: P18
    Purpose: an interrupt needs the NVIC enable bit and a vector to a real handler.
    Stimulus: latency program with one button press; variants not_enabled and no_handler.
    Expected result: not_enabled: pending but never taken; no_handler: taken into Default_Handler, run stops.
    """
    r = run_cached("P18")
    _checks_pass(r)
    ne, nh = variant(r, "not_enabled"), variant(r, "no_handler")
    assert ne["exception_entries"] == 0 and "max_cycles" in ne["stop_reason"]
    assert nh["exception_entries"] == 1 and "Default_Handler" in nh["stop_reason"]
    entry = next(e for e in r["events"] if e["kind"] == "exception_entry")
    assert entry["details"]["exception"] == 37


def test_every_scenario_file_has_a_test_and_learning_notes():
    """Test ID: S00
    Purpose: keep the acceptance sheet complete: one test_<ID>_<slug> per scenario file, and every
             scenario carries the learning fields the viewer and the CLI print.
    Stimulus: scan scenarios/*.yaml and this module.
    Expected result: no scenario without a test, no test without a scenario, no empty learning field.
    """
    from r2s.scenarios import SCENARIO_DIR, list_scenarios, load_scenario
    names = {n for n in globals() if n.startswith("test_P")}
    files = {f"test_{p.stem}" for p in list_scenarios(SCENARIO_DIR)}
    assert names == files, f"missing tests: {files - names}; tests without scenario: {names - files}"
    for path in list_scenarios(SCENARIO_DIR):
        sc = load_scenario(path)
        assert sc["id"] == path.stem.split("_")[0]
        for key in ("what_to_look_for", "what_this_means", "hidden_symptom", "description"):
            assert len(sc[key].split()) >= (6 if key == "hidden_symptom" else 15), (sc["id"], key)
        assert len(sc["likely_root_causes"]) >= 1 and len(sc["how_to_debug"]) >= 3, sc["id"]
        assert sc["expected"]["summary"] and sc["expected"]["checks"]
