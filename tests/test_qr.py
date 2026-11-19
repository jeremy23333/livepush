"""二维码提取。"""

from __future__ import annotations

from livepush.qq.qr import detect_qr, extract_block_qr, find_qr_url, strip_ansi


def test_strip_ansi():
    assert strip_ansi("\x1b[31m红色\x1b[0m") == "红色"


def test_find_qr_url_prefers_hint_lines():
    lines = [
        "starting...",
        "请手动生成二维码：https://example.com/qrcode/abc123",
        "other https://example.com/whatever",
    ]
    assert find_qr_url(lines).endswith("/qrcode/abc123")


def test_find_qr_url_ignores_unrelated_lines():
    assert find_qr_url(["see https://example.com/docs"]) is None


def test_extract_block_qr_requires_minimum_rows():
    block = ["█▀▄" * 6] * 12
    text = extract_block_qr(["boot", *block, "done"])
    assert text is not None
    assert text.count("\n") == 11


def test_extract_block_qr_rejects_tiny_block():
    assert extract_block_qr(["█▀▄", "█▀▄"]) is None


def test_detect_qr_from_file(tmp_path):
    png = tmp_path / "qrcode.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    capture = detect_qr(tmp_path, [])
    assert capture is not None
    assert capture.kind == "file"
    assert capture.data_uri.startswith("data:image/png;base64,")


def test_detect_qr_from_url_renders_svg(tmp_path):
    capture = detect_qr(tmp_path, ["扫码登录：https://example.com/qrcode/xyz"])
    assert capture is not None
    assert capture.kind == "url"
    assert capture.data_uri.startswith("data:image/svg+xml;base64,")


def test_detect_qr_returns_none_when_nothing(tmp_path):
    assert detect_qr(tmp_path, ["nothing here"]) is None
