"""定位 QQ 的 wrapper.node 并读取它的版本号。

自建签名服务需要两样东西：QQ 的 `wrapper.node`（参与 sec-sign 计算的 native 模块）
和与它匹配的「偏移」。QQ 每次升级都可能改偏移，所以这里维护已知表，并允许
`LIVEPUSH_WRAPPER_NODE` 环境变量或显式路径覆盖。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from ..config import runtime

log = logging.getLogger("livepush.wrapper")

# 已知版本 → sign.py 需要的偏移（十六进制）。新版本也可由 sign.py 自动探测。
KNOWN_OFFSETS: dict[str, int] = {
    "3.2.18-36497": 0x59660D0,
    "3.2.19-39038": 0x5ADE220,
    "3.2.27-47354": 0x56D81D1,
    "3.2.35-54395": 0x6856284,
}

QQ_DIRS = (
    Path("/opt/QQ"),
    Path("/opt/qq"),
    Path("/usr/lib/QQ"),
    Path("/usr/lib/qq"),
    Path("/usr/share/QQ"),
    Path("/usr/share/qq"),
)


def find_wrapper(explicit: str = "") -> Path | None:
    """按「显式参数 → 环境变量 → data/sign → 常见安装目录」的顺序找 wrapper.node。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("LIVEPUSH_WRAPPER_NODE", "")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(runtime.settings.sign_dir / "wrapper.node")
    for base in QQ_DIRS:
        candidates.append(base / "resources" / "app" / "wrapper.node")
    for path in candidates:
        with contextlib.suppress(OSError):
            if path.is_file():
                return path
    for base in QQ_DIRS:
        if not base.is_dir():
            continue
        with contextlib.suppress(OSError):
            for found in base.rglob("wrapper.node"):
                if found.is_file():
                    return found
    return None


def read_qq_version(wrapper: Path) -> str:
    """wrapper.node 同目录的 package.json 里就是 QQ 版本（如 `3.2.35-54395`）。"""
    package = Path(wrapper).parent / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "")


def known_offset(version: str) -> int | None:
    if not version:
        return None
    for key, offset in KNOWN_OFFSETS.items():
        if version == key:
            return offset
    for key, offset in KNOWN_OFFSETS.items():
        if version.startswith(key.split("-")[0]):
            return offset
    return None


def qq_dir_of(wrapper: Path) -> str:
    """wrapper.node 位于 <QQ>/resources/app/ 下，往上两级就是 QQ 安装目录。"""
    return str(Path(wrapper).parents[1]) if len(Path(wrapper).parents) >= 2 else ""
