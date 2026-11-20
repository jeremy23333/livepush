"""日志保留：只删过期文件、永不删活动文件、支持预览。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from livepush.util.logretention import (
    area_of,
    cleanup_logs,
    list_log_files,
    next_run_at,
    truncate_service_log,
)


def _touch(path: Path, age_days: float, size: int = 128) -> Path:
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_cleanup_deletes_only_expired_rotated_files(tmp_path: Path):
    _touch(tmp_path / "yogurt-7.log", 30)
    _touch(tmp_path / "lagrange_milky-7.log.2", 40)
    _touch(tmp_path / "sign.log", 1)
    _touch(tmp_path / "app.log", 60)  # 活动文件
    _touch(tmp_path / "app.log.3", 60)  # 轮转备份，可删
    _touch(tmp_path / "service.log", 60)  # 活动文件
    _touch(tmp_path / "notes.txt", 60)  # 非日志

    report = cleanup_logs(14, logs_dir=tmp_path)

    assert report["enabled"] is True
    assert sorted(report["deleted"]) == ["app.log.3", "lagrange_milky-7.log.2", "yogurt-7.log"]
    assert (tmp_path / "app.log").exists()
    assert (tmp_path / "service.log").exists()
    assert (tmp_path / "sign.log").exists()
    assert (tmp_path / "notes.txt").exists()
    assert report["freed_bytes"] > 0


def test_cleanup_dry_run_keeps_everything_with_zero_days(tmp_path: Path):
    _touch(tmp_path / "yogurt-7.log", 30)

    dry = cleanup_logs(14, logs_dir=tmp_path, dry_run=True)
    assert dry["deleted"] == ["yogurt-7.log"]
    assert (tmp_path / "yogurt-7.log").exists()

    off = cleanup_logs(0, logs_dir=tmp_path)
    assert off["enabled"] is False
    assert off["deleted"] == []
    assert (tmp_path / "yogurt-7.log").exists()


def test_cleanup_missing_dir_is_not_an_error(tmp_path: Path):
    report = cleanup_logs(14, logs_dir=tmp_path / "nope")
    assert report["deleted"] == []
    assert report["errors"] == []


def test_list_files_reports_area_and_protection(tmp_path: Path):
    _touch(tmp_path / "app.log", 1)
    _touch(tmp_path / "sign.log.1", 2)
    rows = {row["name"]: row for row in list_log_files(tmp_path)}
    assert rows["app.log"]["protected"] is True
    assert rows["app.log"]["area"] == "应用主日志"
    assert rows["sign.log.1"]["protected"] is False
    assert rows["sign.log.1"]["area"] == "签名服务"


def test_area_of_classification():
    assert area_of("app.log") == "应用主日志"
    assert area_of("app.log.5") == "应用主日志"
    assert area_of("service.log") == "服务进程"
    assert area_of("yogurt-123.log") == "内置协议端"
    assert area_of("whatever.log") == "其他"


def test_truncate_service_log(tmp_path: Path):
    path = tmp_path / "service.log"
    path.write_bytes(b"y" * (3 * 1024 * 1024))

    assert truncate_service_log(path, 3) == 0  # 正好 3MB 不超限
    assert truncate_service_log(path, 0) == 0  # 关闭
    removed = truncate_service_log(path, 1)
    assert removed == 3 * 1024 * 1024
    assert path.stat().st_size == 0
    assert truncate_service_log(tmp_path / "absent.log", 1) == 0


def test_next_run_at_rolls_to_tomorrow():
    from datetime import datetime

    now = datetime(2026, 5, 1, 10, 30, 0).timestamp()
    later_same_day = next_run_at(now, 14)
    assert datetime.fromtimestamp(later_same_day).hour == 14
    assert datetime.fromtimestamp(later_same_day).day == 1

    tomorrow = next_run_at(now, 4)
    target = datetime.fromtimestamp(tomorrow)
    assert target.day == 2
    assert target.hour == 4
