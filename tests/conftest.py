from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def demo_snapshot() -> dict:
    return json.loads((FIXTURES / "demo_snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture
def passing_judgment() -> dict:
    return json.loads((FIXTURES / "passing_judgment.json").read_text(encoding="utf-8"))
