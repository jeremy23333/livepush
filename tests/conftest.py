"""pytest 公共配置。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "app"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
async def _dispose_db():
    """每个用例结束后释放全局数据库引擎，避免跨用例串库。"""
    yield
    try:
        from livepush.db import dispose_engine

        await dispose_engine()
    except Exception:
        pass


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEPUSH_DATA_DIR", str(tmp_path))
    return tmp_path
