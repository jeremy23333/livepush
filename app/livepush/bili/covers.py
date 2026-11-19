"""封面下载与本地缓存。

为什么要自己下载：QQ 协议端去拉 B 站图可能被防盗链拦掉，
本地缓存后以 base64 发送最稳；同时也避免重复拉同一张图。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def guess_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def to_data_uri(data: bytes) -> str:
    import base64

    return f"data:{guess_mime(data)};base64,{base64.b64encode(data).decode()}"


def to_base64_uri(data: bytes) -> str:
    """供 OneBot / Milky 的 image 段使用。"""
    import base64

    return f"base64://{base64.b64encode(data).decode()}"


class CoverCache:
    def __init__(self, root: Path, ttl: int = 7 * 86400, timeout: float = 15.0, max_bytes: int = 4 * 1024 * 1024):
        self.root = root
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._http = httpx.AsyncClient(
            headers={"User-Agent": DEFAULT_UA, "Referer": "https://live.bilibili.com/"},
            timeout=timeout,
            follow_redirects=True,
        )

    def path_for(self, url: str) -> Path:
        return self.root / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")

    def cached(self, url: str) -> Path | None:
        if not url:
            return None
        path = self.path_for(url)
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            return path
        return None

    async def get(self, url: str) -> Path | None:
        if not url:
            return None
        hit = self.cached(url)
        if hit is not None:
            return hit
        path = self.path_for(url)
        try:
            resp = await self._http.get(url)
        except httpx.HTTPError:
            return path if path.exists() else None
        if resp.status_code != 200 or not resp.content or len(resp.content) > self.max_bytes:
            return path if path.exists() else None
        try:
            path.write_bytes(resp.content)
        except OSError:
            return None
        return path

    async def aclose(self) -> None:
        await self._http.aclose()
