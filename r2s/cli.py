"""Command-line interface.

    r2s list                                   scenarios, grouped by layer
    r2s explain P07                            learning notes of one scenario
    r2s run P06 [--trace] [--kinds a,b] [--hidden] [--reveal] [--seed N]
    r2s run-all [--out DIR] [--docs DIR]       every scenario + viewer manifest
    r2s disasm blink|P08                       annotated listing, vector table, memory map
    r2s regs P08 [--at-cycle N]                register dump, decoded
    r2s stack P08 [--at-cycle N]               task stacks, saved frames, watermarks
    r2s fault P07                              fault records decoded (CFSR/HFSR/MMFAR/BFAR)
    r2s quiz [--seed N] [--answer K]           guess the bug from the symptoms
    r2s bench                                  P10 / P11 comparison tables

``python -m r2s ...`` works the same. Console output is plain ASCII;
``--color`` adds ANSI colours (off by default so every terminal is safe).

Author: Farzana Akhter
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from . import faults as fault_lib
from .cpu import FAULT_BITS, HFSR_BITS, decode_cfsr, decode_exc_return
from .isa import disassemble
from .isa_profiles import get_profile
from .kernel import WATERMARK
from .nvic import exc_name
from .programs import BUILDERS
from .scenarios import (LAYERS, SCENARIO_DIR, build_machine, find_scenario, layer_label, list_scenarios,
                        load_scenario, result_payload, run_scenario, run_variant, save_result)
from .trace import KINDS, format_event

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = REPO_ROOT / "traces"
DOCS_TRACE_DIR = REPO_ROOT / "docs" / "traces"


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def ok(self, text: str) -> str:
        return self.c("32", text)

    def bad(self, text: str) -> str:
        return self.c("31", text)

    def dim(self, text: str) -> str:
        return self.c("2", text)

    def bold(self, text: str) -> str:
        return self.c("1", text)


def _ascii(text: Any) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _wrap(text: str, width: int = 96, indent: str = "    ") -> str:
    return "\n".join(textwrap.wrap(" ".join(_ascii(text).split()), width=width, initial_indent=indent,
                                   subsequent_indent=" " * len(indent)))


def _bullets(items: List[str], numbered: bool = False) -> None:
    for i, item in enumerate(items, start=1):
        head = f"{i}. " if numbered else "- "
        print(textwrap.fill(" ".join(_ascii(item).split()), width=96, initial_indent="    " + head,
                            subsequent_indent="    " + " " * len(head)))


def _hex(v: Any) -> int:
    return int(v, 16) if isinstance(v, str) else int(v)


def _scenario(args: argparse.Namespace) -> Dict[str, Any]:
    return load_scenario(find_scenario(args.scenario, Path(args.scenarios)))


def _variant(sc: Dict[str, Any], name: Optional[str]) -> Optional[Dict[str, Any]]:
    variants = sc.get("variants") or []
    if not variants:
        if name:
            raise SystemExit(f"error: {sc['id']} has no variants")
        return None
    if name is None:
        return variants[0]
    for v in variants:
        if v.get("name") == name:
            return v
    raise SystemExit(f"error: {sc['id']} has no variant '{name}'; choose from "
                     f"{', '.join(v['name'] for v in variants)}")


# ---------------------------------------------------------------------------
# Summary printing
# ---------------------------------------------------------------------------
def _print_unit_state(summary: Dict[str, Any], pal: Palette) -> None:
    print("  Final state per unit")
    print(f"    cpu      : {summary.get('stop_reason')}")
    print(f"               cycles={summary.get('cycles')} ({summary.get('time_us')} us), "
          f"instret={summary.get('instret')}, CPI={summary.get('cpi') or 'n/a'}, lockup={summary.get('lockup')}")
    regs = summary.get("final_registers", {})
    if regs:
        print(f"               pc={regs.get('pc')} lr={regs.get('lr')} xPSR={regs.get('xpsr')} "
              f"mode={regs.get('mode')} {'privileged' if regs.get('privileged') else 'unprivileged'} "
              f"sp={regs.get('sp_sel')}")
    steps = summary.get("boot_steps", [])
    step_text = ", ".join(f"{s['step']}{'' if s['ok'] else '(FAIL)'}" for s in steps)
    print(f"    boot     : {step_text}" + (f"  [reset to main: {summary.get('cycles_to_main')} cycles]"
                                           if summary.get("cycles_to_main") is not None else ""))
    crit = summary.get("critical_sections") or {}
    print(f"    nvic     : exception entries={summary.get('exception_entries')}, "
          f"tail-chains={summary.get('tail_chains')}, PRIMASK regions={crit.get('count', 0)} "
          f"(longest {crit.get('longest_cycles', 0)} cycles)")
    print(f"    systick  : ticks={summary.get('systick_ticks')}")
    lines = summary.get("uart_lines", [])
    print(f"    uart     : {len(lines)} line(s)")
    for line in lines[:8]:
        print(f"               | {_ascii(line)[:80]}")
    if len(lines) > 8:
        print(f"               | ... {len(lines) - 8} more")
    print(f"    gpio     : edges per pin {summary.get('gpio_toggles')}")
    markers = summary.get("markers") or []
    if markers:
        print(f"    markers  : {len(markers)} x 'mark' in the program, first: {_ascii(markers[0])[:70]!r}")
    watch = {n: v for n, v in (summary.get("globals") or {}).items()}
    if watch:
        print("    globals  : " + ", ".join(f"{n}={v}" for n, v in list(watch.items())[:8]))
    xip = summary.get("xip_cache", {})
    print(f"    xip cache: hits={xip.get('hits')} misses={xip.get('misses')} enabled={xip.get('enabled')}")
    fr = summary.get("fault_registers", {})
    faults = summary.get("faults", [])
    if faults:
        print(f"    faults   : {len(faults)} record(s); CFSR={fr.get('CFSR')} HFSR={fr.get('HFSR')} "
              f"MMFAR={fr.get('MMFAR')} BFAR={fr.get('BFAR')}")
        for f in faults:
            esc = f" (escalated from {f['escalated_from']})" if f.get("escalated_from") else ""
            print(f"      - {f['kind']}{esc} {f['flag']} at PC={f['pc']} '{_ascii(f['instruction'])}' "
                  f"in {f['context']}")
            print(_wrap(f["detail"], indent="          "))
        print("      (decode them with: r2s fault <id>)")
    else:
        print("    faults   : none")
    kernel = summary.get("kernel")
    if kernel:
        print(f"    kernel   : {kernel['scheduler']} ticks={kernel['ticks']} switches={kernel['switches']} "
              f"idle={kernel['idle_percent']}% kernel={kernel['kernel_percent']}% mpu={kernel['mpu']} "
              f"PendSV prio={kernel.get('pendsv_priority')}")
        for name, t in kernel["tasks"].items():
            w = t["watermark"]
            print(f"      task {name:<5} {t['state']:<8} run={t['run_percent']:>5}%  stack {w['used_bytes']:>3}/"
                  f"{w['size_bytes']} B ({w['percent']:>5}%)  in={t['switches_in']:<3} {_ascii(t['kill_reason'])[:48]}")
        kt = summary.get("kernel_timing", {})
        sc = kt.get("switch_cost_cycles", {})
        if sc.get("n"):
            print(f"      context switch: avg {sc['avg']} cycles (min {sc['min']}, max {sc['max']}, n={sc['n']}); "
                  f"tick handler avg {kt['tick_handler_cycles'].get('avg')} cycles")
    lat = summary.get("latency")
    if lat and lat.get("samples"):
        print(f"    latency  : n={lat['samples']} min={lat['min']} avg={lat['avg']} max={lat['max']} "
              f"jitter={lat['jitter']} cycles ({lat['min_us']}..{lat['max_us']} us)")
        print(f"               histogram {lat['histogram']}")
    bench = summary.get("bench")
    if bench:
        print(f"    bench    : {bench['code_region']}/{bench['profile']} raw={bench['bench_cycles']} "
              f"overhead={bench['measure_overhead']} corrected={bench['bench_cycles_corrected']} cycles, "
              f"instret={bench['instret']} CPI={bench['cpi']} code={bench['static_code_bytes']} B")
    prio = summary.get("priority")
    if prio:
        print(f"    priority : button waited {prio['button_latency']} cycles, pre-empted timer="
              f"{prio['button_preempted_timer']}, tail-chains={prio['tail_chains']}")
    tick = summary.get("tick")
    if tick:
        print(f"    tick     : RVR={tick['reload']} period={tick['period_cycles']} cycles = {tick['period_us']} us "
              f"(target {tick['target_us']} us, core at {tick['actual_hz'] // 1_000_000} MHz)")


def print_summary(result: Dict[str, Any], pal: Palette, hidden: bool, reveal: bool) -> None:
    sc = result["scenario"]
    secret = hidden and not reveal
    variants = sc.get("variants") or []
    symptom = (variants[0].get("hidden_symptom") if len(variants) == 1 else None) or sc.get("hidden_symptom")
    title = (symptom or sc["title"]) if secret else sc["title"]
    print()
    print(pal.bold(f"== {sc['id']}: {_ascii(title)}"))
    if not secret:
        print(_wrap(sc["description"]))
        injected = sorted({f for s in _runs(result) for f in s.get("faults_injected", [])})
        if injected:
            print("  Injected faults:")
            for f in injected:
                print(_wrap(f, indent="    - "))
    else:
        print("    (hidden mode: symptoms only. What would you check first?)")
    print()
    summary = result["summary"]
    if "variants" in summary:
        for name, s in summary["variants"].items():
            print(pal.bold(f"  -- variant: {name}"))
            _print_unit_state(s, pal)
    else:
        _print_unit_state(summary, pal)
    print()
    checks = result["result"]["checks"]
    passed = result["result"]["passed"]
    verdict = pal.ok("PASS") if passed else pal.bad("FAIL")
    if secret:
        print(f"  Result: {verdict}  ({sum(c['passed'] for c in checks)}/{len(checks)} checks as expected; "
              f"check names hidden)")
        print()
        print(pal.dim(f"  Run 'r2s run {sc['id']} --hidden --reveal' (or 'r2s explain {sc['id']}') for the "
                      f"root cause and the explanation."))
        return
    print("  Checks vs expected outcome")
    for c in checks:
        tag = pal.ok("PASS") if c["passed"] else pal.bad("FAIL")
        variant = f"[{c['variant']}] " if c.get("variant") else ""
        args = " ".join(f"{k}={v}" for k, v in c["args"].items())
        print(_wrap(f"{tag}  {variant}{c['name']} {args}  -> {c['detail']}", indent="    "))
    print(f"  Result: {verdict}")
    print(_wrap(f"expected: {sc['expected'].get('summary', '')}", indent="    "))
    print()
    print("  What this means")
    print(_wrap(sc.get("what_this_means", "")))


def _runs(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    s = result["summary"]
    return list(s["variants"].values()) if "variants" in s else [s]


def print_explain(sc: Dict[str, Any]) -> None:
    print(f"== {sc['id']}: {_ascii(sc['title'])}   [{layer_label(sc.get('layer', ''))}]")
    print()
    print("Description")
    print(_wrap(sc["description"]))
    print()
    print("Expected outcome")
    print(_wrap(sc["expected"].get("summary", "")))
    print("    checks:")
    for c in sc["expected"].get("checks", []):
        c = dict(c)
        name = c.pop("check")
        variant = c.pop("variant", None)
        print((f"      - {('[' + variant + '] ') if variant else ''}{name} "
               + " ".join(f"{k}={v}" for k, v in c.items())).rstrip())
    faults = list(sc.get("faults") or [])
    for v in sc.get("variants") or []:
        faults += [dict(f, variant=v["name"]) for f in v.get("faults") or []]
    if faults:
        print()
        print("Injected faults")
        for f in faults:
            where = f" (variant {f.pop('variant')})" if "variant" in f else ""
            print(_wrap(fault_lib.describe(f) + where, indent="    - "))
    print()
    print("What to look for")
    print(_wrap(sc["what_to_look_for"]))
    print()
    print("Likely root causes on real hardware")
    _bullets(sc["likely_root_causes"])
    print()
    print("How to debug")
    _bullets(sc["how_to_debug"], numbered=True)
    print()
    print("What this means")
    print(_wrap(sc.get("what_this_means", "")))
    print()
    print(f"Try: r2s run {sc['id']} --trace    r2s fault {sc['id']}    r2s stack {sc['id']}    r2s disasm {sc['id']}")


# ---------------------------------------------------------------------------
# Commands: list / explain / run / run-all / bench
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    scenarios = [load_scenario(p) for p in list_scenarios(Path(args.scenarios))]
    for layer in LAYERS:
        group = [s for s in scenarios if s.get("layer") == layer]
        if not group:
            continue
        print(layer_label(layer))
        for sc in group:
            faults = [f["type"] for f in sc.get("faults") or []]
            for v in sc.get("variants") or []:
                faults += [f["type"] for f in v.get("faults") or []]
            variants = f" ({len(sc['variants'])} variants)" if sc.get("variants") else ""
            print(f"  {sc['id']:<4} {sc['program']:<8} {_ascii(sc['title'])}{variants}")
            if faults:
                print(f"       faults: {', '.join(dict.fromkeys(faults))}")
    print()
    print("r2s explain <ID> prints the learning notes; r2s run <ID> --trace shows the event log.")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    print_explain(_scenario(args))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pal = Palette(args.color)
    sc = _scenario(args)
    if args.variant:
        _variant(sc, args.variant)
        sc = dict(sc, variants=[v for v in sc["variants"] if v["name"] == args.variant])
    result = run_scenario(sc, seed=args.seed)
    out = save_result(result, Path(args.out))
    secret = args.hidden and not args.reveal
    if args.trace:
        kinds = set(k.strip() for k in args.kinds.split(",")) if args.kinds else None
        if kinds and not kinds <= set(KINDS):
            print(f"error: unknown kind(s) {sorted(kinds - set(KINDS))}; known: {', '.join(KINDS)}", file=sys.stderr)
            return 2
        shown = [e for e in result["events"] if kinds is None or e["kind"] in kinds]
        if secret:
            shown = [e for e in shown if e["kind"] != "scenario"]
        print(f"== event log ({len(shown)} of {len(result['events'])} events)"
              + ("" if kinds is None else f" kinds={','.join(sorted(kinds))}"))
        print(f"{'cycles':>12} {'time':>11}  {'kind':<16} {'from':>14} -> {'to':<16} reason")
        variant = None
        for e in shown:
            v = e["details"].get("variant")
            if v != variant:
                variant = v
                print(f"-- variant {v}")
            print(format_event(e, width=args.width))
    print_summary(result, pal, args.hidden, args.reveal)
    print(f"  Trace written to {out}")
    return 0 if result["passed"] else 1


def cmd_run_all(args: argparse.Namespace) -> int:
    pal = Palette(args.color)
    out_dir = Path(args.out)
    docs_dir = Path(args.docs) if args.docs else None
    index: List[Dict[str, Any]] = []
    all_ok = True
    for path in list_scenarios(Path(args.scenarios)):
        sc = load_scenario(path)
        result = run_scenario(sc)
        save_result(result, out_dir)
        if docs_dir is not None:
            save_result(result, docs_dir)
        n_events = len(result["events"])
        status = pal.ok("PASS") if result["passed"] else pal.bad("FAIL")
        all_ok &= result["passed"]
        print(f"{status}  {sc['id']}  {_ascii(sc['title'])[:66]:<66} events={n_events}")
        index.append({
            "id": sc["id"], "title": sc["title"], "layer": sc["layer"], "file": f"{sc['id']}.json",
            "program": sc["program"], "passed": result["passed"], "events": n_events,
            "variants": result.get("variants", []),
            "faults": sorted({f["type"] for f in sc.get("faults") or []} |
                             {f["type"] for v in sc.get("variants") or [] for f in v.get("faults") or []}),
            "hidden_symptom": sc.get("hidden_symptom", ""),
            "expected": sc["expected"].get("summary", ""),
        })
    manifest = {"project": "reset-to-scheduler-sim", "version": __version__, "scenarios": index}
    for directory in [out_dir] + ([docs_dir] if docs_dir else []):
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "index.json").open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=False)
    print(f"{sum(1 for i in index if i['passed'])}/{len(index)} scenarios as expected; traces in {out_dir}"
          + (f" and {docs_dir}" if docs_dir else ""))
    return 0 if all_ok else 1


def cmd_bench(args: argparse.Namespace) -> int:
    """P10 and P11 as tables."""
    for ident in ("P10", "P11"):
        sc = load_scenario(find_scenario(ident, Path(args.scenarios)))
        result = run_scenario(sc)
        print()
        print(f"== {sc['id']}: {_ascii(sc['title'])}")
        print(f"{'variant':<15} {'region':<6} {'profile':<10} {'raw cyc':>8} {'corr.':>7} {'instret':>8} "
              f"{'CPI':>6} {'code B':>7} {'xip hit':>8} {'miss':>5} {'us':>7}")
        for name, s in result["summary"]["variants"].items():
            b = s["bench"]
            print(f"{name:<15} {b['code_region']:<6} {b['profile']:<10} {b['bench_cycles']:>8} "
                  f"{b['bench_cycles_corrected']:>7} {b['instret']:>8} {b['cpi']:>6} {b['static_code_bytes']:>7} "
                  f"{b['xip_hits']:>8} {b['xip_misses']:>5} {b['bench_us']:>7}")
        print(_wrap(sc.get("what_this_means", "")))
    print()
    print("Note: the cost tables are illustrative models for learning and demonstration, "
          "not measurements of real cores.")
    return 0


# ---------------------------------------------------------------------------
# disasm
# ---------------------------------------------------------------------------
def _machine_for(target: str, args: argparse.Namespace):
    """A linked machine for a builder name (blink, kernel...) or a scenario id."""
    if target in BUILDERS:
        sc = {"id": target, "title": f"program '{target}' with default options", "program": target}
        return sc, build_machine(sc, None)
    sc = load_scenario(find_scenario(target, Path(args.scenarios)))
    return sc, build_machine(sc, _variant(sc, getattr(args, "variant", None)))


def cmd_disasm(args: argparse.Namespace) -> int:
    sc, m = _machine_for(args.target, args)
    profile = get_profile(args.profile)
    img = m.image
    from .boot import write_image
    write_image(m.memory, img, m.faults)
    sym_by_addr: Dict[int, List[str]] = {}
    for name, addr in img.symbols.items():
        sym_by_addr.setdefault(addr, []).append(name)
    print(f"== {sc['id']}: annotated listing ({_ascii(sc['title'])})")
    print(f"   cost columns: cycles / code bytes under the '{profile.name}' profile (illustrative)")
    print()
    print("Memory map")
    for r in m.memory.regions:
        attrs = ("R" if r.readable else "-") + ("W" if r.writable else "-") + ("X" if r.executable else "-")
        print(f"  0x{r.base:08X}-0x{r.end - 1:08X}  {r.name:<7} {attrs} "
              f"{'privileged only' if r.privileged_only else ''}")
    print()
    print(f"Vector table at 0x{img.vector_base:08X} (bit 0 of every handler entry = Thumb bit)")
    for number in range(0, 16 + 22):
        if number in (7, 8, 9, 10, 13) or (16 <= number < 37 and number != 16):
            continue
        word = m.memory.peek(img.vector_base + 4 * number)
        if not isinstance(word, int):
            continue
        if number == 0:
            print(f"  [ 0] initial MSP          0x{word:08X}")
            continue
        target = ", ".join(sym_by_addr.get(word & ~1, ["?"]))
        thumb = "ok" if word & 1 else "BIT 0 CLEAR -> INVSTATE"
        print(f"  [{number:>2}] {exc_name(number):<20} 0x{word:08X}  -> {target:<16} T={word & 1} {thumb}")
    print("  (IRQ 1..20 and 22..31 omitted: they point at Default_Handler in every program here)")
    print()
    for section in img.sections:
        print(f"Section {section.name} in {section.region.upper()} at 0x{section.base:08X} "
              f"({section.program.size_words} instructions)")
        listing = disassemble(section.program, section.base, comments=True).splitlines()
        index = 0
        for line in listing:
            if line.endswith(">:"):
                print(line)
                continue
            instr = section.program.instructions[index]
            index += 1
            cyc, _, size = profile.cost(instr)
            code, _, comment = line.partition(" ; ")
            cost = f"{cyc:>2}c {size:>2}B" if instr.op not in ("mark", "kcall") else " pseudo"
            if instr.op == "kcall" and cyc:
                cost = f"{cyc:>2}c  C"
            print(f"{code:<52} {cost}" + (f"  ; {comment.strip()}" if comment else ""))
        print()
    print("Globals")
    for name, value in img.data.items():
        print(f"  .data {name:<16} 0x{img.symbols[name]:08X}  initial value 0x{value:08X} "
              f"(load copy in flash at 0x{img.data_load + 4 * list(img.data).index(name):08X})")
    for name in img.bss:
        print(f"  .bss  {name:<16} 0x{img.symbols[name]:08X}  zeroed by the startup code")
    if img.reserve:
        print("SRAM reserved by the kernel (32-byte aligned for the MPU)")
        for name, (base, size) in img.reserve.items():
            print(f"  {name:<16} 0x{base:08X}..0x{base + size:08X} ({size} B)")
    print(f"Linker symbols: __data_load=0x{img.data_load:08X} __data_start=0x{img.data_start:08X} "
          f"__data_end=0x{img.data_end:08X} __bss_start=0x{img.bss_start:08X} __bss_end=0x{img.bss_end:08X} "
          f"__stack_top=0x{img.initial_sp:08X}")
    print("Pseudo-ops (mark, kcall, gpio_set, uart_puts...) are not Thumb-2 instructions; see r2s/isa.py.")
    return 0


# ---------------------------------------------------------------------------
# regs / stack / fault: run to a point and inspect
# ---------------------------------------------------------------------------
def _run_to(args: argparse.Namespace):
    sc = _scenario(args)
    variant = _variant(sc, args.variant)
    run = run_variant(sc, variant, seed=getattr(args, "seed", None), stop_at_cycle=getattr(args, "at_cycle", None))
    return sc, variant, run


def _decode_xpsr(x: int) -> str:
    ipsr = x & 0x1FF
    flags = " ".join(f"{n}={(x >> b) & 1}" for n, b in (("N", 31), ("Z", 30), ("C", 29), ("V", 28)))
    where = "thread mode" if ipsr == 0 else f"handler for exception {ipsr} ({exc_name(ipsr)})"
    pad = "  [bit 9: pad word in this frame]" if x & (1 << 9) else ""
    return f"{flags} T={(x >> 24) & 1} IPSR={ipsr} -> {where}{pad}"


def cmd_regs(args: argparse.Namespace) -> int:
    sc, variant, run = _run_to(args)
    m = run["machine"]
    cpu = m.cpu
    vname = f" variant {variant['name']}" if variant else ""
    print(f"== {sc['id']}{vname}: registers at cycle {cpu.cycles} ({run['summary']['stop_reason']})")
    print(f"   context: {cpu.context()} | {'handler' if cpu.in_handler else 'thread'} mode, "
          f"{'privileged' if cpu.privileged else 'unprivileged'}, SP = {cpu.sp_name.upper()}")
    print()
    names = [f"r{i}" for i in range(13)]
    for i in range(0, 13, 4):
        print("   " + "   ".join(f"{n:<3} 0x{cpu.regs[n]:08X}" for n in names[i:i + 4]))
    print(f"   sp  0x{cpu.sp:08X}  ({cpu.sp_name})     msp 0x{cpu.msp:08X}   psp 0x{cpu.psp:08X}")
    lr_note = decode_exc_return(cpu.lr) if cpu.lr >= 0xFFFFFFF0 else (
        "return address (bit 0 = Thumb)" if cpu.lr & 1 else "")
    print(f"   lr  0x{cpu.lr:08X}  {lr_note}")
    word = m.memory.peek(cpu.pc)
    print(f"   pc  0x{cpu.pc:08X}  next: {_ascii(word) if hasattr(word, 'op') else '(not code)'}")
    print(f"   xPSR 0x{cpu.xpsr:08X}  {_decode_xpsr(cpu.xpsr)}")
    print(f"   CONTROL {cpu.control}  nPRIV={cpu.control & 1} ({'unprivileged' if cpu.control & 1 else 'privileged'} "
          f"thread)  SPSEL={(cpu.control >> 1) & 1} (thread uses {'PSP' if cpu.control & 2 else 'MSP'})")
    print(f"   PRIMASK {int(m.nvic.primask)}  active exceptions: "
          f"{', '.join(exc_name(n) for n in m.nvic.active) or 'none'}  pending: "
          f"{', '.join(exc_name(n) for n in m.nvic.pending) or 'none'}")
    print(f"   CYCCNT {cpu.cycles}   instret {cpu.instret}")
    print()
    print("   GDB on the board: 'info registers', 'p/x $xpsr', 'p $control', 'p/x $msp', 'p/x $psp'.")
    return 0


def _frame_labels(layout: Dict[str, Any]) -> List[str]:
    return [*layout["sw_frame"], *layout["hw_frame"], "(above frame)"]


def cmd_stack(args: argparse.Namespace) -> int:
    sc, variant, run = _run_to(args)
    m = run["machine"]
    cpu = m.cpu
    vname = f" variant {variant['name']}" if variant else ""
    print(f"== {sc['id']}{vname}: stacks at cycle {cpu.cycles} ({run['summary']['stop_reason']})")
    kernel = m.kernel
    if kernel is not None and kernel.started:
        layout = kernel.layout()
        labels = _frame_labels(layout)
        print(f"   Suspended task stack, from the saved PSP upwards: software frame {{{', '.join(layout['sw_frame'])}}} "
              f"pushed by PendSV, then the hardware frame (r0-r3, r12, lr, pc, xPSR).")
        if len(layout["sw_frame"]) != 8:
            print(f"   NOTE: the software frame has {len(layout['sw_frame'])} words, not 8: every word above it is "
                  f"shifted compared with the correct layout.")
        for task in kernel.tasks:
            w = kernel.watermark(task)
            bar_len = 20
            filled = int(round(bar_len * w["percent"] / 100))
            print()
            print(f"   Task {task.name:<5} {task.state:<8} stack 0x{task.stack_base:08X}..0x{task.stack_top:08X} "
                  f"guard 0x{task.guard_base:08X}  watermark [{'#' * filled}{'.' * (bar_len - filled)}] "
                  f"{w['used_bytes']}/{w['size_bytes']} B")
            if task.state == "DEAD":
                print(f"     dead: {_ascii(task.kill_reason)[:100]}")
                continue
            base, rows = task.psp, labels
            if task is kernel.current:
                if not cpu.in_handler:
                    print(f"     running: all its registers are live in the CPU, PSP=0x{cpu.psp:08X} "
                          f"(the saved PSP in its TCB is stale until the next switch)")
                    continue
                if task.stack_base <= cpu.psp < task.stack_top:
                    print(f"     current task, interrupted: r4-r11 are live in the CPU (not saved); the hardware "
                          f"frame is at the live PSP=0x{cpu.psp:08X}")
                    base, rows = cpu.psp, [*layout["hw_frame"], "(above frame)"]
                else:
                    print(f"     being switched in: the live PSP (0x{cpu.psp:08X}) still points at the previous "
                          f"task; its saved frame is at the TCB PSP")
            for i, label in enumerate(rows):
                addr = base + 4 * i
                if addr >= task.stack_top:
                    break
                raw = m.memory.peek(addr)
                value = f"0x{raw:08X}" if isinstance(raw, int) else "code"
                note = ""
                if label == "xpsr" and value.startswith("0x"):
                    note = _decode_xpsr(int(value, 16))
                elif label in ("pc", "lr") and value.startswith("0x"):
                    syms = [n for n, a in m.image.symbols.items() if a == (int(value, 16) & ~1)]
                    note = f"<{syms[0]}>" if syms else ""
                elif value == f"0x{WATERMARK:08X}":
                    note = "watermark pattern (never written)"
                print(f"     0x{addr:08X}  {label:<14} {value:<11} {note}")
    else:
        sp = cpu.msp
        top = m.image.initial_sp
        region = m.memory.region_for(sp)
        if region is None or region.name != "SRAM" or sp > top:
            print(f"   No kernel tasks: one stack, MSP. MSP=0x{sp:08X} is NOT inside the SRAM stack below "
                  f"__stack_top=0x{top:08X}: the stack pointer itself is invalid (see r2s fault {sc['id']}).")
        else:
            print(f"   No kernel tasks: one stack, MSP. MSP=0x{sp:08X}, top (__stack_top)=0x{top:08X}, "
                  f"used {top - sp} bytes")
        last = next((e for e in reversed(run["events"]) if e["kind"] in ("exception_entry",)
                     and e["details"].get("stacked_frame")), None)
        if last:
            d = last["details"]
            base = int(d["frame_at"], 16)
            print(f"   Last exception frame: {d['name']} at cycle {last['t_cycles']}, frame at {d['frame_at']} on {d['stack']}")
            for i, (name, value) in enumerate(d["stacked_frame"].items()):
                note = _decode_xpsr(int(value, 16)) if name == "xpsr" else ""
                print(f"     0x{base + 4 * i:08X}  {name:<5} {value}  {note}")
        if top > sp and m.memory.region_for(sp):
            print("   Live stack words (MSP upwards):")
            for addr in range(sp, min(top, sp + 4 * 16), 4):
                print(f"     0x{addr:08X}  0x{m.memory.peek(addr) if isinstance(m.memory.peek(addr), int) else 0:08X}")
    print()
    print("   GDB on the board: 'x/16xw $psp' (task) or 'x/16xw $sp'; compare with the layout above.")
    return 0


_FLAG_ADVICE = {
    "IACCVIOL": "The PC itself went somewhere the MPU forbids, or into an execute-never part of the default "
                "memory map (peripherals, device or system space): a corrupted return address or function "
                "pointer. MMFAR is NOT valid; look at the stacked PC.",
    "DACCVIOL": "A load/store hit an address the MPU does not allow for this privilege level. MMFAR is the address: "
                "compare it with the MPU regions. Just below a task stack = stack overflow into the guard.",
    "MSTKERR": "The core could not push the exception frame: the stack pointer is already inside a protected "
               "region. Almost always a stack overflow; the frame is lost, so the stacked PC is unreliable.",
    "MUNSTKERR": "The core could not pop the frame on return: the saved stack pointer is wrong.",
    "PRECISERR": "Precise bus error: the stacked PC is the load/store that failed and BFAR is the address. "
                 "Unmapped address, a write to flash, or an unprivileged access to the System Control Space.",
    "IBUSERR": "Bus error on an instruction fetch (unmapped or erroring memory): a bad branch target, "
               "corrupted return address or vector. BFAR is NOT valid; look at the stacked PC.",
    "STKERR": "The frame push failed on the bus: the stack pointer points at invalid memory (bad initial SP, "
              "corrupted PSP, stack run off the end of RAM).",
    "UNSTKERR": "Popping the frame failed on the bus: the saved stack pointer is wrong.",
    "UNDEFINSTR": "The core fetched something that is not an instruction: the PC reached data or erased flash.",
    "INVSTATE": "Execution with the Thumb bit clear: a vector or branch target without bit 0 set. The stacked PC "
                "is the bad target address.",
    "INVPC": "Bad exception return: an invalid EXC_RETURN in LR, or a stacked frame that was overwritten "
             "(stacked IPSR does not match the mode returned to).",
    "UNALIGNED": "Unaligned access the core does not allow (LDM/STM always, LDR/STR with CCR.UNALIGN_TRP).",
    "FORCED": "Escalated to HardFault: the real cause is in CFSR. Either the fault's handler is disabled in "
              "SHCSR (default after reset) or the fault happened where its priority could not pre-empt.",
    "VECTTBL": "The vector table could not be read: VTOR points at invalid memory.",
}


def cmd_fault(args: argparse.Namespace) -> int:
    sc, variant, run = _run_to(args)
    s = run["summary"]
    vname = f" variant {variant['name']}" if variant else ""
    faults = s.get("faults", [])
    print(f"== {sc['id']}{vname}: fault records ({len(faults)})")
    if not faults:
        print("   No faults were recorded in this run. If the board misbehaves without faulting, look at")
        print("   boot steps (r2s run <id> --trace --kinds boot_step), globals and markers instead.")
        return 0
    fr = s.get("fault_registers", {})
    print(f"   Final status registers: CFSR={fr.get('CFSR')} HFSR={fr.get('HFSR')} MMFAR={fr.get('MMFAR')} "
          f"BFAR={fr.get('BFAR')} (sticky until written with 1s)")
    for i, f in enumerate(faults, start=1):
        print()
        esc = f", escalated from {f['escalated_from']}" if f.get("escalated_from") else ""
        print(f"-- record {i}: {f['kind']}{esc} at cycle {f['t_cycles']}, context {f['context']} "
              f"({'privileged' if f['privileged'] else 'unprivileged'})")
        print(f"   faulting instruction: PC={f['pc']}  '{_ascii(f['instruction'])}'")
        print(f"   cause: {_ascii(f['detail'])}")
        print(f"   CFSR={f['cfsr_value']} HFSR={f['hfsr_value']} at this point; bits set:")
        for bit in f["decoded"]:
            print(_wrap(f"{bit['register']}.{bit['name']} (bit {bit['bit']}): {bit['meaning']}", indent="     - "))
        if f.get("mmfar"):
            print(f"   MMFAR={f['mmfar']} (MMARVALID)")
        if f.get("bfar"):
            print(f"   BFAR={f['bfar']} (BFARVALID)")
        if f.get("address") and not (f.get("mmfar") or f.get("bfar")):
            print(f"   address involved: {f['address']} (not latched in MMFAR/BFAR for this kind of fault)")
        print(f"   {f['sp']} when the fault was taken")
        if f.get("exc_return"):
            print(f"   EXC_RETURN={f['exc_return']}: {decode_exc_return(_hex(f['exc_return']))}")
        frame = f.get("stacked_frame")
        if frame:
            print("   stacked frame: " + " ".join(f"{k}={v}" for k, v in frame.items() if k not in ("xpsr",)))
            print(f"                  xPSR={frame['xpsr']}: {_decode_xpsr(_hex(frame['xpsr']))}")
        else:
            print("   stacked frame: none (stacking failed, so the handler cannot trust the stack)")
        print("   what it tells you:")
        print(_wrap(_FLAG_ADVICE.get(f["flag"], ""), indent="     "))
    print()
    print("   On the board (GDB): 'x/xw 0xE000ED28' (CFSR), 'x/xw 0xE000ED2C' (HFSR), 'x/xw 0xE000ED34' (MMFAR),")
    print("   'x/xw 0xE000ED38' (BFAR), 'p/x $lr' (EXC_RETURN), then 'x/8xw $psp' or 'x/8xw $msp' for the frame.")
    return 0


# ---------------------------------------------------------------------------
# quiz
# ---------------------------------------------------------------------------
def _quiz_pool(directory: Path) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]]:
    pool = []
    for path in list_scenarios(directory):
        sc = load_scenario(path)
        if sc.get("faults"):
            answer = " + ".join(fault_lib.FAULT_CATALOG[f["type"]] for f in sc["faults"])
            pool.append((sc, None, answer))
        for v in sc.get("variants") or []:
            if v.get("faults"):
                answer = " + ".join(fault_lib.FAULT_CATALOG[f["type"]] for f in v["faults"])
                pool.append((sc, v, answer))
    return pool


def cmd_quiz(args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 10_000)
    rng = random.Random(seed)
    pool = _quiz_pool(Path(args.scenarios))
    sc, variant, answer = pool[rng.randrange(len(pool))]
    others = sorted({a for _, _, a in pool if a != answer})
    choices = rng.sample(others, 3) + [answer]
    rng.shuffle(choices)
    correct = choices.index(answer) + 1
    one = dict(sc, variants=[variant]) if variant else sc
    result = run_scenario(one)
    s = _runs(result)[0]
    print(f"== Quiz (seed {seed}): what is the bug?")
    print()
    symptom = (variant or {}).get("hidden_symptom") or sc["hidden_symptom"]
    print(_wrap(f"Symptom: {symptom}"))
    print()
    print("  What the board shows")
    lines = s.get("uart_lines", [])
    print(f"    UART ({len(lines)} lines): " + " | ".join(_ascii(l)[:40] for l in lines[:4])
          + (" | ..." if len(lines) > 4 else ""))
    print(f"    stopped because: {s.get('stop_reason')}")
    for f in s.get("faults", [])[:3]:
        print(f"    fault: {f['kind']} CFSR={f['cfsr_value']} HFSR={f['hfsr_value']} stacked PC={f['pc']} "
              f"in context {f['context']}")
    if not s.get("faults"):
        print("    fault registers: CFSR=0x00000000 (no fault)")
    bad = [b["step"] for b in s.get("boot_steps", []) if not b["ok"]]
    if bad:
        print(f"    boot steps that failed their check: {', '.join(bad)}")
    k = s.get("kernel")
    if k:
        print("    tasks: " + ", ".join(f"{n}={t['state']}" for n, t in k["tasks"].items()))
    interesting = {n: v for n, v in s.get("globals", {}).items()
                   if n in ("counter", "greeting_len", "corruptions", "svc_errors", "isr_corruptions", "presses")}
    if interesting:
        print("    globals: " + ", ".join(f"{n}={v}" for n, v in interesting.items()))
    t = s.get("tick")
    if t:
        print(f"    10 tick periods: {int(t['period_cycles'] * 10)} cycles, {int(t['period_us'] * 10)} us "
              f"(target {t['target_us'] * 10} us)")
    print()
    print("  Which root cause fits?")
    for i, text in enumerate(choices, start=1):
        print(_wrap(f"{i}) {text}", indent="    "))
    print()
    if args.answer is None:
        print(f"  Answer with: r2s quiz --seed {seed} --answer <1-4>")
        print(f"  Hints: r2s run {sc['id']}{' --variant ' + variant['name'] if variant else ''} --hidden --trace")
        return 0
    if args.answer == correct:
        print(f"  Correct: {correct}) is the bug.")
    else:
        print(f"  Not quite: you chose {args.answer}, the bug is {correct}).")
    print()
    print(f"  It was {sc['id']}{' variant ' + variant['name'] if variant else ''}: {_ascii(sc['title'])}")
    print("  What to look for")
    print(_wrap(sc["what_to_look_for"]))
    print("  How to debug it on the board")
    _bullets(sc["how_to_debug"], numbered=True)
    return 0 if args.answer == correct else 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="r2s",
        description="reset-to-scheduler-sim: a behavioral model for learning and demonstration of boot, "
                    "exceptions and a tiny preemptive kernel on a Pico 2 (RP2350, Cortex-M33) style MCU.",
        epilog="Start with: r2s list, r2s explain P01, r2s run P01 --trace")
    p.add_argument("--scenarios", default=str(SCENARIO_DIR), help="scenario directory")
    p.add_argument("--color", action="store_true", help="ANSI colours in the console output")
    p.add_argument("--version", action="version", version=f"r2s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    s = sub.add_parser("list", help="list scenarios by layer")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("explain", help="print the learning notes of a scenario")
    s.add_argument("scenario")
    s.set_defaults(fn=cmd_explain)

    s = sub.add_parser("run", help="run one scenario, print the summary, write its trace")
    s.add_argument("scenario", help="scenario id (P06) or path to a YAML file")
    s.add_argument("--trace", action="store_true", help="print the event log")
    s.add_argument("--kinds", help="with --trace: only these event kinds, comma separated "
                                   f"({', '.join(KINDS)})")
    s.add_argument("--variant", help="run only this variant")
    s.add_argument("--hidden", action="store_true", help="symptoms only: no title, fault list or explanation")
    s.add_argument("--reveal", action="store_true", help="with --hidden: reveal the root cause")
    s.add_argument("--seed", type=int, help="override the scenario seed (power-on SRAM contents)")
    s.add_argument("--out", default=str(TRACE_DIR), help="trace output directory")
    s.add_argument("--width", type=int, default=160, help="event log line width")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("run-all", help="run every scenario, write traces and the viewer manifest")
    s.add_argument("--out", default=str(TRACE_DIR))
    s.add_argument("--docs", default=str(DOCS_TRACE_DIR), help="docs/traces directory for the viewer ('' to skip)")
    s.set_defaults(fn=cmd_run_all)

    s = sub.add_parser("disasm", help="annotated listing, vector table and memory map of a program or scenario")
    s.add_argument("target", help=f"program ({', '.join(BUILDERS)}) or scenario id")
    s.add_argument("--variant")
    s.add_argument("--profile", default="arm_like", help="cost profile for the cycle/size columns")
    s.set_defaults(fn=cmd_disasm)

    for name, fn, text in (("regs", cmd_regs, "register dump (decoded xPSR, CONTROL, EXC_RETURN)"),
                           ("stack", cmd_stack, "task stacks: saved frames, labels, watermarks"),
                           ("fault", cmd_fault, "decode the fault records (CFSR/HFSR/MMFAR/BFAR, stacked frame)")):
        s = sub.add_parser(name, help=text)
        s.add_argument("scenario")
        s.add_argument("--variant")
        s.add_argument("--seed", type=int)
        if name != "fault":
            s.add_argument("--at-cycle", type=int, dest="at_cycle", help="stop the run at this cycle first")
        s.set_defaults(fn=fn)

    s = sub.add_parser("quiz", help="a hidden scenario: guess the root cause from the symptoms")
    s.add_argument("--seed", type=int, help="pick the quiz deterministically (printed if omitted)")
    s.add_argument("--answer", type=int, help="your guess (1-4)")
    s.set_defaults(fn=cmd_quiz)

    s = sub.add_parser("bench", help="print the P10 / P11 comparison tables")
    s.set_defaults(fn=cmd_bench)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # output piped into 'head' or similar: stop quietly
        import os
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
