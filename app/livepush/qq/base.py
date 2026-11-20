"""QQ 协议端的统一接口。

我们只依赖「协议」不依赖具体实现：
- `onebot11`：OneBot v11 正向 WebSocket（LLBot / waylay / NapCat fork 等）
- `milky`：Milky 规范（Yogurt / Lagrange.Milky / LLBot），HTTP 调 API + WebHook 收事件
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .message import Message


@dataclass(slots=True)
class SendResult:
    ok: bool
    error: str = ""
    retcode: int = 0
    fatal: bool = False  # 目标不可用（群不存在/被踢）→ 应禁用该推送目标
    risk: bool = False  # 账号风控 → 需要长退避
    unknown: bool = False  # 连接不可用/超时 → 保留任务稍后重试

    @classmethod
    def success(cls) -> "SendResult":
        return cls(ok=True)


class BotClient(ABC):
    """一个 QQ 账号连接。"""

    transport: str = ""

    def __init__(self, conn_id: int, name: str) -> None:
        self.conn_id = conn_id
        self.name = name
        self.self_id: int = 0
        self.nickname: str = ""
        self.state: str = "stopped"
        self.last_error: str = ""

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    @property
    def at_all_supported(self) -> bool:
        """是否支持查询「@全体剩余次数」。Milky 规范里没有这个接口。"""
        return self.transport == "onebot11"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(self, kind: str, target_id: str, message: Message) -> SendResult: ...

    @abstractmethod
    async def get_group_list(self) -> list[dict]: ...

    @abstractmethod
    async def get_friend_list(self) -> list[dict]: ...

    @abstractmethod
    async def get_login_info(self) -> dict | None: ...

    async def get_at_all_remain(self, group_id: str) -> dict | None:
        return None

    def health(self) -> dict:
        """运行健康态（掉线探测用）。子类可覆盖补充心跳、事件时间等细节。"""
        return {
            "transport": self.transport,
            "state": self.state,
            "connected": self.connected,
            "self_id": self.self_id,
            "nickname": self.nickname,
            "last_error": self.last_error,
        }
