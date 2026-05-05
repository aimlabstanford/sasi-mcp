from __future__ import annotations

from pathlib import Path

import pytest

from sasi_mcp.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "corpus.sqlite")
    yield s
    s.close()
