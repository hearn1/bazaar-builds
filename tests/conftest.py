from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def sample_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "research" / "samples"


@pytest.fixture
def load_json():
    def _load(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    return _load

