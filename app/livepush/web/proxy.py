"""把托管协议端的 WebUI 内嵌到 livepush 页面里（反向代理 + URL 重写）。

为什么不能直接反代：NapCat 的 WebUI 前端把 `base` 设为 `/webui/`，但请求
`/api/...`、`/files/...` 是**绝对路径**，和 livepush 自己的 `/api` 冲突，
而且它不认路径前缀。所以这里：
1. HTTP/WS 都从 `/hosted/<id>/webui/*` 转发到 `127.0.0.1:<webui端口>/*`；
2. HTML 里注入 `<base>` + 一小段 shim，把 fetch/XHR/WebSocket/EventSource/history
   发出的绝对路径改写到前缀下；
3. HTML 属性里的 `src="/x"`、`href="/x"` 也重写。

只做鉴权（require_user），不做 CSRF：WebUI 自己会发 POST，没有 `X-LP` 头。
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from websockets.asyncio.client import connect

from ..db import session_scope
from ..models import HostedEngine
from .auth import COOKIE_NAME, validate_session

log = logging.getLogger("livepush.web.proxy")

router = APIRouter(tags=["hosted-ui"])

HOP_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
DROP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
}

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _engine(bot_id: int) -> HostedEngine | None:
    from sqlalchemy import select

    async with session_scope() as session:
        return (
            await session.execute(
                select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
            )
        ).scalar_one_or_none()


async def _base_url(bot_id: int) -> str:
    engine = await _engine(bot_id)
    if engine is None or not engine.host_webui_port:
        raise HTTPException(404, "这个连接还没有托管 WebUI（先安装并启动协议端）")
    return f"http://127.0.0.1:{engine.host_webui_port}"


def _shim(prefix: str) -> str:
    return f"""
<script>
(function () {{
  var PREFIX = {prefix!r};
  function fix(u) {{
    if (typeof u !== "string") return u;
    if (!u.startsWith("/") || u.startsWith("//") || u.startsWith(PREFIX)) return u;
    return PREFIX + u;
  }}
  var nativeFetch = window.fetch;
  if (nativeFetch) {{
    window.fetch = function (input, init) {{
      if (typeof input === "string") return nativeFetch(fix(input), init);
      if (input && input.url) {{
        try {{ return nativeFetch(new Request(fix(input.url), input), init); }} catch (e) {{ return nativeFetch(input, init); }}
      }}
      return nativeFetch(input, init);
    }};
  }}
  var open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {{
    var args = Array.prototype.slice.call(arguments);
    args[1] = fix(url);
    return open.apply(this, args);
  }};
  var NativeWS = window.WebSocket;
  window.WebSocket = function (url, protocols) {{
    try {{
      var u = new URL(url, location.origin);
      var scheme = location.protocol === "https:" ? "wss:" : "ws:";
      var target = fix(u.pathname) + u.search;
      return protocols === undefined ? new NativeWS(scheme + "//" + location.host + target) : new NativeWS(scheme + "//" + location.host + target, protocols);
    }} catch (e) {{ return new NativeWS(url, protocols); }}
  }};
  window.WebSocket.prototype = NativeWS.prototype;
  window.WebSocket.CONNECTING = NativeWS.CONNECTING;
  window.WebSocket.OPEN = NativeWS.OPEN;
  window.WebSocket.CLOSING = NativeWS.CLOSING;
  window.WebSocket.CLOSED = NativeWS.CLOSED;
  var NativeES = window.EventSource;
  if (NativeES) {{
    window.EventSource = function (url, opts) {{ return new NativeES(fix(url), opts); }};
    window.EventSource.prototype = NativeES.prototype;
  }}
  ["pushState", "replaceState"].forEach(function (key) {{
    var original = history[key];
    history[key] = function (state, title, url) {{
      return original.call(this, state, title, url ? fix(url) : url);
    }};
  }});
}})();
</script>
"""


def _inject(html: str, prefix: str) -> str:
    # 先重写原文档里的绝对路径属性，再注入 head，避免把 base/shim 自己的 "/" 也改掉
    html = re.sub(r'(src|href)="/(?!/)', lambda m: f'{m.group(1)}="{prefix}/', html)
    head = f'<base href="{prefix}/">' + _shim(prefix)
    if "</head>" in html:
        html = html.replace("</head>", head + "</head>", 1)
    elif "<head>" in html:
        html = html.replace("<head>", "<head>" + head, 1)
    else:
        html = head + html
    return html


@router.api_route(
    "/hosted/{bot_id}/webui/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_http(bot_id: int, path: str, request: Request) -> Response:
    if not validate_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "未登录")
    base = await _base_url(bot_id)
    prefix = f"/hosted/{bot_id}/webui"
    query = request.url.query
    target = f"{base}/{path}" + (f"?{query}" if query else "")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    headers["accept-encoding"] = "identity"
    body = await request.body()
    try:
        upstream = await _http().request(
            request.method, target, content=body or None, headers=headers
        )
    except httpx.HTTPError as exc:
        log.warning("托管 WebUI 代理失败 bot=%s path=%s：%s", bot_id, path, exc)
        return HTMLResponse(
            f"<h3>连不上托管 WebUI</h3><p>容器可能没有运行。原始错误：{type(exc).__name__}: {exc}</p>",
            status_code=502,
        )

    content = upstream.content
    content_type = upstream.headers.get("content-type", "")
    if "text/html" in content_type:
        content = _inject(content.decode("utf-8", "replace"), prefix).encode("utf-8")

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
    location = out.get("location")
    if location:
        for key in list(out):
            if key.lower() == "location":
                if location.startswith("/") and not location.startswith(prefix):
                    out[key] = prefix + location
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=out,
        media_type=None,
    )


@router.websocket("/hosted/{bot_id}/webui/{path:path}")
async def proxy_ws(websocket: WebSocket, bot_id: int, path: str) -> None:
    if not validate_session(websocket.cookies.get(COOKIE_NAME)):
        await websocket.close(code=4401)
        return
    try:
        base = await _base_url(bot_id)
    except HTTPException:
        await websocket.close(code=4404)
        return
    query = urlencode([(k, v) for k, v in websocket.query_params.multi_items()])
    upstream_url = base.replace("http://", "ws://", 1) + f"/{path}" + (f"?{query}" if query else "")
    await websocket.accept()
    try:
        async with connect(upstream_url, ping_interval=20, max_size=None) as upstream:
            async def client_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("text")
                    if data is None:
                        await upstream.send(message.get("bytes") or b"")
                    else:
                        await upstream.send(data)

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:  # pragma: no cover - 上游断开会走这里
        log.debug("托管 WebUI 的 WS 断开 bot=%s：%s", bot_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
