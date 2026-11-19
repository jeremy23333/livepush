"""产物下载：sha256 校验、断点续传、失败清理。

用本地 http.server 而不是 mock，因为要验证真的 Range 续传与 206/200 分支。
"""

from __future__ import annotations

import hashlib
import http.server
import threading

import pytest

from livepush.qq.hosted.base import Artifact
from livepush.qq.hosted.download import DownloadError, fetch, mirror_url


def serve_bytes(body: bytes, *, range_ok: bool = True):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: D401 - 静音
            pass

        def do_GET(self):  # noqa: N802 - http.server 约定
            rng = self.headers.get("Range")
            if range_ok and rng and rng.startswith("bytes="):
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                part = body[start:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(part)))
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.end_headers()
                self.wfile.write(part)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def artifact_for(body: bytes, url: str, *, sha: str | None = None) -> Artifact:
    return Artifact(
        name="test.tar",
        url=url,
        sha256=sha if sha is not None else hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


@pytest.mark.asyncio
async def test_fetch_downloads_and_verifies(tmp_path):
    body = b"x" * 4096
    server, port = serve_bytes(body)
    try:
        seen: list[tuple[int, int]] = []
        dest = await fetch(
            artifact_for(body, f"http://127.0.0.1:{port}/t.tar"),
            tmp_path / "t.tar",
            progress=lambda done, total: seen.append((done, total)),
            retries=1,
        )
        assert dest.read_bytes() == body
        assert seen and seen[-1][0] == len(body)
        assert not (tmp_path / "t.tar.part").exists()
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_fetch_resumes_from_part(tmp_path):
    body = b"y" * 2048
    part = tmp_path / "t.tar.part"
    part.write_bytes(body[:1000])
    server, port = serve_bytes(body)
    try:
        dest = await fetch(artifact_for(body, f"http://127.0.0.1:{port}/t.tar"), tmp_path / "t.tar", retries=1)
        assert dest.read_bytes() == body
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_fetch_restarts_when_server_ignores_range(tmp_path):
    body = b"z" * 1024
    (tmp_path / "t.tar.part").write_bytes(b"garbage")
    server, port = serve_bytes(body, range_ok=False)
    try:
        dest = await fetch(artifact_for(body, f"http://127.0.0.1:{port}/t.tar"), tmp_path / "t.tar", retries=1)
        assert dest.read_bytes() == body
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_fetch_rejects_bad_sha(tmp_path):
    body = b"data"
    server, port = serve_bytes(body)
    try:
        art = artifact_for(body, f"http://127.0.0.1:{port}/t.tar", sha="0" * 64)
        with pytest.raises(DownloadError) as exc:
            await fetch(art, tmp_path / "t.tar", retries=1)
        assert "sha256" in str(exc.value)
        assert not (tmp_path / "t.tar").exists()
        assert not (tmp_path / "t.tar.part").exists()
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_fetch_rejects_bad_size(tmp_path):
    body = b"data"
    server, port = serve_bytes(body)
    try:
        art = Artifact(name="t.tar", url=f"http://127.0.0.1:{port}/t.tar", sha256="", size=999)
        with pytest.raises(DownloadError) as exc:
            await fetch(art, tmp_path / "t.tar", retries=1)
        assert "大小不符" in str(exc.value)
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_fetch_reuses_existing_valid_file(tmp_path):
    body = b"already here"
    dest = tmp_path / "t.tar"
    dest.write_bytes(body)
    # URL 不可达：能直接返回就说明没有发起请求
    art = artifact_for(body, "http://127.0.0.1:9/never")
    assert await fetch(art, dest, retries=1) == dest


def test_mirror_url_prefixes():
    assert mirror_url("", "https://a/b.tar") == "https://a/b.tar"
    assert mirror_url("https://m/", "https://a/b.tar") == "https://m/https://a/b.tar"
