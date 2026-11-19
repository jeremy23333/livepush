"""二维码提取与渲染。

三级优先（对应计划 §3.2）：
1. 协议端把二维码写成图片文件 → 直接读并转 data URI；
2. 协议端在终端打印「可手工生成二维码的 URL」→ 本地用 qrcode 库渲染；
3. 终端里的块字符二维码 → 原样保留给前端用 <pre> 渲染。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
BLOCK_RE = re.compile(r"[█▀▄▌▐░▒▓■□]")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

QR_FILE_CANDIDATES = (
    "qrcode.png",
    "qr.png",
    "qrcode.jpg",
    "cache/qrcode.png",
    "qrcode/qrcode.png",
    "data/qrcode.png",
)


@dataclass(slots=True)
class QrCapture:
    kind: str  # file | url | text
    sha1: str
    data_uri: str = ""
    url: str = ""
    text: str = ""
    source: str = ""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def png_data_uri(data: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(data).decode()


def image_data_uri(data: bytes) -> str:
    """按文件头猜 MIME，避免把 jpg 标成 png。"""
    import base64

    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def render_url_to_data_uri(url: str) -> str:
    """把二维码内容渲染成 SVG data URI（不依赖 Pillow）。失败返回空串。"""
    try:
        import base64
        import io

        import qrcode
        from qrcode.image.svg import SvgPathImage

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
            image_factory=SvgPathImage,
        )
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image().save(buffer)
        return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        return ""


def find_qr_url(lines: list[str]) -> str | None:
    """从输出里挑出「能生成二维码」的登录地址。"""
    for line in reversed(lines):
        clean = strip_ansi(line)
        if not URL_RE.search(clean):
            continue
        hints = ("qrcode", "qr_code", "qr", "scan", "login", "二维码", "扫码")
        if not any(hint.lower() in clean.lower() for hint in hints):
            continue
        for url in URL_RE.findall(clean):
            return url.rstrip(".,;)")
    return None


def extract_block_qr(lines: list[str], max_lines: int = 120) -> str | None:
    """提取终端里的块字符二维码（连续且含块字符的行）。"""
    block: list[str] = []
    best: list[str] = []
    for line in lines[-max_lines:]:
        clean = strip_ansi(line).rstrip("\r\n")
        if BLOCK_RE.search(clean):
            block.append(clean)
            if len(block) > len(best):
                best = list(block)
        elif block:
            block = []
    if len(best) < 8:
        return None
    return "\n".join(best)


def _newest_qr_file(workdir: Path, max_age: float = 900.0) -> Path | None:
    import time

    candidates: list[Path] = []
    for rel in QR_FILE_CANDIDATES:
        path = workdir / rel
        if path.is_file():
            candidates.append(path)
    # 再兜底扫一层目录里的 *qr*.png
    try:
        for path in workdir.rglob("*qr*.png"):
            if path.is_file():
                candidates.append(path)
    except OSError:
        pass
    now = time.time()
    fresh = [p for p in candidates if now - p.stat().st_mtime <= max_age]
    if not fresh:
        return None
    return max(fresh, key=lambda p: p.stat().st_mtime)


def detect_qr(workdir: Path, lines: list[str], prefer: str = "auto") -> QrCapture | None:
    order = ("file", "url", "text") if prefer == "auto" else (prefer, "file", "url", "text")

    if "file" in order:
        path = _newest_qr_file(workdir)
        if path is not None:
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            if data:
                return QrCapture(kind="file", sha1=_sha1(data), data_uri=image_data_uri(data), source=str(path))

    if "url" in order:
        url = find_qr_url(lines)
        if url:
            uri = render_url_to_data_uri(url)
            if uri:
                return QrCapture(kind="url", sha1=_sha1(url.encode()), data_uri=uri, url=url, source="stdout")

    if "text" in order:
        text = extract_block_qr(lines)
        if text:
            return QrCapture(kind="text", sha1=_sha1(text.encode()), text=text, source="stdout")

    return None
