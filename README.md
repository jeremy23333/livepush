# livepush

**B 站直播状态监控 + QQ 推送**，带一个集中管理的 Web UI。

在网页里添加直播间、登录 QQ、把「直播间 × 群聊/私聊」绑起来；开播、下播、改标题时自动推送文字 + 封面图。
单机自用：Ubuntu + Python + SQLite，所有数据都在 `data/` 一个目录里，升级走 Git。

当前版本 **2.1.2**。

---

## 1. 它做了什么

- **采集**：每轮用 `get_status_info_by_uids` 批量拉一次状态（免登录，按 100 个 UID 分块），带自适应间隔（在播/全离线/夜间）、随机抖动、`-412` 熔断、`-352` 降频、可选 Cookie/代理。
- **判定**：状态机去抖——开播要连续 N 次、下播要连续 N 次确认（默认 1/2）；采集失败绝不算下播；`live_status==2`（轮播）按离线处理；重启不丢状态、不重复推送；直播时长按「第一次观测到下播候选」的时刻计算，误差小于一个轮询周期。
- **推送**：outbox 模式——先落库再发送，`(event_id, target_id)` 唯一约束保证幂等；失败按 2s/5s/15s 重试；群不存在自动禁用目标；机器人离线时任务挂着，恢复后补发；超过 6 小时的下播事件直接跳过。
- **QQ**：两种传输（OneBot 11 正向 WS / Milky HTTP+WebHook）× 三种模式（内置 Milky 协议端、Docker 托管 NapCat/SnowLuma、外接已有协议端）。
- **签名**：自建签名服务的进程管理（免 Token）、签名体检、`Code 45/237` 中文诊断，已知版本偏移表 + 自动探测 + `--offset` 逃生口。
- **可观测性**：首页实时日志（等级/功能分区筛选，SSE）、企业微信故障告警（冷却 + 每分钟上限）、日志按天自动清理、QQ 掉线探测（真实往返 + 心跳 + notice + 容器日志）。
- **托管 WebUI**：NapCat 的 WebUI 可以在 livepush 内打开（同源反向代理），令牌自动读取；也能配成 Cloudflare Tunnel 的公网地址，一键「带令牌打开」。

### 为什么不逐房间轮询、也不用弹幕 WS

| 方案 | 每周期请求数（N 个房间） | 登录 | 时效 | 风控暴露 |
|---|---|---|---|---|
| **批量 `get_status_info_by_uids`（本项目）** | **⌈N/100⌉** | 免登录 | 等于轮询间隔 | 低 |
| 逐房间 `getInfoByRoom`（BililiveRecorder 的做法） | **N** | 免登录 | 最长 180s | 中高 |
| 弹幕 WS `getDanmuInfo` | 每个在播房间 1 条长连接 | 需 WBI 签名 + Cookie | 秒级 | 高（常吃 `-352`） |

50 个房间、30 秒周期下，批量方案约 2880 次/天，逐房间约 14.4 万次/天。推送场景 30 秒延迟完全够用。

---

## 2. 快速开始（Ubuntu）

需要 Python ≥ 3.10、`python3-venv`、git。托管模式另外需要 Docker（本项目不会替你装）。

```bash
git clone git@github.com:jeremy23333/livepush.git ~/program/livepush
cd ~/program/livepush

bash scripts/bootstrap.sh               # 建 .venv + 装依赖
cp .env.example .env                    # 可选，按需改

.venv/bin/python -m livepush doctor     # 自检：依赖、目录、数据库、B 站、Docker、签名、QQ、绑定
.venv/bin/python -m livepush run        # 前台启动
```

打开 `http://127.0.0.1:8787`，管理员口令在 `data/admin-password.txt`（也可用 `LIVEPUSH_ADMIN_PASSWORD` 指定）。

常驻（systemd 模板实例）：

```bash
sudo cp deploy/livepush@.service /etc/systemd/system/
sudo systemctl enable --now livepush@$USER
```

> 默认只监听 `127.0.0.1`。要公网访问请自行加反向代理/隧道 + 认证——这个页面能往群里发消息。
> systemd 模板默认按 `~/livepush` 写路径；本项目示例克隆到 `~/program/livepush`，记得改 unit 里的四条路径（`sudo sed -i 's#/home/%i/livepush#/home/%i/program/livepush#g' ...`）。

**升级**（Git 拉取 + 装依赖 + init + doctor + 重启）：

```bash
cd ~/program/livepush
sudo bash scripts/upgrade.sh                 # 跟 main
sudo bash scripts/upgrade.sh --tag v2.1.2    # 固定版本 / 回滚
```

逐步骤清单（拷哪些文件、venv 怎么建、日志在哪、故障怎么查）见 [`docs/DEPLOY-ubuntu.md`](docs/DEPLOY-ubuntu.md)。

---

## 3. 页面怎么用

1. **`/qq`**：新增连接 → 选模式/协议 → 扫码或填地址 → 保存。
2. **`/rooms`**：填直播间链接、房间号、短号或 UID → 先看预览（会显示「识别为：直播间号 / 短号 / UID」）→ 添加。默认「正在直播也不立刻推送」，勾选后才推。每行的「通知」按钮可向该直播间绑定的所有群/私聊手动群发一条消息（见下）。
3. **`/targets`**：建群/私聊目标，或「从机器人导入群」一键拉列表。
4. **`/bindings`**：点亮「直播间 × 目标」矩阵；单元格里配通知类型、@全体、封面、自定义文案；已有绑定可发「测试推送」。
5. **`/logs`**：看事件与推送结果，失败可手动重试。
6. **`/settings`**：轮询/去抖/文案模板、B 站 Cookie/代理、告警、日志保留、掉线探测、托管 WebUI 公网地址。

### 输入怎么识别（2.1.2 起）

| 你填 | 识别为 | 行为 |
|---|---|---|
| `https://space.bilibili.com/<n>` | UID | 批量接口查该 UID 的直播间 |
| `https://live.bilibili.com/<n>`、`?room_id=<n>` | 直播间号 | 走 `/room/v1/Room/get_info`（同时接受短号） |
| `https://live.bilibili.com/6` | 短号 | 返回真实房间 7734200 |
| `uid:123` / `房间号:123` | 前缀指定 | 按前缀解释 |
| 纯数字 | 先房间号，再 UID | 两者都命中时页面黄色提醒，建议改用 `space.` 链接 |
| `b23.tv` / `bili2233.cn` 短链 | 跟随跳转后定型 | 失败时提示复制地址栏 |

解析整体限时 12 秒；失败会显示阶段（解析输入/DNS/房间接口/批量状态）与耗时。
链接生成规则：房间号 → 短号 → 主播空间页，**不会**再拼出 `live.bilibili.com/<UID>` 这种不存在的页面。

### 手动通知（2.2.0 起）

`/rooms` 每一行的「通知」按钮（在「事件」左边）：把一条自定义消息发给这个直播间**当前绑定的所有群聊/私聊**，不走「开播/下播」开关，也不等直播状态变化。

- 文案与开播推送共用占位符：`{uname}` `{title}` `{prev_title}` `{area}` `{url}` `{duration}` `{duration_sec}` `{live_started}` `{online}` `{room_id}` `{uid}`；
- 可勾选 **@全体成员**（只对群聊生效，私聊自动忽略）与 **带封面图**；
- 文案按直播间持久化（存在 `app_setting` 的 `room_notice` 里，不需要改表），下次打开弹窗自动带出；「仅保存文案」可以只改不发送；
- 多个目标时按队列发送：每个目标一条任务，相邻目标错开 2 秒，派发器本身也是串行 + 每目标 1 秒间隔，避免同一秒连发触发风控；
- 发送失败沿用 outbox 的重试策略（2s/5s/15s，3 次后失败；机器人离线则挂起等恢复）；可以在 `/logs` 里看到 `manual` 事件与每条任务的预览/错误。

---

## 4. QQ 登录的四条路线

背景（2026-11 生态剧变、对比矩阵、决策树）见 [`docs/QQ-LOGIN.md`](docs/QQ-LOGIN.md)。

### A. Docker 托管 NapCat / SnowLuma（免签，推荐）

`/qq` 选「托管第三方 NTQQ」→ 选后端 → 填 QQ 号 → 创建 → 列表点「托管」→「安装并启动」。
本项目会：下载镜像 tar（内置 sha256 校验）→ `docker load` → 分配本机端口（仅 `127.0.0.1`）→ 写 OneBot11 配置 → 起容器 → 探测 `get_login_info` 成功即自动绑定。

- NapCat：`http://127.0.0.1:<webui>/webui`，二维码在弹窗或容器日志里；WebUI 令牌自动读取（配置文件 → `docker exec` → 启动日志三层回退）。
- SnowLuma：用它自己的 WebUI（5099）与远程桌面（6081）登录，账号由已登录的 QQ 进程注入识别。
- 内嵌打开：`/hosted/<bot_id>`，同源代理，无需再加端口或隧道路由。
- 代码与排障见 [`docs/HOSTED.md`](docs/HOSTED.md)。

### B. 外接 + OneBot 11（免签，最省事）

自己跑 NTQQ 系协议端（LLBot / waylay 等），开 OneBot 11 正向 WS：
`/qq` →「外接已有协议端」→ OneBot 11 → 填 `ws://127.0.0.1:3001` 与 Token。

签名由真实 QQ 客户端完成，本项目不托管它的进程。

### C. 内置 Milky + 自建签名（网页里直接扫码）

`/qq` →「内置托管」→ 选 Yogurt 或 Lagrange.Milky → 填 QQ 号 → 创建即启动，网页弹二维码。

签名准备：

```bash
bash scripts/bootstrap.sh --with-sign
```

它把上游 `sign.py`、`symbols.c` 和官方 QQ 的 `wrapper.node` 放到 `data/sign/`，编译符号桩；之后在设置页点「启动自建签名」。

**自建签名不需要 Token**：`Signer.Token` 是远程/官方签名服务的访问凭证，本地服务没有鉴权，留空即可。
官方公共签名（`sign.lagrangecore.org`）已不可用，且官方 V2 是 GPG 白名单制，自建能绕开这些麻烦。

### D. 外接 + Milky

已有 Lagrange.Milky / Yogurt 在跑：填 HTTP 地址与 `AccessToken`，事件走本项目提供的 `/webhook/milky`。

### 二维码三级捕获

1. 协议端写出的图片（`qrcode.png` 等）；
2. 终端打印的登录 URL → 本地用 `qrcode` 渲染；
3. 终端里的块字符二维码 → 原样 `<pre>` 展示。

过期自动重启换码（最多 3 次）。旧会话存在时先尝试复用登录态（30 秒验证），失败再出码。

---

## 5. 托管 WebUI：令牌与公网地址

- **令牌**：NapCat 的 `webui.json` 在 `<workdir>/config/`（容器内 `/app/napcat/config/`），宿主机可直接读；读不到就 `docker exec`，再不行抓启动日志。弹窗显示来源，可复制、可带令牌打开。
- **内嵌**：`/hosted/<bot_id>` 用 livepush 自己的会话鉴权 + 同源反向代理（HTTP + WebSocket）。
- **公网地址**：设置页 →「托管协议端 WebUI」填全局默认（如 `https://qq.dgnra.net`），或在单条连接的「托管」弹窗里逐条覆盖。之后「带令牌打开」会跳到 `https://qq.dgnra.net/webui?token=…`（NapCat 自动补 `/webui`，SnowLuma 用根路径）。
  只接受 `http(s)://`；末尾斜杠自动去掉；已带 `/webui` 不重复拼。

---

## 6. 运维与诊断

细节见 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。要点：

- **实时日志**：总览页底部，按等级/功能分区筛选（`livepush.monitor` 采集、`livepush.push` 推送、`livepush.qq.*` 连接、`livepush.hosted.*` 托管、`livepush.api` Web 请求……）。
- **日志文件**：`data/logs/app.log`（业务日志，5MB×5 轮转）、`service.log`（systemd 捕获的 stdout/stderr）、`yogurt-<id>.log` / `lagrange_milky-<id>.log` / `sign.log`（子进程）。托管容器日志在 Docker 里（`docker logs livepush-<backend>-<id>`）。
- **日志保留**：设置页配「保留 N 天」（默认 14，0 关闭）、每天几点清理、`service.log` 启动截断大小；`app.log` / `service.log` 永不删。命令：`python -m livepush logs list|cleanup [--dry-run]`。
- **故障告警**：填企业微信群机器人 webhook，按最低等级 + 同类冷却（默认 600s）+ 每分钟上限（默认 10 条）推送；有「发送测试告警」按钮，结果会显示 errcode 与耗时。
- **掉线探测**：WS 假在线（进程在、协议层断）时，靠 `get_login_info` 真实往返、心跳 `good=false`、下线 notice、容器状态与日志关键词综合判断；超过宽限期（默认 120s）才告警，只告警不自动重启。
- **502 排查**：页面详情会区分阶段并带耗时；仍失败时跑 `python -m livepush bili "<房间号或链接>"` 逐层定位，或在页面点「诊断这个输入」「网络诊断」。详见 [`docs/OPERATIONS.md`](docs/OPERATIONS.md#6-502--添加直播间失败怎么查)。

---

## 7. 配置

**只有基础设施与密钥放 `.env`**（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIVEPUSH_DATA_DIR` | `./data` | 数据库、封面、日志、协议端工作目录 |
| `LIVEPUSH_HOST` / `LIVEPUSH_PORT` | `127.0.0.1:8787` | Web 监听 |
| `LIVEPUSH_LOG_LEVEL` | `INFO` | 控制台/文件日志级别 |
| `LIVEPUSH_ADMIN_PASSWORD` | 自动生成 | 留空写 `data/admin-password.txt` |
| `LIVEPUSH_SECRET_KEY` | 自动生成 | 会话签名密钥，写 `data/secret.key` |
| `LIVEPUSH_SIGN_HOST` / `LIVEPUSH_SIGN_PORT` | `127.0.0.1:8080` | 自建签名服务监听 |
| `LIVEPUSH_SIGN_EXTRA_ARGS` | 空 | 追加给 `sign.py`，如 `--offset 0x59660D0` |
| `LIVEPUSH_DOCKER_BIN` | `docker` | Docker 可执行文件（可用假脚本演练） |
| `LIVEPUSH_ARTIFACT_MIRROR` | 空 | 镜像下载的镜像前缀 |
| `LIVEPUSH_WRAPPER_NODE` | 空 | 手动指定 `wrapper.node` 路径 |

> systemd 单元里的 `Environment=` **优先级高于 `.env`**；改监听地址要改 unit。

**业务参数全在 `/settings`**：轮询间隔（在播/空闲/夜间）、抖动、批量大小与批间隔、开播/下播确认次数、标题去抖、最小事件间隔、封面方式、默认 @全体、三个文案模板、B 站 Cookie/代理、日志保留、企业微信告警、QQ 掉线探测、托管 WebUI 公网地址。

数据库是 SQLite（WAL），`data/livepush.db`。新表由 `create_all` 自动建，本项目不做破坏性迁移。

---

## 8. 目录

```
app/livepush/
├─ bili/        批量状态采集、输入定型、封面缓存、诊断
├─ monitor/     状态机、轮询引擎、事件总线
├─ qq/          Message 模型、OneBot11、Milky、子进程托管、扫码编排、签名
│  └─ hosted/   Docker 托管 NapCat / SnowLuma（下载、docker、端口探测、WebUI 令牌、管理器）
├─ push/        渲染、outbox 派发
├─ util/        日志、环形日志缓冲、日志清理
├─ alerts.py    企业微信故障告警
├─ web/         路由、Jinja2 模板、静态资源、内嵌 WebUI 反向代理
├─ cli.py       run / doctor / init / checks / hosted / logs / bili
└─ main.py      FastAPI 装配
scripts/bootstrap.sh        一次性环境准备（含可选签名）
scripts/upgrade.sh          服务器端升级（git 拉取 + 装依赖 + doctor + 重启）
deploy/livepush@.service    systemd 模板
tests/                      pytest
data/                       运行期数据（勿提交）
```

---

## 9. 命令行

```bash
python -m livepush run                  # 前台启动
python -m livepush init                 # 初始化数据库与目录
python -m livepush doctor               # 环境自检（含 Docker / 托管容器 / 签名 / QQ / 绑定）
python -m livepush checks               # JSON 形式的自检
python -m livepush logs list            # 日志文件清单（大小、年龄、分区）
python -m livepush logs cleanup --dry-run
python -m livepush bili "https://live.bilibili.com/22637261"   # 解析链路逐层诊断
python -m livepush hosted list          # 托管连接
python -m livepush hosted install <bot_id>
python -m livepush hosted start|stop|logs <bot_id>
```

---

## 10. 开发与测试

```bash
.venv/bin/python -m pytest -q          # 175 passed
.venv/bin/python -m ruff check app tests
```

测试覆盖：状态机表驱动（含直播时长边界）、B 站返回解析（`live_time` int/str/0、缺失 uid、短号）、输入定性与链接回退、模板与两种协议的段序列化、二维码三级提取、签名诊断与偏移、托管编排（假 Docker）、outbox 幂等/重试/禁用目标、日志缓冲与保留、告警冷却、掉线看门狗、反向代理注入、Web 鉴权/CSRF/CRUD。

---

## 11. 已知限制

- 只做直播状态：不做动态、不录制、不接弹幕。
- 单实例 + SQLite，不做多租户。
- 内置托管只覆盖 Milky（Yogurt / Lagrange.Milky）；Docker 托管覆盖 NapCat / SnowLuma；其它协议走外接。
- Docker 托管依赖第三方镜像，本项目不负责更新镜像，也不自动安装 Docker。
- 第三方 QQ 协议本身有账号风险，上游也可能随时变动（2026-11 NapCat 主仓库被清空就是例子）。所以本项目只依赖 **Milky / OneBot11 两个协议**，不绑定具体协议端实现。
- Windows 可用于开发（管道捕获），但 PTY 只有 Linux 有，二维码优先靠「文件」与「URL」两级。

---

## 12. 参考与致谢

这个项目站在这些已有仓库的肩膀上（**只借鉴思路/协议，源码与二进制都不随本仓库分发**；需要第三方产物时由你的服务器在安装时从上游下载并在本机运行）：

| 仓库 | 参考了什么 |
|---|---|
| [sealdice/sealdice-core](https://github.com/sealdice/sealdice-core) | QQ 协议端作为**受管子进程**的托管方式：解析子进程 stdout 里的二维码标记、登录状态机、进程与工作目录布局、过期换码。 |
| [BililiveRecorder/BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) | 直播间状态轮询与「在播/离线」判定、标题过滤与退避/防重试风暴的思路（对比后本项目改用批量接口）。 |
| [SK-415/HarukaBot](https://github.com/SK-415/HarukaBot) | 订阅模型与推送交互效果（开播/下播/改标题、按群绑定、直播时长），以及默认轮询节奏的量级参考。该项目已停止维护。 |
| [Micuks/ntqq-sign-server](https://github.com/Micuks/ntqq-sign-server) | 自建签名方案：`sign.py` / `symbols.c`，以及 QQ `wrapper.node` 偏移探测思路。`bootstrap.sh --with-sign` 会从上游拉取（AGPL-3.0，请遵守其许可）。 |
| [XingYunStar/NapCatQQ](https://github.com/XingYunStar/NapCatQQ) | NapCat 的归档镜像（Docker tar），托管模式 A 使用；本项目只 `docker load` 并运行，不修改、不打包。 |
| [XingYunStar/snowluma](https://github.com/XingYunStar/snowluma) | SnowLuma 的归档镜像，托管模式 A 使用；其许可为「源码可见、非商业」，本项目仅本机自用。 |
| [LagrangeDev/Lagrange.Core](https://github.com/LagrangeDev/Lagrange.Core) | 内置模式 C 所支持的 Milky 系协议端（Lagrange.Milky）的上游谱系；`HttpSigner` 的签名请求格式（`Authorization: Bearer <Token>`、`POST sign/sec-sign`）参考该实现族。 |
| Yogurt（Milky 协议端发行包） | 内置模式 C 支持的另一个协议端。本项目只生成配置、托管进程，不随仓库分发其二进制。 |
| [LuckyLilliaDev/LuckyLilliaBot](https://github.com/LuckyLilliaDev/LuckyLilliaBot) | 外接模式 B 的推荐协议端之一（OneBot 11）。 |

协议规范：Milky（HTTP API + WebHook/SSE 事件，`text` / `image` / `mention_all` 消息段）与 [OneBot 11](https://github.com/botuniverse/onebot-11)（正向 WebSocket + echo 关联）。
