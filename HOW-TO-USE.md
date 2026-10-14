# How to use Reset to Scheduler

A step-by-step guide for first-time users on **Windows** (PowerShell) or **Linux/macOS**.
You need no Pico 2, debug probe or toolchain. A Cortex-M33-style microcontroller is modeled in
Python: boot, exceptions, a tiny preemptive kernel, the MPU and cycle counters. The viewer runs in any
web browser.

By the end you will have:

1. installed and validated the simulator (about 10 minutes);
2. followed a program from power-on reset to a running multitasking kernel;
3. decoded faults and broken context switches the way you would in GDB.

---

## 1. One-time setup

### Windows

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>.
   On the first installer screen, tick **"Add python.exe to PATH"**, then click *Install Now*.
2. Open **PowerShell**: press the Start key, type `powershell`, press Enter.
3. Check the install with `py --version`. You should see `Python 3.10` or newer.

### Linux / macOS

Check `python3 --version`. On Debian or Ubuntu, install `python3-venv` if it is missing.

---

## 2. Get the files and install

Unzip `reset-to-scheduler-sim.zip`, for example to `C:\Users\<you>\projects\reset-to-scheduler-sim`.
Open a terminal in that folder: in File Explorer, click the address bar, type `powershell`, press Enter.

| Step | Windows (PowerShell) | Linux / macOS |
|---|---|---|
| Create a private Python environment | `py -m venv .venv` | `python3 -m venv .venv` |
| Turn it on | `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |
| Install the simulator | `python -m pip install -e ".[dev]"` | `python -m pip install -e ".[dev]"` |

When the environment is on, your prompt starts with `(.venv)`.

> **PowerShell says "running scripts is disabled"?** Run this once, answer `Y`, then activate again:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

`r2s ...` and `python -m r2s ...` are the same command.

---

## 3. Validate it (10-minute checklist)

| # | Command | What you should see |
|---|---|---|
| 1 | `r2s list` | 18 scenarios (P01 to P18), grouped by layer: ISA, Boot, Drivers, Exceptions, Kernel, Experiments |
| 2 | `r2s run P01` | A clean boot: vector table -> Reset_Handler -> .data copy -> .bss zero -> main, UART `boot ok`, and `Result: PASS` |
| 3 | `r2s run P06` | Three tasks switching on the SysTick tick; about 56 cycles per context switch; `Result: PASS` |
| 4 | `r2s run-all` | All 18 PASS, ending `18/18 scenarios as expected` |
| 5 | `python -m pytest -q` | Ends with `... passed` and no failures |
| 6 | `python scripts/view.py` | Your browser opens the viewer at `http://localhost:8000/` |

If all six look like this, the project works on your machine.

---

## 4. Use the viewer

```
python scripts/view.py
```

This starts a small web server **on your own computer only** and opens your browser. Keep the terminal open;
press `Ctrl+C` there to stop it. (Double-clicking `docs/index.html` does not work, because browsers block
a page opened from disk from loading the trace files.)

| Panel | What it shows / what to do |
|---|---|
| **Scenario + Mystery mode** | Pick a scenario. Mystery mode hides the injected fault until you click *Reveal root cause*. |
| **Timeline: who owns the CPU** | One lane per context: Reset_Handler, main, Task A/B/C, and the SVCall, SysTick and PendSV handlers. Context switches, faults and markers are ticks on the top strip. Zoom with `+`/`-`/*Fit*. |
| **Step controls** | *Next*/*Prev* (or the arrow keys) walk event by event; *Step by* jumps to the next context switch, fault and so on. *Set cursor A* measures the cycles between two events. |
| **Counters** | CYCCNT (core cycles), time at 150 MHz, cycles since the previous event |
| **Registers** | r0-r12, MSP/PSP, LR (with EXC_RETURN decoded), PC, xPSR, CONTROL, privilege, at the selected event |
| **Stack and memory** | Task stacks with the hardware exception frame and the software-saved r4-r11 labelled, plus the watermark |
| **Exception and fault decoder** | CFSR/HFSR bits in plain English, the faulting address, the stacked PC |
| **Measurements** | Interrupt latency, context-switch cost, flash vs SRAM, ARM-like vs RISC-V-like CPI |
| **Summary / Learning notes** | PASS/FAIL checks, what to look for, likely causes, how to debug on real hardware |

---

## 5. Guided tour: from reset to scheduler, one layer at a time

| Layer | Run | What to notice |
|---|---|---|
| **Boot** | `r2s disasm P01` then `r2s run P01 --trace` | The vector table (initial MSP, the reset vector with its Thumb bit), then each startup step before `main` |
| Boot faults | `r2s run P02`, `P03`, `P13`, `P14` | Forgotten `.bss` zeroing, a bad initial stack pointer, `.data` never copied, the Thumb bit missing on reset. Each is a classic bring-up bug. |
| **Drivers / timer** | `r2s run P16` | SysTick reload off by one, and a wrong clock assumption: small errors that add up |
| **Exceptions** | `r2s run P04`, `r2s run P18` | Interrupt latency in cycles, tail-chaining, and a button interrupt that is never enabled or has no handler |
| Priorities | `r2s run P05`, `r2s run P15` | Priority problems, and why PendSV must be the lowest priority |
| **Kernel** | `r2s run P06`, `r2s regs P06`, `r2s stack P06` | A preemptive round-robin kernel: what PendSV saves, where each task's frame sits, EXC_RETURN `0xFFFFFFFD` |
| Kernel bugs | `r2s run P08`, `r2s stack P08` | PendSV forgets r8: no fault, just silent data corruption |
| System calls | `r2s run P12`, `r2s run P17` | The SVC path, privilege, and a system call that returns the wrong value |
| Protection | `r2s run P09` then `r2s fault P07` | A stack overflow without the MPU (silent corruption) vs. with an MPU guard (a precise MemManage fault, with the address in MMFAR) |
| **Experiments** | `r2s bench` | Flash vs SRAM with and without the XIP cache (P10), and ARM-like vs RISC-V-like cost profiles (P11) |

Useful extras:

- `r2s explain <ID>` prints the learning notes.
- `r2s run <ID> --trace --kinds context_switch,fault` filters the event log.
- `docs/guide/`: start with `how-it-works.md`, then `debug-guide.md`.

---

## 6. Practise debugging (mystery mode)

| Command | What it does |
|---|---|
| `r2s run P14 --hidden` | Symptoms only, as a board would show them: UART output, the final state, fault registers |
| `r2s run P14 --hidden --reveal` | Then the root cause and explanation |
| `r2s fault <ID>` | Decode the fault records (CFSR/HFSR/MMFAR/BFAR bits, stacked frame) |
| `r2s regs <ID> [--at-cycle N]` | Register dump at a point in time |
| `r2s stack <ID>` | Task stacks: saved frames, labels, watermarks |
| `r2s quiz` | A random hidden scenario with numbered choices; it prints its seed |
| `r2s quiz --seed 2 --answer 1` | Answer it, then get the explanation and the GDB steps to confirm |

The method to practise, the same one used on a real board with GDB:

1. Read the symptom.
2. Decode the fault registers.
3. Find the stacked PC.
4. Look at the stack.
5. Form a hypothesis.
6. Confirm it.

---

## 7. Command cheat sheet

| Command | Purpose |
|---|---|
| `r2s list` | Scenarios by layer |
| `r2s explain <ID>` | Learning notes |
| `r2s run <ID> [--trace] [--hidden] [--reveal] [--variant V]` | Run a scenario: summary + PASS/FAIL + "What this means" |
| `r2s disasm <ID>` | Annotated listing, vector table and memory map |
| `r2s regs <ID> [--at-cycle N]` | Register dump (xPSR, CONTROL, EXC_RETURN decoded) |
| `r2s stack <ID>` | Task stacks and watermarks |
| `r2s fault <ID>` | Fault decoder |
| `r2s bench` | Flash vs SRAM and ARM-like vs RISC-V-like tables |
| `r2s quiz` | Hidden-cause quiz |
| `r2s run-all` | Run everything (refreshes the viewer's traces) |
| `python scripts/view.py` | Open the viewer |

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `py`/`python` not recognised | Reinstall Python with "Add python.exe to PATH" ticked; open a **new** PowerShell window. |
| `r2s` not recognised | The environment is not active. Activate it, or use `python -m r2s ...`. |
| "running scripts is disabled on this system" | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again. |
| `No module named yaml` | Activate `.venv` and install again: `python -m pip install -e ".[dev]"`. |
| The viewer cannot load traces | Start it with `python scripts/view.py`, not by double-clicking the HTML file. |
| Port 8000 in use | `python scripts/view.py --port 8080` |

To come back later: open PowerShell in the folder, run `.\.venv\Scripts\Activate.ps1`
(Linux: `source .venv/bin/activate`) and continue.

---

Author: Farzana Akhter
