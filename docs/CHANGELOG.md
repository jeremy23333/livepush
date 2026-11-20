# 变更记录

## 2.1.0（2026-11-20）

主题：**可观测性与运维**——实时日志、故障告警、日志自动清理、QQ 掉线探测、绑定级测试推送、内嵌托管 WebUI，外加 502 错误定位与直播时长精度。

### 新增

- **首页实时日志**：`LogBuffer` 环形缓冲（1000 条）+ `GET /api/logs`、`GET /api/logs/stream?cursor=&level=`（SSE）；总览页可**按等级、按功能分区**筛选，支持暂停/清空。
- **企业微信故障告警**（`app/livepush/alerts.py`）：把 `WARNING/ERROR` 以上日志推到群机器人 webhook；同一类错误按「logger + 归一化消息」冷却（默认 10 分钟）、每分钟硬上限（默认 10 条）、`msgtype=markdown`；设置页可「发送测试告警」，接口 `GET /api/alerts/state`、`POST /api/alerts/test`。
- **日志自动清理**（`app/livepush/util/logretention.py`）：设置里配「保留 N 天」（`log_retention_days`，默认 14，0 关闭）、每天几点跑（`log_cleanup_hour`）、`service.log` 启动截断大小（`log_service_truncate_mb`）；只删过期的一层 `*.log*`，**`app.log`/`service.log` 永不删**。设置页有文件清单与「立即清理」，CLI 为 `python -m livepush logs list|cleanup [--dry-run]`。
- **QQ 掉线探测**（`app/livepush/qq/watchdog.py`）：30 秒一轮，综合真实往返（`get_login_info`）、心跳年龄与 `status.good`、下线 notice、托管容器状态与容器日志关键词；连续异常超过宽限期才告警，**只告警不自动重启**。健康状态挂到 `GET /api/bots` 的 `health`。
- **绑定级测试推送**：`POST /api/subscriptions/{id}/test-push`，按真实模板渲染该绑定的开播/下播消息（强制不带 @全体），末尾标注「测试消息」；5 秒限流，不写事件与任务表。绑定弹窗里可直接点。
- **内嵌托管 WebUI**：`/hosted/{bot_id}` 用 iframe 打开 NapCat WebUI，`/hosted/{bot_id}/webui/*` 做 HTTP + WebSocket 反向代理，HTML 注入 `<base>` 与 fetch/XHR/WS/EventSource 重写 shim，复用 livepush 登录态。经 Cloudflare Tunnel 访问时不再需要额外端口。
- **B 站诊断**：`python -m livepush bili <房间号/链接>` 与 `GET /api/diag/bili?query=`，按「解析 → DNS → HTTP 接口 → 批量状态 → 房间详情」逐步给 OK/FAIL 与耗时。
- **模板占位符 `{live_started}`**：下播消息里可写开播时刻。

### 改进

- **添加直播间的 502**：`BiliError` 增加 `stage`；`room_init` 的 `-404/-400` 正确转成 404「没有这个直播间」；`/rooms`、`/rooms/resolve` 失败会写 `livepush.api` 日志（带 traceback），页面显示可读的阶段与业务码；`/api/diag/bili` 可一键定位。
- **B 站客户端**：`trust_env=False`（不再被系统 `HTTP_PROXY` 环境干扰，只用设置里的 `bili_proxy`）；连接/读超时拆开并重试一次；DNS 走 `asyncio.to_thread` 不阻塞事件循环。
- **直播时长**：下播时长改以「第一次观测到下播候选」的时刻为终点（不再多算一个轮询周期）；直播中若 B 站 `live_time` 与本地记录差超过 60 秒会校准；开播时刻不会取未来值。

### 修复

- `alerts.normalize()` 会把十六进制偏移里的 `0` 也归一（`0x6856284` → `#x#`），导致偏移不同被当成同一类错误；改为占位符保护。
- `alerts.configure()` 对纯空白 webhook 地址仍判为启用。
- 反向代理 `_inject()` 把注入的 `<base>` 自己又重写一遍，导致 WebUI 资源路径重复。

### 文档

- 新增 [`docs/OPERATIONS.md`](OPERATIONS.md)：日志清单与功能分区、保留策略、告警配置、掉线探测判据、内嵌 WebUI、502 排查。
- `docs/DEPLOY-ubuntu.md` 增补日志保留、告警、诊断、内嵌 WebUI 与验收项。

### 兼容性

- 配置项都有默认值，旧 `app_config` 缺字段时自动补齐；数据库无结构变化。

## 2.0.0（2026-11-19）

主题：**把第三方 NTQQ 协议端变成一键可用的托管后端**，并修正签名与登录判定的老问题。

### 新增

- **托管后端框架**（`app/livepush/qq/hosted/`）
  - `napcat`：Docker 一键装 NapCat 4.18.42，自动写 OneBot11 配置（WS + HTTP）、映射本机端口、探测并绑定。
  - `snowluma`：Docker 拉起 SnowLuma 2.4.2（WebUI 5099 / 远程桌面 6081），探测其 OneBot 连接后绑定。
  - 产物下载：断点续传、大小 + sha256 校验、镜像前缀（`LIVEPUSH_ARTIFACT_MIRROR`）、失败重试 3 次。
  - 端口自动分配（连续 4 个空闲高位端口）、`docker load/run/start/stop/rm/logs/inspect` 封装、安装进度上报。
- **新表 `hosted_engine`**（`SCHEMA_VERSION` 1 → 2，`create_all` 自动建表，无需手工迁移）。
- **Web UI**：`/qq` 增加「托管第三方 NTQQ」模式与「托管」面板（安装进度、状态、镜像、工作目录、WebUI/远程桌面、OneBot WS + Token、启停删、日志）。
- **API**：`GET /api/hosted/backends`、`POST /api/hosted/install`、`GET /api/hosted/install/{job_id}`、`GET /api/hosted/{bot_id}/status`、`POST /api/hosted/{bot_id}/attach|start|stop|remove`。
- **CLI**：`python -m livepush hosted list|install|start|stop|logs [bot_id]`；`doctor` 增加「Docker」与「托管容器」两项检查。
- **文档**：[`docs/QQ-LOGIN.md`](QQ-LOGIN.md)（四条路线决策树）、[`docs/HOSTED.md`](HOSTED.md)（托管操作与排障）、[`docs/PLAN-v2.md`](PLAN-v2.md)（本版本方案）。
- **工具**：`tools/fake_docker.py`，没装 Docker 时可用它演练托管流程。

### 改进

- **签名服务**：`wrapper.node` 自动发现（`LIVEPUSH_WRAPPER_NODE` / `data/sign/` / 常见安装目录 / 递归搜索）；读取 QQ 版本并优先用 `KNOWN_OFFSETS` 中的偏移（新增 `3.2.35-54395 = 0x6856284`）；带偏移启动 25 秒不健康则去掉 `--offset` 用特征码自动探测重试一次；`status()` 暴露 `qq_version` 与 `known_offset`。
- **登录态复用**：keystore 出现不再直接算「已登录」，改为 `reusing_session` + `get_login_info` 实探，30 秒验不通自动回落扫码。
- **`bootstrap.sh --with-sign`**：不再钉死已下线的 QQ deb 地址，改为运行时向官方接口解析 + `--range 0-0` 探活 + 备选 3.2.35 地址，全部失败时打印三条出路。

### 修复

- `qq/wrapper.py` 的相对导入越界（`ImportError: attempted relative import beyond top-level package`）。
- `qq/login.py` 里用 `any(workdir.glob(...))` 判断 keystore——`Path.glob` 返回生成器恒为真，导致所有连接都误判为「复用登录态」。
- 非群聊目标误带 `@全体`；`trigger_now()` 无法打断轮询等待；OneBot 启动即调用导致的竞态。

### 兼容性

- 配置文件无需改动；新表由 `create_all` 自动创建。
- 旧备份在 `backups/livepush-1.0.0-*.tar.gz`，恢复方式见 `backups/RESTORE.md`。

## 1.0.0（2026-11-19）

首个可用版本：B 站直播状态批量采集、状态机去抖、outbox 推送、OneBot11 / Milky 双传输、内置 Yogurt / Lagrange.Milky 登录编排、自建签名托管、Web 管理后台、systemd 部署。
