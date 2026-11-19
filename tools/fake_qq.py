"""本地假 OneBot v11 协议端，用于没有 QQ 也能跑通端到端。

用法：
    python tools/fake_qq.py --port 3001

然后在 livepush 里新建「外接 + OneBot 11」连接，WS 地址填 ws://127.0.0.1:3001。
收到的群消息会打印在终端里（带 @全体 与图片段）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from websockets.asyncio.server import serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("fake-qq")

SELF_ID = 10001
NICKNAME = "假机器人"

GROUPS = [
    {"group_id": 111222333, "group_name": "测试群 A", "member_count": 42},
    {"group_id": 444555666, "group_name": "测试群 B", "member_count": 7},
]

FRIENDS = [
    {"user_id": 90001, "nickname": "小明"},
]


def describe_message(segments) -> str:
    parts = []
    for seg in segments or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(data.get("text", ""))
        elif kind == "image":
            value = str(data.get("file", ""))
            parts.append(f"[图片 {len(value)} 字节]")
        elif kind == "at":
            parts.append("@全体成员" if data.get("qq") == "all" else f"@{data.get('qq')}")
        else:
            parts.append(f"[{kind}]")
    return " ".join(parts)


async def handle(ws) -> None:
    log.info("客户端已连接：%s", getattr(ws, "remote_address", "?"))
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            action = payload.get("action")
            params = payload.get("params") or {}
            echo = payload.get("echo")
            data = None

            if action == "get_login_info":
                data = {"user_id": SELF_ID, "nickname": NICKNAME}
            elif action == "get_group_list":
                data = GROUPS
            elif action == "get_friend_list":
                data = FRIENDS
            elif action == "get_group_at_all_remain":
                data = {
                    "can_at_all": True,
                    "remain_at_all_count_for_group": 10,
                    "remain_at_all_count_for_uin": 10,
                }
            elif action in ("send_group_msg", "send_private_msg"):
                target = params.get("group_id") or params.get("user_id")
                log.info("→ %s %s：%s", action, target, describe_message(params.get("message")))
                data = {"message_id": 1}
            elif action == "get_status":
                data = {"online": True, "good": True}
            else:
                log.warning("未实现的 action：%s", action)
                await ws.send(json.dumps({"status": "failed", "retcode": 1404, "data": None, "echo": echo}))
                continue

            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo}))
    except Exception as exc:  # 连接断开
        log.info("连接结束：%s", type(exc).__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3001)
    args = parser.parse_args()
    async with serve(handle, args.host, args.port):
        log.info("假 OneBot11 服务已启动：ws://%s:%s", args.host, args.port)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
