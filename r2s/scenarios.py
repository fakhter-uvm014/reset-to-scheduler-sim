"""Scenario files: loading, running, checking and analysing.

A scenario is a YAML file (``scenarios/P06_round_robin.yaml``)::

    id: P06
    title: Three-task round-robin kernel
    description: ...
    program: kernel              # builder name from r2s.programs
    options: {...}               # builder options
    config: {seed: 1, max_cycles: 200000, profile: arm_like}
    faults: [{type: no_mpu}]
    stimuli: [{at_us: 50, action: button_press}]
    variants:                    # optional: run the same scenario several times
      - {name: flash, options: {code_region: flash}}
      - {name: sram,  options: {code_region: sram}}
    expected:
      summary: one sentence
      checks:
        - {check: uart_contains, text: "boot ok"}
        - {check: no_faults}
    what_to_look_for: ...
    likely_root_causes: [...]
    how_to_debug: [...]
    what_this_means: ...
    hidden_symptom: ...          # what --hidden prints instead of the title

Checks (the vocabulary a scenario author can use)
--------------------------------------------------
    no_faults                     cpu.faults is empty
    fault_count     count         exactly this many fault records
    fault_kind      kind          last fault record has this kind (exception taken)
    fault_flag      flag          some fault set this CFSR/HFSR bit (DACCVIOL, INVSTATE...)
    fault_escalated_from kind     last fault was escalated from this kind
    fault_reason_contains text
    lockup          value
    boot_step       step, ok      the named boot step passed/failed
    boot_complete                 all six boot steps ok
    uart_contains   text
    uart_not_contains text
    global_equals   name, value   final SRAM value of a symbol
    global_gt       name, value
    global_ne       name, value
    metric_equals   metric, value   summary value at a dotted path (tick.period_cycles)
    metric_between  metric, min, max
    no_event        kind[, to]     no event of this kind (e.g. no exception_entry)
    event_contains  kind, text     some event of this kind has ``text`` in its reason
    boot_problem_contains text     the vector_table boot step reports this problem
    event_sequence  kinds         these event kinds appear in this order
    min_events      kind, count
    task_state      task, state
    task_alive      task          state is READY/RUNNING/BLOCKED
    min_switches    count
    tail_chains_min count
    watermark_min_percent task, value
    stop_reason_contains text
    variant_lt      metric, a, b  summary[metric] of variant a < variant b
    variant_gt      metric, a, b
    latency_jitter_gt a, b        max latency jitter in variant a > variant b

Author: Farzana Akhter
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import faults as fault_lib
from .boot import Image
from .engine import Machine, MachineConfig, Stimulus
from .isa_profiles import get_profile
from .nvic import IRQ_BASE, IRQ_GPIO, exc_name
from .programs import BUILDERS
from .trace import write_trace

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
REQUIRED_KEYS = ("id", "title", "layer", "description", "program", "expected", "what_to_look_for",
                 "likely_root_causes", "how_to_debug", "what_this_means", "hidden_symptom")
LAYERS = ("1 ISA", "2 Boot", "3 Drivers", "4 Exceptions", "5 Kernel", "Experiments")


def layer_label(layer: str) -> str:
    """'2 Boot' -> 'Layer 2 Boot'; the unnumbered 'Experiments' stays as it is
    (the course has five layers plus an experiments phase)."""
    return f"Layer {layer}" if layer[:1].isdigit() else layer


def load_scenario(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"{path}: scenario is missing keys {missing}")
    if data["layer"] not in LAYERS:
        raise ValueError(f"{path}: layer must be one of {LAYERS}")
    if not data["expected"].get("checks"):
        raise ValueError(f"{path}: expected.checks is empty")
    data["_path"] = str(path)
    return data


def list_scenarios(directory: Path = SCENARIO_DIR) -> List[Path]:
    return sorted(Path(directory).glob("*.yaml"))


def find_scenario(ident: str, directory: Path = SCENARIO_DIR) -> Path:
    p = Path(ident)
    if p.exists():
        return p
    for path in list_scenarios(directory):
        if path.stem.upper().startswith(ident.upper()) or path.stem == ident:
            return path
    raise FileNotFoundError(f"no scenario named '{ident}' in {directory}")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _stimuli(machine: Machine, specs: List[Dict[str, Any]]) -> None:
    per_us = machine.config.cpu_hz // 1_000_000
    for spec in specs or []:
        if "every_us" in spec:
            start = int(spec.get("start_us", 0))
            count = int(spec.get("count", 1))
            for i in range(count):
                at = (start + i * int(spec["every_us"])) * per_us
                machine.add_stimulus(Stimulus(at, spec["action"], dict(spec)))
        else:
            at = int(spec["at_cycles"]) if "at_cycles" in spec else int(spec["at_us"]) * per_us
            machine.add_stimulus(Stimulus(at, spec["action"], dict(spec)))


def _merge(scenario: Dict[str, Any], variant: Optional[Dict[str, Any]]):
    options = dict(scenario.get("options") or {})
    config_dict = dict(scenario.get("config") or {})
    faults = list(scenario.get("faults") or [])
    stimuli = list(scenario.get("stimuli") or [])
    name = None
    if variant:
        name = variant.get("name")
        options.update(variant.get("options") or {})
        config_dict.update(variant.get("config") or {})
        faults += list(variant.get("faults") or [])
        stimuli += list(variant.get("stimuli") or [])
    return name, options, config_dict, faults, stimuli


def build_machine(scenario: Dict[str, Any], variant: Optional[Dict[str, Any]] = None,
                  seed: Optional[int] = None) -> Machine:
    """Build the image and the machine for one run, ready for ``power_on``.

    ``seed`` overrides the scenario's ``config.seed`` (``r2s run --seed``).
    """
    name, options, config_dict, faults, stimuli = _merge(scenario, variant)
    options = fault_lib.apply_to_options(faults, options)
    config_dict = fault_lib.apply_to_config(faults, config_dict)
    if seed is not None:
        config_dict["seed"] = int(seed)
    config = MachineConfig(**{k: v for k, v in config_dict.items() if k in MachineConfig.__dataclass_fields__})
    config.variant = name
    image = Image()
    builder = BUILDERS[scenario["program"]]
    kernel = builder(image, options)
    machine = Machine(image, config, fault_lib.boot_faults(faults))
    if kernel is not None:
        kernel.attach(machine)
    fault_lib.machine_faults(faults, machine)
    _stimuli(machine, stimuli)
    machine.scenario_options = options
    machine.scenario_faults = faults
    machine.trace.emit("scenario", "scenario",
                       f"scenario {scenario['id']}{' variant ' + name if name else ''}: {scenario['title']}",
                       scenario=scenario["id"], variant=name,
                       faults=[fault_lib.describe(f) for f in faults], options=options)
    return machine


def run_variant(scenario: Dict[str, Any], variant: Optional[Dict[str, Any]] = None,
                seed: Optional[int] = None, stop_at_cycle: Optional[int] = None) -> Dict[str, Any]:
    """Build the image, power on, run, analyse. Returns ``{summary, events, ...}``."""
    machine = build_machine(scenario, variant, seed)
    machine.stop_at_cycle = stop_at_cycle
    machine.power_on()
    summary = machine.run()
    options, faults = machine.scenario_options, machine.scenario_faults
    summary["faults_injected"] = [fault_lib.describe(f) for f in faults]
    summary["options"] = options
    analyze(scenario["program"], machine, summary, options)
    return {"name": variant.get("name") if variant else None, "summary": summary,
            "events": machine.trace.as_dicts(), "machine": machine, "image_map": image_map(machine),
            "config": {"seed": machine.config.seed, "cpu_hz": machine.config.cpu_hz,
                       "profile": machine.config.profile, "max_cycles": machine.config.max_cycles,
                       "xip_cache_enabled": machine.config.xip_cache_enabled,
                       "xip_miss_penalty": machine.config.xip_miss_penalty}}


def image_map(machine: Machine) -> Dict[str, Any]:
    img = machine.image
    vectors = []
    for number in range(0, 16 + 8):
        word = machine.memory.peek(img.vector_base + 4 * number)
        if not isinstance(word, int):
            continue
        label = "initial MSP" if number == 0 else exc_name(number)
        if number in (7, 8, 9, 10, 13):
            continue  # reserved slots
        vectors.append({"index": number, "name": label, "value": f"0x{word:08X}"})
    return {
        "vector_base": f"0x{img.vector_base:08X}",
        "initial_sp": f"0x{img.initial_sp:08X}",
        "vectors": vectors,
        "sections": [{"name": s.name, "region": s.region, "base": f"0x{s.base:08X}",
                      "words": s.program.size_words} for s in img.sections],
        "data": {k: f"0x{img.symbols[k]:08X}" for k in img.data},
        "bss": {k: f"0x{img.symbols[k]:08X}" for k in img.bss},
        "reserved": {k: {"base": f"0x{b:08X}", "size": s} for k, (b, s) in img.reserve.items()},
        "data_load": f"0x{img.data_load:08X}",
    }


def run_scenario(scenario: Dict[str, Any], seed: Optional[int] = None) -> Dict[str, Any]:
    """Run every variant (or the single default run), evaluate checks."""
    variants = scenario.get("variants") or [None]
    runs = [run_variant(scenario, v, seed=seed) for v in variants]
    by_name = {r["name"]: r["summary"] for r in runs}
    checks = evaluate_checks(scenario, runs, by_name)
    passed = all(c["ok"] for c in checks)
    header = {k: v for k, v in scenario.items() if not k.startswith("_")}
    if len(runs) == 1 and runs[0]["name"] is None:
        summary = runs[0]["summary"]
    else:
        summary = {"variants": {r["name"]: r["summary"] for r in runs},
                   "stop_reason": "; ".join(f"{r['name']}: {r['summary']['stop_reason']}" for r in runs)}
    summary["measurements"] = measurements(scenario, runs)
    result = {
        "scenario": header,
        "config": runs[0]["config"],
        "options": runs[0]["summary"].get("options"),
        "image_map": runs[0]["image_map"],
        "events": [e for r in runs for e in r["events"]],
        "summary": summary,
        "variants": [r["name"] for r in runs if r["name"]],
        "result": {"passed": passed,
                   "checks": [{"name": c["check"], "passed": c["ok"], "detail": c["detail"],
                               "variant": c["variant"], "args": c["args"]} for c in checks]},
        # convenience aliases for Python callers (not written to the trace file)
        "checks": checks,
        "passed": passed,
    }
    return result


# ---------------------------------------------------------------------------
# Analysis (per program type)
# ---------------------------------------------------------------------------
def analyze(program: str, machine: Machine, summary: Dict[str, Any], options: Dict[str, Any]) -> None:
    if program == "latency":
        summary["latency"] = latency_stats(machine)
    if program == "bench":
        summary["bench"] = bench_stats(machine)
    if program == "priority":
        summary["priority"] = priority_stats(machine)
    if program == "tick":
        summary["tick"] = tick_stats(machine, options)
    if program == "kernel":
        summary["kernel_timing"] = kernel_timing(machine)
    main = next((b for b in summary.get("boot_steps", []) if b["step"] == "main_entered"), None)
    summary["cycles_to_main"] = main["t_cycles"] if main else None


def _durations(events, start_kinds, start_exc: int, end_exc: int) -> List[int]:
    """Cycles from entering exception ``start_exc`` to the return of ``end_exc``."""
    out, t0 = [], None
    for e in events:
        if e.kind in start_kinds and e.details.get("exception") == start_exc:
            t0 = e.t_cycles
        elif e.kind == "exception_return" and e.details.get("exception") == end_exc and t0 is not None:
            out.append(e.t_cycles - t0)
            t0 = None
    return out


def _stats(values: List[int]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "avg": round(sum(values) / len(values), 1), "max": max(values)}


def kernel_timing(machine: Machine) -> Dict[str, Any]:
    """Context-switch cost and tick-handler cost, measured from the trace the
    way a student measures them with CYCCNT on the board."""
    from .nvic import EXC_PENDSV, EXC_SYSTICK
    ev = machine.trace.events
    switch = _durations(ev, ("exception_entry", "tail_chain"), EXC_PENDSV, EXC_PENDSV)
    tick = _durations(ev, ("exception_entry",), EXC_SYSTICK, EXC_SYSTICK)
    return {"switch_cost_cycles": _stats(switch), "tick_handler_cycles": _stats(tick),
            "note": "switch cost = PendSV entry (or tail-chain) to its exception return, "
                    "including the 10-cycle unstacking; tick cost = SysTick entry to return"}


def tick_stats(machine: Machine, options: Dict[str, Any]) -> Dict[str, Any]:
    g = {k: machine.memory.peek(machine.image.symbols[k]) for k in
         ("ticks", "t_start_cycles", "t_end_cycles", "t_start_us", "t_end_us", "reload_value")}
    cycles10 = g["t_end_cycles"] - g["t_start_cycles"]
    us10 = g["t_end_us"] - g["t_start_us"]
    target = int(options.get("tick_us", 1000))
    return {"reload": g["reload_value"], "period_cycles": cycles10 / 10, "period_us": us10 / 10,
            "target_us": target, "assumed_hz": int(options.get("assumed_hz", 150_000_000)),
            "actual_hz": machine.config.cpu_hz, "ratio_to_target": round(us10 / 10 / target, 3) if target else None}


def latency_stats(machine: Machine) -> Dict[str, Any]:
    """Button press -> IRQ entry -> marker pin high, per press."""
    events = machine.trace.events
    samples = []
    press_cycles: Optional[int] = None
    entry_cycles: Optional[int] = None
    for e in events:
        if e.kind == "gpio" and e.details.get("stimulus"):
            press_cycles, entry_cycles = e.details.get("nominal", e.t_cycles), None
        elif e.kind in ("exception_entry", "tail_chain") and e.details.get("exception") == IRQ_BASE + IRQ_GPIO \
                and press_cycles is not None:
            entry_cycles = e.t_cycles
        elif e.kind == "gpio" and e.details.get("pin") == 16 and e.details.get("level") == 1 \
                and press_cycles is not None and entry_cycles is not None:
            samples.append({"press": press_cycles, "entry": entry_cycles - press_cycles,
                            "marker": e.t_cycles - press_cycles})
            press_cycles = None
    values = [s["marker"] for s in samples]
    if not values:
        return {"samples": 0}
    hist: Dict[str, int] = {}
    for v in values:
        bucket = f"{(v // 10) * 10}-{(v // 10) * 10 + 9}"
        hist[bucket] = hist.get(bucket, 0) + 1
    avg = sum(values) / len(values)
    return {
        "samples": len(values), "min": min(values), "max": max(values), "avg": round(avg, 1),
        "jitter": max(values) - min(values),
        "min_us": machine.trace.cycles_to_us(min(values)), "max_us": machine.trace.cycles_to_us(max(values)),
        "entry_min": min(s["entry"] for s in samples), "entry_max": max(s["entry"] for s in samples),
        "histogram": dict(sorted(hist.items(), key=lambda kv: int(kv[0].split("-")[0]))),
        "per_press": samples,
    }


def bench_stats(machine: Machine) -> Dict[str, Any]:
    section = machine.image.section("benchmark")
    profile = machine.profile
    static_size = sum(profile.cost(i)[2] for i in section.program.instructions)
    cycles = machine.memory.peek(machine.image.symbols["bench_cycles"])
    # Only count what ran inside main (the benchmark call), not the startup code.
    instret = machine.cpu.ctx_instret.get("main", 0)
    main_cycles = machine.cpu.ctx_cycles.get("main", 0)
    overhead = machine.memory.peek(machine.image.symbols["measure_overhead"])
    return {
        "code_region": section.region, "profile": profile.name,
        "xip_cache_enabled": machine.memory.xip_cache.enabled,
        "bench_cycles": cycles, "bench_us": machine.trace.cycles_to_us(cycles),
        "measure_overhead": overhead, "bench_cycles_corrected": cycles - overhead,
        "instret": instret, "cpi": round(main_cycles / instret, 3) if instret else None,
        "main_cycles": main_cycles,
        "instret_total": machine.cpu.instret, "cpi_total": machine.summary()["cpi"],
        "static_code_bytes": static_size, "static_instructions": section.program.size_words,
        "xip_hits": machine.memory.xip_cache.hits, "xip_misses": machine.memory.xip_cache.misses,
        "flash_accesses": machine.memory.access_counts["FLASH"], "sram_accesses": machine.memory.access_counts["SRAM"],
    }


def priority_stats(machine: Machine) -> Dict[str, Any]:
    events = machine.trace.events
    order = [(e.t_cycles, e.details.get("name"), e.kind) for e in events
             if e.kind in ("exception_entry", "tail_chain")]
    button_wait = None
    press = next((e.details.get("nominal", e.t_cycles) for e in events if e.kind == "gpio" and e.details.get("stimulus")), None)
    entry = next((e.t_cycles for e in events if e.kind in ("exception_entry", "tail_chain")
                  and e.details.get("exception") == IRQ_BASE + IRQ_GPIO), None)
    if press is not None and entry is not None:
        button_wait = entry - press
    preempted = any(e.kind == "exception_entry" and e.details.get("exception") == IRQ_BASE + IRQ_GPIO
                    and (e.frm or "").startswith("ISR:") for e in events)
    return {"entries": order, "button_latency": button_wait, "button_preempted_timer": preempted,
            "tail_chains": machine.cpu.tail_chains}


def measurements(scenario: Dict[str, Any], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The numbers a student would put in the lab report, one row each:
    ``{name, variant, value, unit, note}``. The viewer shows this table."""
    rows: List[Dict[str, Any]] = []

    def add(name, variant, value, unit, note=""):
        if value is not None:
            rows.append({"name": name, "variant": variant, "value": value, "unit": unit, "note": note})
    for r in runs:
        s, v = r["summary"], r["name"]
        hz = r["config"]["cpu_hz"]
        add("reset to main", v, s.get("cycles_to_main"), "cycles",
            f"{round((s.get('cycles_to_main') or 0) * 1e6 / hz, 2)} us at {hz // 1_000_000} MHz")
        add("CPI (whole run)", v, s.get("cpi"), "cycles/instr", "includes exception entry/return overhead")
        lat = s.get("latency") or {}
        if lat.get("samples"):
            add("IRQ latency min (edge -> marker)", v, lat["min"], "cycles", "entry 12 + marker store")
            add("IRQ latency avg", v, lat["avg"], "cycles")
            add("IRQ latency max", v, lat["max"], "cycles", "bounded by the longest cpsid region")
            add("IRQ latency jitter", v, lat["jitter"], "cycles")
        prio = s.get("priority") or {}
        if prio.get("button_latency") is not None:
            add("button IRQ wait", v, prio["button_latency"], "cycles",
                "pre-empted the timer" if prio.get("button_preempted_timer") else "waited for the timer handler")
        kt = s.get("kernel_timing") or {}
        if kt.get("switch_cost_cycles", {}).get("n"):
            sc = kt["switch_cost_cycles"]
            add("context switch (PendSV) avg", v, sc["avg"], "cycles", f"min {sc['min']}, max {sc['max']}, n={sc['n']}")
        if kt.get("tick_handler_cycles", {}).get("n"):
            add("SysTick handler avg", v, kt["tick_handler_cycles"]["avg"], "cycles")
        k = s.get("kernel") or {}
        if k:
            add("context switches / s", v, k.get("switches_per_second"), "1/s", "at the model clock")
            add("idle time", v, k.get("idle_percent"), "%")
        b = s.get("bench") or {}
        if b:
            add("benchmark cycles (raw)", v, b["bench_cycles"], "cycles", f"code in {b['code_region']}, {b['profile']}")
            add("measurement overhead", v, b["measure_overhead"], "cycles", "two back-to-back CYCCNT reads")
            add("benchmark cycles (corrected)", v, b["bench_cycles_corrected"], "cycles")
            add("benchmark CPI", v, b["cpi"], "cycles/instr")
            add("benchmark code size", v, b["static_code_bytes"], "bytes")
            add("XIP cache hits / misses", v, f"{b['xip_hits']} / {b['xip_misses']}", "")
        t = s.get("tick") or {}
        if t:
            add("tick period", v, t["period_cycles"], "cycles", f"RVR={t['reload']}")
            add("tick period", v, t["period_us"], "us", f"target {t['target_us']} us")
    return rows


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _get(d: Dict[str, Any], dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def assert_sequence(events: List[Dict[str, Any]], kinds: List[str], key: str = "kind") -> bool:
    """True if ``kinds`` appear in order (not necessarily adjacent) in ``events``.

    Each item may be ``"kind"`` or ``"kind:to"`` to also match the ``to`` field.
    """
    i = 0
    for e in events:
        want = kinds[i]
        if ":" in want:
            k, to = want.split(":", 1)
            ok = e.get(key) == k and (e.get("to") or "") == to
        else:
            ok = e.get(key) == want
        if ok:
            i += 1
            if i == len(kinds):
                return True
    return False


def evaluate_checks(scenario: Dict[str, Any], runs: List[Dict[str, Any]],
                    by_name: Dict[Optional[str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    first = runs[0]
    names = {r["name"] for r in runs}
    for spec in scenario["expected"].get("checks", []):
        spec = dict(spec)
        name = spec.pop("check")
        variant = spec.pop("variant", None)
        # a check about a variant that is not part of this run (r2s run --variant) is skipped
        if variant is not None and variant not in names:
            continue
        if name in ("variant_lt", "variant_gt", "latency_jitter_gt") and \
                not {spec.get("a"), spec.get("b")} <= names:
            continue
        run = next((r for r in runs if r["name"] == variant), first)
        s, events = run["summary"], run["events"]
        ok, detail = _check(name, spec, s, events, by_name)
        results.append({"check": name, "args": spec, "variant": variant, "ok": bool(ok), "detail": detail})
    return results


def _check(name: str, a: Dict[str, Any], s: Dict[str, Any], events: List[Dict[str, Any]],
           by_name: Dict[Optional[str], Dict[str, Any]]):
    faults = s.get("faults", [])
    last = faults[-1] if faults else None
    if name == "no_faults":
        return not faults, f"{len(faults)} fault(s)"
    if name == "fault_kind":
        return bool(last) and last["kind"] == a["kind"], (last["kind"] if last else "no fault")
    if name == "fault_flag":
        want = a["flag"]
        hit = [f for f in faults if f.get("flag") == want or want in sum(f.get("cfsr", {}).values(), [])]
        return bool(hit), ", ".join(f"{f['kind']}:{f.get('flag')}" for f in faults) or "no fault"
    if name == "fault_count":
        return len(faults) == a["count"], f"{len(faults)} fault(s)"
    if name == "fault_escalated_from":
        return bool(last) and last.get("escalated_from") == a["kind"], (last.get("escalated_from") if last else "no fault")
    if name == "fault_reason_contains":
        return bool(last) and a["text"] in last["reason"], (last["reason"] if last else "no fault")
    if name == "lockup":
        return s.get("lockup") == bool(a["value"]), f"lockup={s.get('lockup')}"
    if name == "boot_step":
        step = next((b for b in s.get("boot_steps", []) if b["step"] == a["step"]), None)
        return step is not None and step["ok"] == bool(a.get("ok", True)), (f"{a['step']}: ok={step['ok']}" if step else "step not reached")
    if name == "boot_complete":
        steps = {b["step"]: b["ok"] for b in s.get("boot_steps", [])}
        want = ["power_on", "vector_table", "reset_handler", "data_copied", "bss_zeroed", "main_entered"]
        return all(steps.get(w) for w in want), f"steps={steps}"
    if name == "uart_contains":
        return a["text"] in s.get("uart_output", ""), repr(s.get("uart_output", "")[:120])
    if name == "uart_not_contains":
        return a["text"] not in s.get("uart_output", ""), repr(s.get("uart_output", "")[:120])
    if name == "global_equals":
        v = s.get("globals", {}).get(a["name"])
        return v == a["value"], f"{a['name']}={v}"
    if name == "global_gt":
        v = s.get("globals", {}).get(a["name"])
        return v is not None and v > a["value"], f"{a['name']}={v}"
    if name == "global_ne":
        v = s.get("globals", {}).get(a["name"])
        return v is not None and v != a["value"], f"{a['name']}={v}"
    if name in ("metric_equals", "metric_between"):
        v = _get(s, a["metric"])
        if name == "metric_equals":
            return v == a["value"], f"{a['metric']}={v}"
        return v is not None and a["min"] <= v <= a["max"], f"{a['metric']}={v} (want {a['min']}..{a['max']})"
    if name == "event_contains":
        hit = [e for e in events if e["kind"] == a["kind"] and a["text"] in e["reason"]]
        return bool(hit), (hit[0]["reason"][:120] if hit else f"no {a['kind']} event mentions {a['text']!r}")
    if name == "no_event":
        n = sum(1 for e in events if e["kind"] == a["kind"] and (not a.get("to") or e.get("to") == a["to"]))
        return n == 0, f"{n} x {a['kind']}"
    if name == "boot_problem_contains":
        step = next((b for b in s.get("boot_steps", []) if b["step"] == "vector_table"), {})
        probs = step.get("problems", [])
        return any(a["text"] in p for p in probs), "; ".join(probs) or "no problems"
    if name == "event_sequence":
        return assert_sequence(events, a["kinds"]), f"sequence {a['kinds']}"
    if name == "min_events":
        n = sum(1 for e in events if e["kind"] == a["kind"] and (not a.get("to") or e.get("to") == a["to"]))
        return n >= a["count"], f"{n} x {a['kind']}"
    if name == "task_state":
        t = _get(s, f"kernel.tasks.{a['task']}") or {}
        return t.get("state") == a["state"], f"{a['task']}: {t.get('state')} ({t.get('kill_reason', '')})"
    if name == "task_alive":
        t = _get(s, f"kernel.tasks.{a['task']}") or {}
        return t.get("state") in ("READY", "RUNNING", "BLOCKED"), f"{a['task']}: {t.get('state')}"
    if name == "min_switches":
        n = _get(s, "kernel.switches") or 0
        return n >= a["count"], f"switches={n}"
    if name == "tail_chains_min":
        return (s.get("tail_chains") or 0) >= a["count"], f"tail_chains={s.get('tail_chains')}"
    if name == "watermark_min_percent":
        w = _get(s, f"kernel.tasks.{a['task']}.watermark.percent")
        return w is not None and w >= a["value"], f"{a['task']} watermark={w}%"
    if name == "stop_reason_contains":
        return a["text"] in (s.get("stop_reason") or ""), s.get("stop_reason")
    if name in ("variant_lt", "variant_gt"):
        va, vb = _get(by_name[a["a"]], a["metric"]), _get(by_name[a["b"]], a["metric"])
        ok = (va < vb) if name == "variant_lt" else (va > vb)
        return va is not None and vb is not None and ok, f"{a['metric']}: {a['a']}={va} {a['b']}={vb}"
    if name == "latency_jitter_gt":
        ja, jb = _get(by_name[a["a"]], "latency.jitter"), _get(by_name[a["b"]], "latency.jitter")
        return ja is not None and jb is not None and ja > jb, f"jitter {a['a']}={ja} {a['b']}={jb}"
    if name == "priority_preempted":
        v = _get(s, "priority.button_preempted_timer")
        return v == bool(a["value"]), f"button_preempted_timer={v}"
    return False, f"unknown check '{name}'"


# ---------------------------------------------------------------------------
# Trace output
# ---------------------------------------------------------------------------
def result_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """What goes into the trace file (drops the Python-only aliases)."""
    return {k: v for k, v in result.items() if k not in ("checks", "passed")}


def save_result(result: Dict[str, Any], directory: Path) -> Path:
    path = Path(directory) / f"{result['scenario']['id']}.json"
    write_trace(path, result_payload(result))
    return path
