"""本地假签名服务，用于在没装真签名服务时验证「签名体检」。

用法：
    python tools/fake_sign.py --port 8099
然后在设置页把签名服务地址填 http://127.0.0.1:8099/ 点「签名体检」，
应看到「可用」以及平台/版本信息。

加 --mismatch 可以模拟版本不匹配（体检应报 mismatch）。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "3.2.27-47354"
MISMATCH_VERSION = "3.2.19-39038"


class Handler(BaseHTTPRequestHandler):
    mismatch = False

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        version = MISMATCH_VERSION if self.mismatch else VERSION
        if self.path.startswith("/health"):
            self._json({"status": "ok", "uptime_seconds": 123, "platform": "Linux", "version": version})
        elif self.path.startswith("/api/sign/appinfo") or self.path.startswith("/appinfo"):
            self._json({"Os": "Linux", "CurrentVersion": version, "Kernel": "nt", "AppClientVersion": version})
        elif self.path.startswith("/stats"):
            self._json({"uptime_seconds": 123, "call_count": 7, "avg_native_ms": 1.2})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(body)
        except ValueError:
            request = {}
        self._json(
            {
                "platform": "Linux",
                "version": VERSION,
                "value": {
                    "sign": "FAKE-SIGN",
                    "extra": "FAKE-EXTRA",
                    "token": "FAKE-TOKEN",
                    "cmd": request.get("cmd", ""),
                },
            }
        )

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print("fake-sign:", fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--mismatch", action="store_true", help="模拟协议版本不匹配")
    args = parser.parse_args()
    Handler.mismatch = args.mismatch
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"假签名服务：http://{args.host}:{args.port}/  （mismatch={args.mismatch}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
