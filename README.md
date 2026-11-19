# livepush

自用的 **B 站直播状态监控 + QQ 推送**，带一个集中管理 Web UI：在网页里添加直播间、扫码登录 QQ、把「直播间 × 群/私聊」绑起来，开播/下播自动推送文字 + 封面图。

单机部署（Ubuntu，无 Docker），数据全在 `data/` 一个目录里。

---

## 1. 它做了什么

- **采集**：每轮用 `get_status_info_by_uids` 批量拉一次状态（免登录，按 100 个 UID 分块），带自适应间隔、抖动、`-412` 熔断、`-352` 降频。
- **判定**：状态机去抖——下播要连续 2 次确认才发通知，采集失败绝不算下播，重启不丢状态也不重复推送。
- **推送**：outbox 模式（先落库再发送，`UNIQUE(event_id,target_id)` 保证幂等），失败按 2s/5s/15s 重试；群不存在自动禁用该目标；机器人离线时任务挂着，恢复后补发。
- **QQ**：支持两条协议（Milky / OneBot 11）和三种模式（内置托管 / Docker 托管第三方 NTQQ / 外接已有协议端），Web UI 里能直接扫码登录。
- **签名**：内置自建签名服务的进程管理（免 Token），并带体检与 `Code 45` 中文诊断；对不上的版本自动探测偏移，也支持手动 `--offset`。

### 为什么不逐房间轮询 / 不用弹幕 WS

| 方案 | 每周期请求数（N 个房间） | 登录 | 时效 | 风控暴露 |
|---|---|---|---|---|
| **批量 `get_status_info_by_uids`（本项目）** | **⌈N/100⌉** | 免登录 | 等于轮询间隔 | 低 |
| 逐房间 `getInfoByRoom`（BililiveRecorder 的做法） | **N** | 免登录 | ≥180s | 中高 |
| 弹幕 WS `getDanmuInfo` | 每个在播房间 1 条长连接 | 需 WBI 签名 + Cookie | 秒级 | 高（常吃 `-352`） |

50 个房间、30 秒周期下，批量方案是 **2880 次/天**，逐房间是 **14.4 万次/天**。推送场景 30 秒延迟完全够用，所以选批量。

---

## 2. 快速开始（Ubuntu）

```bash
git clone <this-repo> ~/livepush && cd ~/livepush
bash scripts/bootstrap.sh              # 建 venv + 装 Python 依赖
cp .env.example .env                   # 可选，按需改
.venv/bin/python -m livepush doctor    # 自检
.venv/bin/python -m livepush run       # 启动
```

打开 `http://127.0.0.1:8787`，管理员口令在 `data/admin-password.txt`（或用 `LIVEPUSH_ADMIN_PASSWORD` 指定）。

常驻（systemd 用户实例）：

```bash
sudo cp deploy/livepush@.service /etc/systemd/system/
sudo systemctl enable --now livepush@$USER
```

> 默认只监听 `127.0.0.1`。要在别的机器访问，改成 `LIVEPUSH_HOST=0.0.0.0` 并自行加反代/防火墙——这个页面能往群里发消息。

逐步骤的部署 / 调试清单（拷哪些文件、venv 怎么建、按什么顺序跑、日志在哪、故障怎么查）见
[`docs/DEPLOY-ubuntu.md`](docs/DEPLOY-ubuntu.md)。

---

## 3. 用起来

1. **`/qq` 新增连接** → 选定方式 → 扫码登录。
2. **`/rooms`** 填入 UID / 房间号 / 短号 / 直播间链接 → 先看预览 → 添加。默认「正在直播也不立刻推送」，勾选后才推送。
3. **`/targets`** 建群/私聊目标，或用「从机器人导入群」一键拉群列表。
4. **`/bindings`** 点亮「直播间 × 目标」矩阵；点单元格配通知类型、@全体、封面图、自定义开播文案。
5. **`/logs`** 看推送结果，失败可手动重试。
6. **`/settings`** 调轮询间隔、去抖次数、文案模板、B 站 Cookie/代理、签名服务。

---

## 4. QQ 登录的四条路线

先看 [`docs/QQ-LOGIN.md`](docs/QQ-LOGIN.md)：里面有 2026-11 生态剧变的背景、四条路线的对比矩阵与决策树。

### A. Docker 托管 NapCat / SnowLuma（**免签，推荐**）

服务器能跑 Docker 时，`/qq` 选「托管第三方 NTQQ」→ 选后端 → 填 QQ 号 → 创建 → 列表里点「托管」→「安装并启动」。
本项目会下载镜像 tar、`docker load`、分配本机端口、写 OneBot11 配置、起容器、探测端口并自动绑定。

- NapCat：网页/日志里扫码；SnowLuma：用它自己的 WebUI + 远程桌面登录。
- 镜像 600–740 MB，首次安装要等下载；镜像 sha256 内置校验。
- 细节与排障见 [`docs/HOSTED.md`](docs/HOSTED.md)。

### B. 外接 + OneBot 11（**免签，最省事**）

自己跑一个 NTQQ 系协议端（LLBot / waylay 等），开 OneBot 11 正向 WS，然后在 `/qq`：
连接方式选「外接已有协议端」→ 协议选 OneBot 11 → 填 `ws://127.0.0.1:3001` 与 Token。

**不需要签名服务器**——签名由真实 QQ 客户端完成。代价是二维码要在协议端自己的界面扫，本项目不托管它的进程。

### C. 内置 Milky + 自建签名（**网页里直接扫码**）

`/qq` 选「内置托管」→ 协议端选 Yogurt 或 Lagrange.Milky → 填 QQ 号 → 创建即启动，网页弹二维码，手机扫码。

这里的关键是**签名**：

```bash
bash scripts/bootstrap.sh --with-sign
```

它会把 `sign.py`、`symbols.c` 和官方 QQ 的 `wrapper.node` 放到 `data/sign/`，编译符号桩，然后可在设置页点「启动自建签名」。

**自建签名不需要 Token。** `Signer.Token` 是给远程/官方签名服务用的访问凭证，协议端以 `Authorization: Bearer <Token>` 调 `POST {BaseUrl}/sign/sec-sign`；本地自建服务没有鉴权，Token 留空即可。

> 官方公共签名 `https://sign.lagrangecore.org/api/` 已不可直接使用（实测各路径均 404），且官方 V2 是 GPG 白名单制（QQ 群内 `#signer register <GitHub用户名> <GPG Key ID>`，Token 用你的公钥加密下发，1 个主号最多绑 3 个子号）。自建就没有这些麻烦。

### D. 外接 + Milky

已有 Lagrange.Milky / Yogurt 在跑，就填它的 HTTP 地址与 `AccessToken`，事件走本项目提供的 `/webhook/milky`。

### 二维码怎么拿到

按优先级尝试三级，任何一个成功就能在网页上扫：

1. 协议端写出的二维码图片（`qrcode.png` 等）；
2. 协议端终端打印的**登录 URL** → 本地用 `qrcode` 库渲染成图；
3. 终端里的块字符二维码 → 原样在网页 `<pre>` 里渲染。

过期会自动重启协议端换一张新码（最多 3 次），不需要像 sealdice 那样删连接重建。

---

## 5. 部署前必须校准的地方（M0）

各协议端的**配置键名与日志标记会随版本变化**。首次在目标机跑通后，请对照实际输出检查 `app/livepush/qq/backends/__init__.py` 里的：

- `qr_markers` / `success_markers` / `expired_markers`：从协议端日志里实际看到的字符串；
- `keystore_globs`：登录态文件名（Yogurt 是 `session-store.json`）；
- `config_name` 与 `_writer`：生成的配置文件名与结构；
- `extra_args`：启动参数（Lagrange.Milky 默认给了 `shell`）。

改这一个文件即可，其余代码不用动。`/qq` 页面的「查看协议端日志」就是为这件事准备的。

---

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 登录报 `Code 45` | 签名不可用或协议版本不匹配。设置页点「签名体检」，确认服务可达、版本一致 |
| B 站接口 `412` | IP 级风控。已自动熔断 30 分钟；配 `bili_proxy` 或降低频率、夜间延长间隔 |
| 收到 `-352` | 单次请求警告。引擎会自动降频；可配 `SESSDATA` Cookie 降低概率 |
| 二维码很快过期 | 正常。页面会自动换新码，最多 3 次 |
| 推送图片裂开 | 把封面方式从 `url` 改成 `base64`（设置页） |
| 手机扫码后 PC 端 QQ 掉线 | NTQQ PC 协议会互相挤占，扫码期间不要同时登 PC QQ |
| 机器人离线时开播了 | 任务会留在 outbox，机器人恢复后补发；超过 6 小时的旧下播事件直接跳过 |
| 想看环境到底哪坏了 | `python -m livepush doctor`，或在设置页点「环境自检」 |

---

## 7. 目录

```
app/livepush/
├─ bili/        批量状态采集、解析、封面缓存
├─ monitor/     状态机、轮询引擎、事件总线
├─ qq/          Message 模型、OneBot11、Milky、子进程托管、扫码编排、签名
│  └─ hosted/   Docker 托管 NapCat / SnowLuma（下载、docker、端口探测、管理器）
├─ push/        渲染、outbox 派发
├─ web/         路由、Jinja2 模板、静态资源
├─ cli.py       run / doctor / init
└─ main.py      FastAPI 装配
scripts/bootstrap.sh        一次性环境准备（含可选签名）
deploy/livepush@.service    systemd 模板
tests/                      pytest
data/                       运行期数据（勿提交）
```

## 8. 开发

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests   # 可选
```

测试覆盖：状态机表驱动、B 站返回解析（`live_time` 为 int/str/0、缺失 uid）、模板与两种协议的段序列化、二维码三级提取、签名诊断、outbox 幂等/重试/禁用目标、Web 鉴权与 CRUD。

## 9. 已知限制

- 只做直播状态，不做动态、不录制、不接弹幕。
- 单实例 + SQLite，不做多租户。
- 内置托管只覆盖 Milky（Yogurt / Lagrange.Milky）；Docker 托管覆盖 NapCat / SnowLuma；OneBot 11 之外的其他协议走外接。
- Docker 托管依赖第三方镜像，本项目不负责更新镜像，也不自动安装 Docker。
- Windows 上可开发（管道捕获），但 PTY 只有 Linux 有，二维码优先靠「文件」与「URL」两级。
- 第三方 QQ 协议本身有账号风险；上游项目也可能随时变动（2026-11 NapCat 仓库被清空就是例子）。所以本项目只依赖 **Milky / OneBot11 这两个协议**，不绑定任何具体协议端实现。
