# v2 方案：托管第三方 NTQQ 协议端

> 状态：已实现（2.0.0）。本文件记录批准时的范围与验收口径，便于以后回看「为什么这么设计」。

## 目标

让「QQ 登录」不再卡在签名上：把社区维护的 NapCat / SnowLuma（真实 QQ 客户端 + 进程内 hook）
做成**一键可用的托管后端**，同时保留原有纯协议（Yogurt / Lagrange.Milky + 自建签名）与外接路线。

成功标准：

1. 服务器有 Docker 时，从 Web UI 点几下就能把 NapCat 装起来、扫码登录、把连接绑到推送目标；
2. 服务器没有 Docker 时，`doctor` 明确说明缺什么，并且「外接 + OneBot 11」仍然可用；
3. 签名服务能自动选择偏移、失败能自愈一次，`Code 45` 有可操作的中文提示；
4. 监控 / 推送主链路行为不变，旧数据库可直接升级。

## 范围

做：

- 托管后端框架（下载、docker 封装、端口分配与探测、进程管理、状态持久化）；
- NapCat（Docker 优先）与 SnowLuma（Docker）；
- 签名 `wrapper.node` 自动发现与偏移表、登录态复用修正；
- Web UI / API / CLI / doctor / 测试 / 文档。

不做：

- 不自动安装 Docker；
- 不构建本项目自己的镜像（不写 Dockerfile）；
- 不实现 NTQQ 注入（许可证 + 脆弱性 + 桌面依赖），只用别人的现成产物；
- 不改推送与监控主链路（只加新表与新模块）。

## 里程碑

### M0 备份（先做）

- `pyproject.toml` 版本记为 1.0.0；
- `backups/livepush-1.0.0-<ts>.tar.gz`，只含源码/测试/工具/文档/部署文件；
- `backups/SHA256SUMS` + `backups/RESTORE.md`；
- `data/`（`admin-password.txt`、`secret.key`）不进源码包，单独备份。

### M1 托管框架

- 新表 `hosted_engine`（1:1 关联 `bot_connection`，`SCHEMA_VERSION` 1 → 2，`create_all` 自动建）；
- `qq/hosted/base.py`：`HostedBackend` 抽象、`Artifact`、`HostPorts`、`HostedContext/Status`；
- `qq/hosted/download.py`：续传 + sha256 + 进度 + 重试 + 镜像前缀；
- `qq/hosted/docker.py`：`available/images/load/run/start/stop/rm/state/logs`；
- `qq/hosted/discover.py`：对端口发 OneBot11 `get_login_info` 探测；
- `qq/hosted/manager.py`：`install/attach/start/stop/remove/status/resume_all/stop_all`；
- `main.py`：启动 `resume_all()`、关闭 `stop_all()`（只停不删）。

### M2 NapCat

- 镜像 `napcat-docker-v4.18.42-{amd64,arm64}.tar`（sha256 内置）；
- 挂载 `config/` 与 `cache/`，端口 `ws/http/webui` → 容器 3001/3000/6099；
- 生成 `onebot11_<uin>.json` 与 `onebot11.json`（WS Server + HTTP Server + Token）；
- 二维码：容器 `cache/qrcode.png` 或日志里的链接；
- 无 Docker 后备：官方 QQ deb（`3.2.35-54395`，已验证可用）+ `dpkg-deb -x` + `NapCat.Shell.zip` + `xvfb-run`；
  依赖缺失时给出 `apt install` 清单，不静默失败。

### M3 SnowLuma

- 镜像 `SnowLuma-v2.4.2-docker-{amd64,arm64}.tar`；
- 起容器（`--shm-size=1g`、`SYS_PTRACE`、`seccomp=unconfined`、WebUI 5099、远程桌面 6081）；
- 不生成它的配置；页面给 WebUI/远程桌面直链，并从日志抓初始密码；
- 后台每 5 秒探测，`get_login_info` 成功即绑定。

### M4 签名与登录

- `qq/wrapper.py`：自动发现 `wrapper.node`、读 QQ 版本、`KNOWN_OFFSETS`（含 3.2.35）；
- `sign_server.py`：先用已知偏移，25 秒不健康则自动探测重试，再失败给出 `--offset` 提示；
- `login.py`：keystore → `reusing_session` → 实探 → 成功 `logged_in` / 30 秒回落扫码。

### M5 UI / API / CLI / 测试 / 文档

- `/qq` 三模式 + 托管面板；API 六个端点；CLI `hosted` 子命令；`doctor` 加两项；
- 测试：`test_hosted_specs` / `test_download` / `test_discover` / `test_hosted_manager`，并更新 `test_login_flow`、`test_web`；
- 文档：`QQ-LOGIN.md`、`HOSTED.md`、`CHANGELOG.md`、本文件、`DEPLOY-ubuntu.md` 增补、`README.md`。

## 关键取舍

| 选择 | 原因 |
| --- | --- |
| Docker 镜像 tar 而不是让用户 `docker pull` | 上游仓库已归档/清空，明确版本的 tar 更可控，sha256 可校验 |
| 只支持 Docker 托管，不做原生注入 | 注入要 hook 进程 + 平台相关代码，许可证与维护成本都不可接受 |
| 端口用高位随机段，容器端口固定 | 不跟宿主机已有的 3000/3001/6099 抢 |
| 配置由我们生成而不是让用户在协议端里点 | 减少一次性手工步骤，出问题也能重建 |
| 托管失败不阻塞监控 | 推送目标绑定照旧，`doctor` 只提示 |

## 验收

- `pytest -q` 全绿（当前 103 项）；
- `python -m livepush doctor` 在没 Docker 的机器上给出「Docker 未安装」而不是崩溃；
- 有 Docker 的机器：安装 → 扫码 → 收到测试推送；
- 无 Docker：`mode=external` 的 OneBot11 连接能正常推送。
