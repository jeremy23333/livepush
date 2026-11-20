"""命令行入口：run / doctor / init。

`doctor` 是个人自用场景最有用的一条命令：一次把所有前置条件过一遍。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .config import Runtime, runtime


def _ok(name: str, ok: bool, detail: str = "", hint: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


async def collect_doctor() -> list[dict]:
    checks: list[dict] = []
    settings = runtime.settings

    # 1. 依赖
    missing = []
    for mod in ("fastapi", "uvicorn", "httpx", "sqlalchemy", "greenlet", "aiosqlite", "jinja2", "websockets", "qrcode"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    checks.append(
        _ok("Python 依赖", not missing, "齐全" if not missing else f"缺少：{', '.join(missing)}", "pip install -e .")
    )

    # 2. 数据目录可写
    probe = settings.abs_data_dir / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_ok("数据目录", True, str(settings.abs_data_dir)))
    except OSError as exc:
        checks.append(_ok("数据目录", False, str(exc), "检查目录权限或 LIVEPUSH_DATA_DIR"))

    # 3. 数据库
    try:
        from .db import get_sessionmaker, init_engine, init_schema, session_scope
        from sqlalchemy import select

        from .models import LiveRoom

        await init_engine(settings.db_url)
        await init_schema()
        async with session_scope() as session:
            count = (await session.execute(select(LiveRoom.id))).all()
        checks.append(_ok("SQLite", True, f"{settings.db_path}（{len(count)} 个直播间，WAL 已开）"))
    except Exception as exc:
        checks.append(_ok("SQLite", False, f"{type(exc).__name__}: {exc}", "删掉 data/livepush.db 重建，或检查磁盘"))

    # 4. B 站接口
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(
                "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                params=[("uids[]", "2")],
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36",
                    "Referer": "https://live.bilibili.com/",
                },
            )
        if resp.status_code == 412:
            checks.append(_ok("B 站接口", False, "HTTP 412（IP 被风控）", "配代理或换网络；轮询会熔断，等解封"))
        else:
            payload = resp.json()
            checks.append(
                _ok(
                    "B 站接口",
                    payload.get("code") == 0,
                    f"code={payload.get('code')} message={payload.get('message')}",
                    "code=-352/-412 是风控；非 0 时先确认网络与 UA",
                )
            )
    except Exception as exc:
        checks.append(_ok("B 站接口", False, f"{type(exc).__name__}: {exc}", "检查网络/代理"))

    # 5. 签名服务
    try:
        from .qq.sign_server import sign_server

        if sign_server.installed:
            status = sign_server.status()
            async with httpx.AsyncClient(timeout=5) as client:
                try:
                    health = await client.get(sign_server.base_url + "health")
                    healthy = health.status_code == 200
                    detail = f"{sign_server.base_url} → HTTP {health.status_code}"
                except httpx.HTTPError:
                    healthy = False
                    detail = f"{sign_server.base_url} 不可达（installed={status['installed']} running={status['running']}）"
            checks.append(_ok("签名服务", healthy, detail, "设置页点「启动自建签名」，或跑 scripts/bootstrap.sh"))
        else:
            checks.append(
                _ok(
                    "签名服务",
                    True,
                    "未安装（仅内置 Milky 需要；用 OneBot11 免签路线可忽略）",
                    "需要 Milky 时跑 scripts/bootstrap.sh 装自建签名",
                )
            )
    except Exception as exc:
        checks.append(_ok("签名服务", False, f"{type(exc).__name__}: {exc}"))

    # 6. 机器人连接
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import BotConnection
        from .qq import registry

        async with session_scope() as session:
            bots = (await session.execute(select(BotConnection))).scalars().all()
        if not bots:
            checks.append(_ok("QQ 连接", False, "还没有配置机器人", "去 /qq 页添加并扫码登录"))
        for bot in bots:
            # 自检不主动拉起连接：协议端没起时那样会白等十几秒。
            # 只看当前状态，并给出下一步该干嘛。
            client = registry.manager.peek(bot.id)
            if client is None:
                checks.append(
                    _ok(
                        f"QQ 连接 · {bot.name}",
                        False,
                        f"未在运行（模式={bot.mode} 状态={bot.state}）",
                        "启动服务后在 /qq 页确认；内置模式需要先扫码登录",
                    )
                )
                continue
            if not client.connected:
                checks.append(
                    _ok(
                        f"QQ 连接 · {bot.name}",
                        False,
                        f"连接未就绪（状态={client.state} {client.last_error}）",
                        "检查协议端是否在跑、WS/API 地址与 Token 是否正确",
                    )
                )
                continue
            info = await client.get_login_info()
            checks.append(
                _ok(
                    f"QQ 连接 · {bot.name}",
                    bool(info),
                    f"状态={client.state} 账号={client.self_id or bot.self_id} 昵称={client.nickname or bot.nickname}",
                    "未登录就去 /qq 页扫码；外接模式检查协议端是否在跑",
                )
            )
    except Exception as exc:
        checks.append(_ok("QQ 连接", False, f"{type(exc).__name__}: {exc}"))

    # 7. 绑定完整性
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import PushTarget, Subscription

        async with session_scope() as session:
            subs = (await session.execute(select(Subscription))).scalars().all()
            targets = {t.id: t for t in (await session.execute(select(PushTarget))).scalars().all()}
        orphan = [s.id for s in subs if s.target_id not in targets]
        disabled = [s.id for s in subs if not s.enabled]
        checks.append(
            _ok(
                "绑定关系",
                not orphan,
                f"{len(subs)} 条绑定，{len(disabled)} 条停用，{len(orphan)} 条悬空",
                "悬空绑定删掉重建",
            )
        )
    except Exception as exc:
        checks.append(_ok("绑定关系", False, f"{type(exc).__name__}: {exc}"))

    # 8. Docker（只有托管模式需要）
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import HostedEngine
        from .qq.hosted.docker import Docker

        async with session_scope() as session:
            engines = (await session.execute(select(HostedEngine))).scalars().all()
        version = await Docker(settings.docker_bin).available()
        if version:
            checks.append(_ok("Docker", True, f"服务端 {version}"))
        elif engines:
            checks.append(
                _ok(
                    "Docker",
                    False,
                    f"不可用，但已有 {len(engines)} 个托管容器",
                    "安装 Docker 并确认当前用户能直接用 docker 命令",
                )
            )
        else:
            checks.append(
                _ok("Docker", True, "未安装（只用内置/外接模式可忽略）", "需要托管 NapCat/SnowLuma 时装 Docker")
            )
    except Exception as exc:
        checks.append(_ok("Docker", False, f"{type(exc).__name__}: {exc}"))

    # 9. 托管容器
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import BotConnection, HostedEngine
        from .qq.hosted.docker import Docker

        async with session_scope() as session:
            engines = (await session.execute(select(HostedEngine))).scalars().all()
            bots = {b.id: b.name for b in (await session.execute(select(BotConnection))).scalars().all()}
        if not engines:
            checks.append(_ok("托管容器", True, "没有托管容器"))
        else:
            docker = Docker(settings.docker_bin)
            up = 0
            for engine in engines:
                state = await docker.state(engine.container_name)
                if state == "running":
                    up += 1
                checks.append(
                    _ok(
                        f"托管容器 · {bots.get(engine.bot_connection_id, engine.bot_connection_id)}",
                        state == "running",
                        f"{engine.container_name} 状态={state or '不存在'} 后端={engine.backend}",
                        "在 /qq 页点「托管」查看日志或重新安装",
                    )
                )
            if up and len(engines) > up:
                checks.append(_ok("托管容器汇总", False, f"{len(engines)} 个，{up} 个在运行"))
    except Exception as exc:
        checks.append(_ok("托管容器", False, f"{type(exc).__name__}: {exc}"))

    return checks


def _safe_print(text: str) -> None:
    """Windows 控制台常是 GBK，直接 print emoji 会抛 UnicodeEncodeError。"""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _print_checks(checks: list[dict]) -> int:
    bad = 0
    for item in checks:
        mark = "[ OK ]" if item["ok"] else "[FAIL]"
        _safe_print(f"{mark} {item['name']}：{item['detail']}")
        if not item["ok"]:
            bad += 1
            if item.get("hint"):
                _safe_print(f"        ↳ {item['hint']}")
    _safe_print("")
    _safe_print(f"共 {len(checks)} 项，{len(checks) - bad} 项通过，{bad} 项需要处理")
    return 0 if bad == 0 else 1


async def _doctor() -> int:
    Runtime.load()
    from .util.logging import setup_logging

    setup_logging("WARNING", runtime.settings.logs_dir / "app.log")
    try:
        checks = await collect_doctor()
    finally:
        from .db import dispose_engine

        await dispose_engine()
    return _print_checks(checks)


async def _init() -> int:
    Runtime.load()
    from .db import init_engine, init_schema

    await init_engine(runtime.settings.db_url)
    await init_schema()
    print(f"数据库已就绪：{runtime.settings.db_path}")
    print(f"管理员口令文件：{runtime.settings.abs_data_dir / 'admin-password.txt'}")
    return 0


def _run(host: str | None, port: int | None, reload: bool) -> int:
    import uvicorn

    Runtime.load()
    settings = runtime.settings
    from .util.logretention import truncate_service_log

    # service.log 由 systemd 直接 append，没有轮转；每次启动先截断，避免无限增长
    truncate_service_log(settings.logs_dir / "service.log", 32)
    uvicorn.run(
        "livepush.main:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )
    return 0


async def _hosted(cmd: str, bot_id: int | None = None) -> int:
    Runtime.load()
    from .util.logging import setup_logging

    setup_logging("INFO", runtime.settings.logs_dir / "app.log")
    from .db import dispose_engine, init_engine, init_schema

    await init_engine(runtime.settings.db_url)
    await init_schema()
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import BotConnection, HostedEngine
        from .qq.hosted import hosted_manager

        if cmd == "list":
            async with session_scope() as session:
                rows = (await session.execute(select(HostedEngine))).scalars().all()
                bots = {b.id: b for b in (await session.execute(select(BotConnection))).scalars().all()}
            if not rows:
                _safe_print("还没有托管容器。先在 /qq 页创建一条「托管」模式的连接。")
                return 0
            for row in rows:
                bot = bots.get(row.bot_connection_id)
                name = bot.name if bot else "?"
                _safe_print(
                    f"#{row.bot_connection_id} {name} · {row.backend} · {row.container_name}\n"
                    f"    镜像 {row.image_ref or '-'} · 端口 ws={row.host_ws_port} "
                    f"http={row.host_http_port} webui={row.host_webui_port} · "
                    f"状态 {row.state} {row.last_error or ''}"
                )
            return 0

        if bot_id is None:
            _safe_print("这个子命令需要一个 bot id：python -m livepush hosted list")
            return 2

        if cmd == "install":
            job = hosted_manager.install(bot_id)
            last = ""
            while not job.done:
                if job.message != last:
                    _safe_print(f"… {job.message}")
                    last = job.message
                await asyncio.sleep(0.5)
            for line in job.logs[-20:]:
                _safe_print(f"    {line}")
            _safe_print(job.message)
            return 0 if job.ok else 1
        if cmd == "start":
            info = await hosted_manager.start(bot_id)
            _safe_print(f"已连接：{info}")
            return 0
        if cmd == "stop":
            await hosted_manager.stop(bot_id)
            _safe_print("已停止容器")
            return 0
        if cmd == "logs":
            status = await hosted_manager.status(bot_id)
            _safe_print(status.get("log_tail") or "（没有日志：容器可能没在运行）")
            return 0
        _safe_print(f"未知子命令：{cmd}")
        return 2
    finally:
        await dispose_engine()


async def _logs(action: str = "list", dry_run: bool = False) -> int:
    Runtime.load()
    settings = runtime.settings
    from .util.logretention import cleanup_logs, list_log_files

    if action == "list":
        rows = list_log_files(settings.logs_dir)
        if not rows:
            _safe_print(f"{settings.logs_dir} 下还没有日志文件。")
            return 0
        _safe_print(f"日志目录：{settings.logs_dir}")
        for row in rows:
            mark = "（活动文件，不删）" if row["protected"] else ""
            _safe_print(
                f"  {row['name']:<34} {row['size'] / 1024:>9.1f} KB  "
                f"{row['age_days']:>6.1f} 天  {row['area']}{mark}"
            )
        return 0

    from .appconfig import load_app_config
    from .db import dispose_engine, init_engine, init_schema, session_scope

    await init_engine(settings.db_url)
    await init_schema()
    try:
        async with session_scope() as session:
            config = await load_app_config(session)
        if config.log_retention_days <= 0:
            _safe_print("已关闭自动清理（设置里的「日志保留天数」为 0）。")
            return 0
        report = cleanup_logs(
            config.log_retention_days, logs_dir=settings.logs_dir, dry_run=dry_run
        )
        for name in report["deleted"]:
            _safe_print(f"  {'将删除' if dry_run else '已删除'} {name}")
        for name in report["errors"]:
            _safe_print(f"  失败 {name}")
        _safe_print(
            f"{'预演' if dry_run else '完成'}：删除 {len(report['deleted'])} 个，"
            f"释放 {report['freed_bytes'] / 1024:.1f} KB，保留 {report['kept']} 个"
            f"（保留天数 {report['days']}）"
        )
        return 0
    finally:
        await dispose_engine()


async def _bili(query: str) -> int:
    Runtime.load()
    from .bili.client import BiliClient

    client = BiliClient(covers_dir=runtime.settings.covers_dir)
    try:
        result = await client.diagnose(query)
    finally:
        await client.aclose()
    _safe_print(f"输入：{query}")
    for step in result["steps"]:
        flag = "OK  " if step["ok"] else "FAIL"
        detail = step.get("detail") or ""
        _safe_print(f"  [{flag}] {step['name']:<16} {step['ms']:>6} ms  {detail}")
    _safe_print("结论：" + result["summary"])
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="livepush", description="B 站直播监控 + QQ 推送")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="启动 Web 服务与后台任务")
    run_p.add_argument("--host", default=None)
    run_p.add_argument("--port", type=int, default=None)
    run_p.add_argument("--reload", action="store_true")

    sub.add_parser("doctor", help="自检环境与配置")
    sub.add_parser("init", help="初始化数据库与口令文件")
    sub.add_parser("checks", help="以 JSON 输出自检结果")

    hosted_p = sub.add_parser("hosted", help="托管协议端（NapCat / SnowLuma 容器）")
    hosted_p.add_argument("action", choices=["list", "install", "start", "stop", "logs"])
    hosted_p.add_argument("bot_id", nargs="?", type=int, default=None)

    logs_p = sub.add_parser("logs", help="查看日志文件 / 按保留天数清理")
    logs_p.add_argument("action", choices=["list", "cleanup"], nargs="?", default="list")
    logs_p.add_argument("--dry-run", action="store_true", help="只显示会删哪些，不真的删")

    bili_p = sub.add_parser("bili", help="诊断一个直播间输入（链接/短号/UID）")
    bili_p.add_argument("query")

    args = parser.parse_args(argv)
    if args.cmd in (None, "run"):
        return _run(getattr(args, "host", None), getattr(args, "port", None), getattr(args, "reload", False))
    if args.cmd == "doctor":
        return asyncio.run(_doctor())
    if args.cmd == "init":
        return asyncio.run(_init())
    if args.cmd == "checks":
        async def _json() -> int:
            Runtime.load()
            from .util.logging import setup_logging

            setup_logging("ERROR", runtime.settings.logs_dir / "app.log")
            print(json.dumps(await collect_doctor(), ensure_ascii=False, indent=2))
            return 0

        return asyncio.run(_json())
    if args.cmd == "hosted":
        return asyncio.run(_hosted(args.action, args.bot_id))
    if args.cmd == "logs":
        return asyncio.run(_logs(args.action, args.dry_run))
    if args.cmd == "bili":
        return asyncio.run(_bili(args.query))
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
