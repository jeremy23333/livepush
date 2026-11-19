"""进程级配置。

只放「基础设施与密钥」：数据目录、监听地址、管理员口令、密钥。
运行期可改的业务参数（轮询间隔、模板、B 站 Cookie 等）在数据库 `app_setting` 里。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVEPUSH_", env_file=".env", extra="ignore")

    # --- 基础设施 ---
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"

    # --- 密钥（留空则首次启动自动生成并持久化）---
    admin_password: str = ""
    secret_key: str = ""

    # --- 托管子进程的默认监听地址 ---
    sign_host: str = "127.0.0.1"
    sign_port: int = 8080
    # 额外传给 sign.py 的参数（换了 QQ 版本、偏移自动探测失败时用 --offset 0x...）
    sign_extra_args: str = ""

    # --- 托管协议端（NapCat / SnowLuma）---
    docker_bin: str = "docker"
    # 产物下载镜像前缀（GitHub 慢时填 https://ghproxy.example.com/ 之类）
    artifact_mirror: str = ""

    @property
    def abs_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.abs_data_dir / "livepush.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def covers_dir(self) -> Path:
        return self.abs_data_dir / "covers"

    @property
    def logs_dir(self) -> Path:
        return self.abs_data_dir / "logs"

    @property
    def protocol_dir(self) -> Path:
        return self.abs_data_dir / "protocol"

    @property
    def sign_dir(self) -> Path:
        return self.abs_data_dir / "sign"

    @property
    def hosted_dir(self) -> Path:
        """托管协议端的工作根目录：镜像/产物缓存在它的 artifacts 子目录。"""
        return self.abs_data_dir / "hosted"

    @property
    def secret_file(self) -> Path:
        return self.abs_data_dir / "secret.key"

    def ensure_dirs(self) -> None:
        for d in (self.abs_data_dir, self.covers_dir, self.logs_dir, self.protocol_dir, self.sign_dir, self.hosted_dir):
            d.mkdir(parents=True, exist_ok=True)


def _load_secret(settings: Settings) -> str:
    """密钥优先取环境变量，其次读文件，最后生成并落盘。"""
    if settings.secret_key:
        return settings.secret_key
    path = settings.secret_file
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:  # 收紧权限（Windows 上会失败，忽略）
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _load_admin_password(settings: Settings) -> tuple[str, bool]:
    """返回 (口令, 是否自动生成)。自动生成时由调用方打印到控制台。"""
    if settings.admin_password:
        return settings.admin_password, False
    path = settings.abs_data_dir / "admin-password.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value, False
    value = secrets.token_urlsafe(12)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value, True


class Runtime:
    """进程级单例，避免各模块反复读环境变量。"""

    settings: Settings
    secret_key: str
    admin_password: str
    password_generated: bool

    @classmethod
    def load(cls) -> "Runtime":
        settings = Settings()
        settings.ensure_dirs()
        cls.settings = settings
        cls.secret_key = _load_secret(settings)
        cls.admin_password, cls.password_generated = _load_admin_password(settings)
        return cls


runtime = Runtime
