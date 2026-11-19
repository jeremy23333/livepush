"""签名相关的纯逻辑。"""

from __future__ import annotations

from livepush.qq.signer import diagnose_login_code, normalize_base


def test_normalize_base_adds_scheme_and_trailing_slash():
    assert normalize_base("127.0.0.1:8080") == "http://127.0.0.1:8080/"
    assert normalize_base("https://sign.example.com/api") == "https://sign.example.com/api/"


def test_normalize_base_keeps_existing_slash():
    assert normalize_base("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/"
    assert normalize_base("  https://x.com/a/  ") == "https://x.com/a/"


def test_normalize_base_empty():
    assert normalize_base("") == ""
    assert normalize_base("   ") == ""


def test_diagnose_code_45_mentions_signature():
    text = diagnose_login_code(45)
    assert "签名" in text
    assert "Code 45" in text


def test_diagnose_code_237_mentions_verification():
    assert "验证码" in diagnose_login_code(237)


def test_diagnose_unknown_code():
    assert "Code 999" in diagnose_login_code(999)
