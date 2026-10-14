# Contributing

Thanks for helping make `reset-to-scheduler-sim` a better learning tool.
The goal of every change is the same as the goal of the project: help
someone understand what happens between power-on and a running kernel, what
to expect, and how to debug it when it goes wrong.

## Set up

```
python -m venv .venv
# Linux/macOS: source .venv/bin/activate     Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m pytest -q
python scripts/run_all.py
```

## Ground rules

- The simulator package uses the Python standard library plus `pyyaml`; it
  must run identically on Linux, macOS and Windows (Python 3.10+). Use
  `pathlib`, open files with `encoding="utf-8"`, print plain ASCII.
- Determinism: all randomness goes through `random.Random(seed)`. The same
  seed must give byte-identical traces (there is a test for it).
- Every trace event needs a human-readable `reason`.
- The project describes itself as a behavioral model for learning and
  demonstration. Do not describe it as an emulator or claim exact timing.
  When you are not sure how the real hardware behaves, say so in the docs
  and point to the reference manual section to check.

## Adding a scenario

1. Copy a similar file in `scenarios/` to `PNN_short_slug.yaml`. Fill in
   every field: `id`, `title`, `layer`, `description`, `program`, `options`,
   `faults`, `config`, `expected` (summary + checks), `what_to_look_for`,
   `likely_root_causes`, `how_to_debug`, `what_this_means`, `hidden_symptom`.
2. If you need a new bug, add it to `FAULT_CATALOG` in `r2s/faults.py` and
   implement it as a builder option, a boot fault or a machine fault.
3. Add `test_PNN_short_slug` to `tests/test_scenarios.py` with a docstring
   giving Test ID / Purpose / Stimulus / Expected result. The test suite
   checks that every scenario file has its test.
4. Run `python scripts/run_all.py` to regenerate `docs/traces/` and
   `docs/guide/scenario-catalog.md`, open the viewer and step through the
   new trace as a learner would. If the trace does not tell the story by
   itself, improve the event reasons.
5. Add the symptom to `docs/guide/debug-guide.md` if it is new.

## Changing the model

Each module's docstring states the hardware rule it models. If you change a
rule, update that docstring, `docs/guide/how-it-works.md`, and add a unit
test in `tests/test_units.py` that pins the behaviour down.

## Pull requests

- `python -m pytest -q -W error` passes and `python scripts/run_all.py`
  reports every scenario as expected.
- Regenerated `docs/traces/*.json` are committed so the viewer works on
  GitHub Pages without CI.
- Links use the repository URLs (`https://github.com/fakhter-uvm014/reset-to-scheduler-sim`);
  forks can run `python scripts/set_github_user.py NEWNAME`.
