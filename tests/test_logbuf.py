"""内存日志缓冲：给首页「实时日志」用的环形缓冲与分区映射。"""

from __future__ import annotations

import logging

from livepush.util import logbuf
from livepush.util.logbuf import LogBuffer, area_of, get_buffer, install


def _record(name: str, level: int, message: str, *args) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


def test_area_mapping_covers_known_namespaces():
    assert area_of("livepush.monitor.engine") == "采集"
    assert area_of("livepush.push.dispatcher") == "推送"
    assert area_of("livepush.login") == "登录"
    assert area_of("livepush.qq.hosted.manager") == "托管"
    assert area_of("livepush.hosted.docker") == "托管"
    assert area_of("livepush.onebot11") == "QQ 传输"
    assert area_of("livepush.milky") == "QQ 传输"
    assert area_of("livepush.signer.server") == "签名"
    assert area_of("livepush.api") == "Web API"
    assert area_of("livepush") == "服务"
    assert area_of("third.party") == "其他"


def test_buffer_add_snapshot_and_seq():
    buffer = LogBuffer(maxlen=10)
    buffer.add(_record("livepush.monitor.engine", logging.INFO, "第 %s 次采集", 1))
    buffer.add(_record("livepush.push.dispatcher", logging.ERROR, "推送失败"))

    items = buffer.snapshot()
    assert [item["message"] for item in items] == ["第 1 次采集", "推送失败"]
    assert items[0]["area"] == "采集"
    assert items[1]["level"] == "ERROR"
    assert buffer.seq == 2


def test_buffer_since_and_level_filter():
    buffer = LogBuffer(maxlen=10)
    buffer.add(_record("livepush.x", logging.INFO, "info-1"))
    buffer.add(_record("livepush.x", logging.ERROR, "error-1"))
    buffer.add(_record("livepush.x", logging.INFO, "info-2"))

    only_errors = buffer.snapshot(level="ERROR")
    assert [item["message"] for item in only_errors] == ["error-1"]

    tail = buffer.since(1)
    assert [item["message"] for item in tail] == ["error-1", "info-2"]

    assert buffer.since(99) == []
    buffer.clear()
    assert buffer.snapshot() == []


def test_buffer_is_bounded():
    buffer = LogBuffer(maxlen=3)
    for i in range(10):
        buffer.add(_record("livepush.x", logging.INFO, "line-%d", i))
    items = buffer.snapshot()
    assert len(items) == 3
    assert [item["message"] for item in items] == ["line-7", "line-8", "line-9"]


def test_install_captures_runtime_logs():
    logbuf.buffer.clear()
    handler = install(level=logging.INFO)
    try:
        logging.getLogger("livepush.unittest").warning("抓到一条 %s", "告警")
        messages = [item["message"] for item in get_buffer().snapshot(level="WARNING")]
        assert "抓到一条 告警" in messages
        # 重复安装不会叠加 handler
        again = install(level=logging.INFO)
        assert again is handler
    finally:
        logging.getLogger().removeHandler(handler)
        logbuf.buffer.clear()
