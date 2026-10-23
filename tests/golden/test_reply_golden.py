"""Snapshot tests for every reply template. Regenerate with: UPDATE_GOLDEN=1 uv run pytest tests/golden"""

import os
from pathlib import Path

import pytest

from tests.golden.cases import CASES

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("part", ["txt", "html"])
def test_golden(name, part):
    reply = CASES[name]()
    got = reply.text if part == "txt" else reply.html
    path = SNAPSHOTS / f"{name}.{part}"
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(got)
    assert path.exists(), f"missing snapshot {path.name}; run with UPDATE_GOLDEN=1"
    assert got == path.read_text()


def test_no_orphan_snapshots():
    names = {p.stem for p in SNAPSHOTS.glob("*.*")}
    assert names <= set(CASES)
