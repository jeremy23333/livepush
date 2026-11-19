# 变更记录

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
