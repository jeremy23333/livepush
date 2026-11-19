"""产物下载：断点续传 + sha256 校验 + 进度回调。

镜像 tar 动辄 600 MB，所以支持中断后续传（服务器返回 200 时自动从头重下），
下载完成先校验大小再校验 sha256，任何一项不符都删掉重来而不是留下坏文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Callable

import httpx

from .base import Artifact

log = logging.getLogger("livepush.hosted.download")

CHUNK = 1 << 20
ProgressFn = Callable[[int, int], None]
LogFn = Callable[[str], None]


class DownloadError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def mirror_url(mirror: str, url: str) -> str:
    if not mirror:
        return url
    return mirror.rstrip("/") + "/" + url


async def _download_once(
    url: str,
    part: Path,
    *,
    progress: ProgressFn | None,
    timeout: float,
) -> None:
    resume_from = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(timeout, read=timeout)
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resume_from and resp.status_code == 200:
                # 服务器不支持 Range：丢掉半截文件从头来。
                resume_from = 0
                mode = "wb"
            elif resume_from and resp.status_code == 206:
                mode = "ab"
            elif resp.status_code == 200:
                mode = "wb"
            else:
                raise DownloadError(f"下载失败：HTTP {resp.status_code}")
            total = resume_from + int(resp.headers.get("content-length") or 0)
            written = resume_from
            if progress:
                progress(written, total)
            with part.open(mode) as fh:
                async for chunk in resp.aiter_bytes(CHUNK):
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written, total)


async def fetch(
    artifact: Artifact,
    dest: Path,
    *,
    mirror: str = "",
    progress: ProgressFn | None = None,
    log_fn: LogFn | None = None,
    retries: int = 3,
    timeout: float = 60.0,
) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    say = log_fn or (lambda msg: log.info("%s", msg))

    if dest.exists():
        if (
            (not artifact.size or dest.stat().st_size == artifact.size)
            and (not artifact.sha256 or sha256_file(dest) == artifact.sha256.lower())
        ):
            say(f"已存在合格产物：{dest.name}")
            return dest
        say(f"已有文件校验不符，重新下载：{dest.name}")
        dest.unlink(missing_ok=True)

    part = dest.with_name(dest.name + ".part")
    url = mirror_url(mirror, artifact.url)
    say(f"开始下载 {artifact.name}（{artifact.size / 1048576:.0f} MB）")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await _download_once(url, part, progress=progress, timeout=timeout)
            last_error = None
            break
        except (httpx.HTTPError, OSError, DownloadError) as exc:
            last_error = exc
            say(f"第 {attempt}/{retries} 次下载失败：{exc}")
            if attempt < retries:
                await asyncio.sleep(min(2 * attempt, 8))
    if last_error is not None:
        raise DownloadError(f"下载 {artifact.name} 失败：{last_error}") from last_error

    size = part.stat().st_size
    if artifact.size and size != artifact.size:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{artifact.name} 大小不符：期望 {artifact.size}，实际 {size}")
    digest = sha256_file(part)
    if artifact.sha256 and digest.lower() != artifact.sha256.lower():
        part.unlink(missing_ok=True)
        raise DownloadError(f"{artifact.name} sha256 校验失败：期望 {artifact.sha256}，实际 {digest}")
    part.replace(dest)
    say(f"下载完成：{dest.name}（sha256 {digest[:12]}…）")
    return dest
