"""数据库引擎与会话管理（SQLite + WAL）。"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, SchemaVersion, now_ts

SCHEMA_VERSION = 2  # v2：新增 hosted_engine（托管协议端）

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _install_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


async def init_engine(url: str) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    _engine = create_async_engine(url, future=True, pool_pre_ping=True)
    _install_pragmas(_engine)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


async def init_schema() -> None:
    assert _engine is not None, "init_engine() first"
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        row = (await conn.execute(select(SchemaVersion.version).order_by(SchemaVersion.version.desc()))).first()
        if not row:
            await conn.execute(SchemaVersion.__table__.insert().values(version=SCHEMA_VERSION, applied_at=now_ts()))
        elif row[0] < SCHEMA_VERSION:
            await conn.execute(SchemaVersion.__table__.insert().values(version=SCHEMA_VERSION, applied_at=now_ts()))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    assert _sessionmaker is not None, "init_engine() first"
    return _sessionmaker


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务边界：正常退出提交，异常回滚。"""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> bool:
    if _engine is None:
        return False
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
