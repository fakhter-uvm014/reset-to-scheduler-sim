"""Command-line tests: run ``python -m r2s`` exactly as a student would, in a
subprocess with ``sys.executable`` so the same test works on Linux, macOS and
Windows. Every command's output must be plain ASCII.

Author: Farzana Akhter
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def r2s(*args: str, ok: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, "-m", "r2s", *args], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", timeout=120,
                          stdin=subprocess.DEVNULL)  # never inherit the console
    if ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    proc.stdout.encode("ascii")  # raises if anything non-ASCII was printed
    return proc


def test_cli_list_and_version():
    """Test ID: C01
    Purpose: 'list' shows every scenario grouped by layer; '--version' works.
    Stimulus: r2s list; r2s --version.
    Expected result: all scenario ids, the five layer headings and Experiments appear.
    """
    out = r2s("list").stdout
    ids = [p.stem.split("_")[0] for p in (REPO / "scenarios").glob("P*.yaml")]
    assert ids and all(i in out for i in ids)
    for heading in ("Layer 1 ISA", "Layer 2 Boot", "Layer 3 Drivers", "Layer 4 Exceptions", "Layer 5 Kernel",
                    "Experiments"):
        assert heading in out
    assert "Layer 6" not in out
    assert r2s("--version").stdout.startswith("r2s ")


def test_cli_explain():
    """Test ID: C02
    Purpose: 'explain' prints every learning section of a scenario.
    Stimulus: r2s explain P05.
    Expected result: description, expected outcome, what to look for, causes, debug steps, meaning.
    """
    out = r2s("explain", "P05").stdout
    for heading in ("Description", "Expected outcome", "What to look for", "Likely root causes",
                    "How to debug", "What this means"):
        assert heading in out


def test_cli_run_trace_and_summary(tmp_path):
    """Test ID: C03
    Purpose: 'run --trace --kinds' prints a filtered event log, the per-unit summary, the verdict and
             the explanation, and writes the trace JSON.
    Stimulus: r2s run P08 --trace --kinds context_switch,marker --out <tmp>.
    Expected result: exit 0, only the requested kinds, Result: PASS, trace file with result block.
    """
    out = r2s("run", "P08", "--trace", "--kinds", "context_switch,marker", "--out", str(tmp_path)).stdout
    assert "context_switch" in out and "taskA: r8 corrupted" in out
    assert " exception_entry " not in out
    assert "Final state per unit" in out and "Result: PASS" in out and "What this means" in out
    data = json.loads((tmp_path / "P08.json").read_text(encoding="utf-8"))
    assert data["result"]["passed"] is True and data["scenario"]["id"] == "P08"
    assert "checks" in data["result"] and "regs" in data["events"][0]


def test_cli_hidden_and_reveal(tmp_path):
    """Test ID: C04
    Purpose: --hidden shows symptoms only; --reveal adds the root cause back.
    Stimulus: r2s run P08 --hidden, then with --reveal.
    Expected result: hidden output has the symptom and no title/fault list; revealed output has both.
    """
    title = "PendSV forgets"
    hidden = r2s("run", "P08", "--hidden", "--out", str(tmp_path)).stdout
    assert title not in hidden and "Injected faults" not in hidden and "occasionally computes garbage" in hidden
    revealed = r2s("run", "P08", "--hidden", "--reveal", "--out", str(tmp_path)).stdout
    assert title in revealed and "Injected faults" in revealed and "pendsv_forgets_register" in revealed


def test_cli_seed_and_variant(tmp_path):
    """Test ID: C05
    Purpose: --seed changes power-on garbage; --variant runs one variant and only its checks.
    Stimulus: r2s run P02 --seed 7; r2s run P16 --variant wrong_clock.
    Expected result: both exit 0; the variant run reports the 12.5x period.
    """
    r2s("run", "P02", "--seed", "7", "--out", str(tmp_path))
    out = r2s("run", "P16", "--variant", "wrong_clock", "--out", str(tmp_path)).stdout
    assert "variant: wrong_clock" in out and "12500.0 us" in out and "variant: correct" not in out


def test_cli_fault_stack_regs_disasm():
    """Test ID: C06
    Purpose: the inspection commands decode what a debugger would show.
    Stimulus: r2s fault P07; r2s stack P08; r2s regs P06 --at-cycle 3000; r2s disasm blink.
    Expected result: CFSR bits named, short software frame flagged, decoded xPSR/CONTROL, odd vectors.
    """
    out = r2s("fault", "P07").stdout
    assert "MMFSR.DACCVIOL" in out and "MSTKERR" in out and "MMFAR=0x20000270" in out
    out = r2s("stack", "P08").stdout
    assert "software frame has 7 words" in out and "xpsr" in out
    out = r2s("regs", "P06", "--at-cycle", "3000").stdout
    assert "registers at cycle 30" in out and "CONTROL" in out and "IPSR=" in out
    out = r2s("disasm", "blink").stdout
    assert "Vector table" in out and "Reset_Handler" in out and "T=1 ok" in out
    assert "BIT 0 CLEAR" in r2s("disasm", "P14").stdout


def test_cli_quiz_is_deterministic_with_seed():
    """Test ID: C07
    Purpose: the quiz picks a hidden scenario from the seed and grades a numbered answer.
    Stimulus: r2s quiz --seed 2 twice, then --answer 1..4.
    Expected result: identical questions; exactly one answer is accepted (exit 0).
    """
    a, b = r2s("quiz", "--seed", "2").stdout, r2s("quiz", "--seed", "2").stdout
    assert a == b and "Which root cause fits?" in a
    results = [r2s("quiz", "--seed", "2", "--answer", str(k), ok=False).returncode for k in (1, 2, 3, 4)]
    assert sorted(results) == [0, 1, 1, 1]


def test_cli_errors_are_friendly():
    """Test ID: C08
    Purpose: a wrong scenario id gives an error message, not a traceback.
    Stimulus: r2s run P99.
    Expected result: exit code 2 and 'error:' on stderr.
    """
    proc = r2s("run", "P99", ok=False)
    assert proc.returncode == 2 and "error:" in proc.stderr and "Traceback" not in proc.stderr


@pytest.mark.slow
def test_cli_run_all_writes_manifest(tmp_path):
    """Test ID: C09
    Purpose: run-all runs every scenario, all as expected, and writes index.json for the viewer.
    Stimulus: r2s run-all --out <tmp> --docs <tmp>/docs.
    Expected result: exit 0; a manifest entry and a trace per scenario in both directories.
    """
    docs = tmp_path / "docs"
    r2s("run-all", "--out", str(tmp_path), "--docs", str(docs))
    for d in (tmp_path, docs):
        manifest = json.loads((d / "index.json").read_text(encoding="utf-8"))
        assert all(s["passed"] for s in manifest["scenarios"])
        assert all((d / s["file"]).exists() for s in manifest["scenarios"])
