"""Shared pytest fixtures and helpers.

Author: Farzana Akhter
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from r2s.scenarios import SCENARIO_DIR, assert_sequence, find_scenario, load_scenario, run_scenario

_CACHE: Dict[str, Dict[str, Any]] = {}


def run_cached(ident: str) -> Dict[str, Any]:
    """Run a scenario once per session (they are deterministic)."""
    if ident not in _CACHE:
        _CACHE[ident] = run_scenario(load_scenario(find_scenario(ident, SCENARIO_DIR)))
    return _CACHE[ident]


@pytest.fixture
def scenario_result():
    return run_cached


def kinds(events: List[Dict[str, Any]]) -> List[str]:
    return [e["kind"] for e in events]


def variant(result: Dict[str, Any], name: str) -> Dict[str, Any]:
    return result["summary"]["variants"][name]


__all__ = ["assert_sequence", "run_cached", "kinds", "variant"]
