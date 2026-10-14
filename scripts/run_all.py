"""Run every scenario, write traces/ and docs/traces/ (with the index.json
manifest the viewer reads), and regenerate docs/guide/scenario-catalog.md
from the scenario YAML files so the catalog always matches them.

    python scripts/run_all.py            # Linux / macOS / Windows alike

Exit code 0 only if every scenario matched its expected outcome.

Author: Farzana Akhter
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from r2s import faults as fault_lib  # noqa: E402
from r2s.cli import main  # noqa: E402
from r2s.scenarios import LAYERS, layer_label, list_scenarios, load_scenario  # noqa: E402


def viewer_url() -> str:
    """The Pages URL from pyproject.toml, so set_github_user.py renames stick."""
    import re
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^Viewer\s*=\s*"([^"]+)"', text, re.M)
    return (m.group(1) if m else "https://fakhter-uvm014.github.io/reset-to-scheduler-sim/").rstrip("/") + "/"


def _checks(sc) -> str:
    out = []
    for c in sc["expected"]["checks"]:
        c = dict(c)
        name = c.pop("check")
        variant = c.pop("variant", None)
        args = ", ".join(f"{k}={v}" for k, v in c.items())
        out.append(f"`{name}`" + (f" ({args})" if args else "") + (f" [{variant}]" if variant else ""))
    return "; ".join(out)


def write_catalog(path: Path) -> None:
    scenarios = [load_scenario(p) for p in list_scenarios()]
    url = viewer_url()
    lines = [
        "# Scenario catalog",
        "",
        "Generated from `scenarios/*.yaml` by `scripts/run_all.py`; edit the YAML, not this file.",
        "Each scenario is one experiment from the course, healthy or with one injected bug.",
        "Run one with `r2s run <ID>`, read its notes with `r2s explain <ID>`, or open it in the",
        f"[live viewer]({url}) with `#<ID>` appended to the URL.",
        "",
        "| ID | Layer | Title | Injected faults | Variants |",
        "|----|-------|-------|-----------------|----------|",
    ]
    for sc in scenarios:
        faults = [f["type"] for f in sc.get("faults") or []]
        faults += [f["type"] for v in sc.get("variants") or [] for f in v.get("faults") or []]
        variants = ", ".join(v["name"] for v in sc.get("variants") or []) or "-"
        lines.append(f"| [{sc['id']}](#{sc['id'].lower()}) | {sc['layer']} | {sc['title']} | "
                     f"{', '.join(dict.fromkeys(faults)) or '-'} | {variants} |")
    for layer in LAYERS:
        group = [s for s in scenarios if s["layer"] == layer]
        if not group:
            continue
        lines += ["", f"## {layer_label(layer)}"]
        for sc in group:
            lines += ["", f"### {sc['id']}", "", f"**{sc['title']}** "
                      f"([open in the viewer]({url}#{sc['id']}))",
                      "", " ".join(sc["description"].split()), ""]
            lines.append(f"- **Program:** `{sc['program']}`")
            all_faults = list(sc.get("faults") or []) + \
                [dict(f, variant=v["name"]) for v in sc.get("variants") or [] for f in v.get("faults") or []]
            for f in all_faults:
                where = f" (variant `{f.pop('variant')}`)" if "variant" in f else ""
                lines.append(f"- **Injected:** `{f['type']}`{where}: {fault_lib.FAULT_CATALOG[f['type']]}")
            lines.append(f"- **Expected:** {sc['expected']['summary']}")
            lines.append(f"- **Checks:** {_checks(sc)}")
            lines.append(f"- **Symptom (mystery mode):** {sc['hidden_symptom']}")
            lines += ["", "What to look for: " + " ".join(sc["what_to_look_for"].split())]
            lines += ["", "Likely root causes on real hardware:", ""]
            lines += [f"- {x}" for x in sc["likely_root_causes"]]
            lines += ["", "How to debug:", ""]
            lines += [f"{i}. {x}" for i, x in enumerate(sc["how_to_debug"], start=1)]
            lines += ["", "What this means: " + " ".join(sc["what_this_means"].split())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    start = time.perf_counter()
    code = main(["run-all", "--out", str(ROOT / "traces"), "--docs", str(ROOT / "docs" / "traces")])
    write_catalog(ROOT / "docs" / "guide" / "scenario-catalog.md")
    print(f"catalog written to docs/guide/scenario-catalog.md; total {time.perf_counter() - start:.1f} s")
    sys.exit(code)
