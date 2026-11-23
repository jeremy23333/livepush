# 变更记录

## 2.2.0（2026-11-23）

主题：新增「按直播间手动通知」——从 `/rooms` 直接给该直播间绑定的所有群/私聊群发一条自定义消息。

### 新增

- **`/rooms` 页「通知」按钮**（在「事件」左侧）：弹窗里编辑文案、勾选 @全体 / 带封面，点「发送」即可向该直播间**当前绑定的所有目标**各发一条；「仅保存文案」可以只改不发送。
- **文案持久化**：按直播间保存在 `app_setting` 的 `room_notice`（JSON），不新增表、老库无需迁移；下次打开弹窗自动带出上次的文案与勾选。
- **占位符与开播推送一致**：`{uname}` `{title}` `{prev_title}` `{area}` `{url}` `{duration}` `{duration_sec}` `{live_started}` `{online}` `{room_id}` `{uid}`；默认文案类似开播推送。
- **队列与限速**：每个目标生成一条 `push_job`，相邻目标 `next_attempt_at` 错开 2 秒；派发器串行发送并保留「每目标 1 秒、全局 0.2 秒」的限速，失败沿用 2s/5s/15s 重试与离线挂起。
- **接口**：`GET /api/rooms/{id}/notice`（文案 + 将发往的目标列表 + 间隔）、`POST /api/rooms/{id}/notice`（`text` / `at_all` / `include_cover` / `dry_run`）。

### 变更

- `LiveEvent.kind = "manual"` 走与真实事件相同的渲染/派发链路；`EVENT_LABELS` 增加「手动通知」，`/logs` 与 `/rooms` 的「事件」历史都能看到。
- 渲染层把「可自定义文案」「可带封面」「可 @全体」分别抽成 `OVERRIDE_KINDS` / `COVER_KINDS` / `AT_ALL_KINDS`，手动通知与开播同权。

### 测试

- 新增 `tests/test_notice.py`（入队间隔、目标过滤、文案持久化、渲染占位符、派发器实发）；`tests/test_web.py` 增加通知端点鉴权/CSRF/404/400 与「仅保存」用例。全量 **185 passed**。

## 2.1.2（2026-11-21）

主题：修掉「添加直播间存成 `live.bilibili.com/<UID>`」与「内嵌 WebUI 打不开」，并支持把托管 WebUI 换成 Cloudflare Tunnel 的公网地址。

### 修复

- **添加直播间链接变成 `live.bilibili.com/<UID>`**：前端确认时把 **UID** 发回后端二次解析，一旦那一次没拿到 `room_id`，链接生成 `room_id or short_id or uid` 就会拼出指向主播空间的直播页。现在：
  - 前端发**原始输入**，并带 `expected_uid`；后端解析后 UID 与预览不一致直接 `409`；
  - 后端拿到不到 `room_id` 时再兜底 `/room/v1/Room/getRoomInfoOld?mid=<uid>`；
  - `RoomSnapshot.url` / `ResolvedRoom.url` / `LiveRoom.url` 统一三段回退：房间号 → 短号 → `space.bilibili.com/<uid>`；
  - 启动时 `backfill_room_ids()` 自动补全历史 `room_id == 0` 的行（批量优先、逐个兜底、节流）。
- **在 livepush 内打开 NapCat WebUI 报 `Cannot read properties of null (reading 'addEventListener')`**：托管页复用了 `active="qq"`，`app.js` 在缺少 `#b-mode` 的页面上跑 QQ 页逻辑。现在托管页有独立的 `active="hosted"` 与空 `pages.hosted`，QQ 页脚本也会先检查元素存在。

### 新增

- **托管 WebUI 公网地址**：设置页新增全局 `hosted_webui_public_base`；每条托管连接可在「托管」弹窗里单独设置（存 `app_setting` 的 `hosted_webui_public`，不改表结构）。设置后「带令牌打开」跳到 `https://<你的域名>/webui?token=…`；NapCat 自动补 `/webui`，SnowLuma 用根路径；内嵌页「在新标签打开」也优先用公网地址。接口 `POST /api/hosted/{bot_id}/public-url`。

### 测试

- 新增 `tests/test_room_url.py`（三段回退）、`getRoomInfoOld` 兜底用例、`expected_uid` 409 / 公网地址端点 / 托管页 `data-page="hosted"` 用例；全量 **175 passed**。

## 2.1.1（2026-11-20）

主题：**修掉「添加直播间 502」的真根因**（输入被当成 UID），并补上企微测试告警与 NapCat WebUI 令牌的可操作性。

### 修复

- **添加直播间 502 的根因**：`extract_id()` 只抠数字、不区分形态，`https://live.bilibili.com/22637261` 被当作 **UID 22637261** 查询（命中另一个 UP 主的房间）；号码不是 UID 时又走 `room_init`，而该接口对**合法房间**也返回 `code=60004`，最终被路由映射成 502。现在：
  - 输入先定型（`space.bilibili.com/<n>` → UID、`live.bilibili.com/<n>` 或 `?room_id=` → 房间号、`uid:` / `房间号:` 前缀、裸数字 → 先房间号后 UID），新增 `InputRef` / `parse_input()`。
  - 房间号改用 `/room/v1/Room/get_info`（实测同时接受房间号与短号，`7734200` 与 `6` 指向同一房间），`room_init` 退出主链路。
  - 解析响应新增 `input_kind` / `input_number` / `resolved_as`（room/short/uid）/ `ambiguous`；页面会显示「识别为：直播间号 / 短号 / UID」，裸数字同时命中别的 UID 时给黄色提醒，建议改用 `space.bilibili.com/<数字>`。
  - 解析整体限时 12 秒（`resolve_timeout`），单请求 6 秒、不重试，避免反向代理先返回 502；超时给「解析超时」而不是 502。
- **`b23.tv` / `bili2233.cn` 短链**：自动跟随跳转后再定型，失败提示「打开链接后复制地址栏」。
- **企业微信测试告警按钮**：`send_test()` 之前不计数、不回传结果；现在返回 `configured/errcode/elapsed_ms`，`sent` 计数正常。页面按钮加 `try/catch`、发送中置灰、结果写回「最近一次测试」；地址还没保存时先提示「先点保存设置」。
- **非 JSON 的 5xx 不再只显示「HTTP 502」**：`LP.api()` 先读文本再解析，错误提示带上状态码、`server`、`X-Livepush-Elapsed` 耗时与正文片段，并 `console.error` 完整响应。
- **静态资源缓存**：`app.js` / `app.css` 加 `?v={{ version }}`，升级后不会被 Cloudflare/浏览器旧缓存挡住新按钮；`/static/` 响应带 `Cache-Control: no-cache`。

### 新增

- **NapCat WebUI 令牌自动读取**：`<workdir>/config/webui.json`（容器内 `/app/napcat/config/webui.json` 的挂载）→ `docker exec cat` → 启动日志正则，三层回退，10 秒缓存；托管弹窗显示令牌、「复制」「带令牌打开」，内嵌页 iframe 自动带 `?token=`。
- 托管容器日志默认从 60 行放宽到 **300 行**（NapCat 令牌在启动早期）。
- `GET /api/diag/network`：DNS → TCP 443 → TLS → HTTP 四步网络体检（`/rooms` 页「网络诊断」按钮），用于区分「本机连不上 B 站」与「B 站接口报错」。
- 请求耗时：所有响应带 `X-Livepush-Elapsed`，`/api/*` 访问日志记一行「路径 状态 耗时」。

### 文档

- [`docs/OPERATIONS.md`](OPERATIONS.md) 增「为什么添加直播间会 502」小节与输入形态对照表。
- [`docs/HOSTED.md`](HOSTED.md) 增「WebUI 令牌怎么来的」三层说明。
- `README.md` FAQ 补「添加直播间报错怎么排查」。

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
- **升级脚本 `scripts/upgrade.sh`**：服务器上一条命令完成「检查工作区 → `git fetch` → `ff-only` 合并/切 tag → 装依赖 → `init` → `doctor` → 重启 systemd」，支持 `--tag` 回滚、`--no-restart`、`--force-restart`；`doctor` 有失败项时默认不重启。

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
