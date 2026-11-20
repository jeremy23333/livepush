# Ubuntu 部署与调试 checklist

面向「一台自己的 Ubuntu 服务器 + 一个普通用户 + 不用 Docker」的场景。
每一步都有**验证方法**，通过再走下一步；出问题直接跳到最后的[排障对照表](#排障对照表)。

下面假定：

- 服务器用户：`ubuntu`（换成你自己的用户名，systemd 模板里的 `%i` 就是它）
- 项目目录：`/home/ubuntu/livepush`
- 监听端口：`8787`

---

## 0. 先把文件传到服务器

> **现在推荐直接 `git clone`**（见文末「以后怎么更新」）：代码在 `git@github.com:jeremy23333/livepush.git`。
> 第一次装也可以不走 tar，直接：
>
> ```bash
> sudo apt-get install -y git
> git clone git@github.com:jeremy23333/livepush.git ~/livepush
> cd ~/livepush
> ```
>
> 然后从第 1 步继续。下面这节保留给「无法访问 GitHub / 想先离线装」的情况。

### 要拷什么

**需要**（缺一个都跑不起来）：

| 路径 | 作用 |
| --- | --- |
| `pyproject.toml` | 依赖清单 + 打包配置，`pip install -e .` 必须有 |
| `app/` | 全部源代码（含 `web/templates`、`web/static`） |
| `scripts/bootstrap.sh` | 一键装环境（也可以手工照做） |
| `deploy/livepush@.service` | systemd 单元 |
| `.env.example` | 配置样例，复制成 `.env` |
| `README.md`、`docs/` | 文档 |
| `tests/`、`tools/` | 可选：自测脚本和假协议端，服务器上排查很有用 |

**不要拷**：`.venv/`、`.tmp/`、`data/`、`__pycache__/`、`.pytest_cache/`、`pip-*`、`pytest-*`、`app/probe2.txt`。

`data/` 不要拷是关键：那里面有本机的数据库和密钥，服务器上要重新生成（免得两边共用同一个 `secret.key` / 登录态）。

### 怎么拷

Windows 本机（PowerShell，在项目根目录）：

```powershell
tar -czf livepush.tar.gz --exclude .venv --exclude .tmp --exclude data --exclude __pycache__ `
    --exclude .pytest_cache --exclude "pip-*" --exclude "pytest-*" app pyproject.toml scripts deploy docs tools tests .env.example README.md .gitignore
scp livepush.tar.gz ubuntu@<服务器IP>:~/
```

Linux/macOS 本机可以直接 `rsync`（-c 会跳过未变的文件，适合以后更新）：

```bash
rsync -avc --exclude .venv --exclude .tmp --exclude data --exclude __pycache__ \
      --exclude .pytest_cache --exclude 'pip-*' --exclude 'pytest-*' \
      ./ ubuntu@<服务器IP>:~/livepush/
```

服务器上解包：

```bash
mkdir -p ~/livepush && tar -xzf ~/livepush.tar.gz -C ~/livepush && cd ~/livepush
ls    # 应看到 app/ pyproject.toml scripts/ deploy/ ...
```

> 仓库里的 `.sh` / `.service` 已经是 LF 换行，用 `scp`/`tar` 传不会坏。
> 如果你用某些 Windows 压缩软件打包，可能被转成 CRLF，`bash` 会报 `$'\r': command not found`，
> 这时跑一下 `sed -i 's/\r$//' scripts/bootstrap.sh deploy/livepush@.service` 即可。

---

## 1. 装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates
```

**验证**（必须 ≥ 3.10）：

```bash
python3 -V          # 期望 Python 3.10.x / 3.11.x / 3.12.x / 3.13.x
python3 -m venv --help >/dev/null && echo "venv OK"
```

- Ubuntu 22.04 自带 3.10，Ubuntu 24.04 自带 3.12，都够用。
- 只有 **内置 Milky 协议端 + 自建签名服务** 才需要额外的一堆图形库（`build-essential`、`libvips42` 等），
  那是第 7 步的事，先不用装。
- 服务器是 ARM（`uname -m` 不是 `x86_64`）时：自建签名不可用，只能走「外接 OneBot11」免签路线。

---

## 2. 建虚拟环境、装依赖

```bash
cd ~/livepush
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel
.venv/bin/python -m pip install -e .
```

国内网络慢就加镜像（任选其一）：

```bash
.venv/bin/python -m pip install -e . -i https://mirrors.aliyun.com/pypi/simple/
# 或 https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/
```

`-e .`（可编辑安装）的好处：以后改了 `app/` 里的代码只要重启服务，不用重装。

**验证**：

```bash
.venv/bin/python -c "import livepush, fastapi, sqlalchemy, greenlet, websockets, qrcode; print('deps OK')"
```

`greenlet` 单独列出来是有原因的：SQLAlchemy 从 2.1 起把它放进了 `[asyncio]` extra，
依赖写成 `sqlalchemy>=2.0` 就不会装上，`livepush init` 会在 `from sqlalchemy.ext.asyncio import ...` 处
报 `ModuleNotFoundError: No module named 'greenlet'`。本项目已改成 `sqlalchemy[asyncio]>=2.0`；
如果你手上的副本还是旧版，见排障表最后一行。

> 也可以直接 `bash scripts/bootstrap.sh`：它做的正是第 1、2 步，
> 外加创建 `data/` 下各子目录。唯一要注意的是它假定项目就在脚本的上一级目录。

---

## 3. 初始化数据目录与管理员口令

```bash
cd ~/livepush
mkdir -p data/logs data/covers data/protocol data/sign   # systemd 日志目录必须先存在
.venv/bin/python -m livepush init
```

**验证**：

```bash
ls -l data/                       # 应有 livepush.db、secret.key、admin-password.txt
cat data/admin-password.txt       # 这就是 Web UI 的登录口令
```

三样东西的作用（**别外传、别提交进 git**）：

- `data/livepush.db` —— 全部业务数据（直播间、QQ 连接、推送目标、绑定、事件、推送任务）
- `data/secret.key` —— 会话 Cookie 的签名密钥，删了所有人要重新登录
- `data/admin-password.txt` —— 登录口令，想换就改 `LIVEPUSH_ADMIN_PASSWORD` 或直接改这个文件后重启

想改监听地址/端口，`cp .env.example .env` 后编辑；不改就用默认 `127.0.0.1:8787`。

---

## 4. 自检（最重要的一步）

```bash
cd ~/livepush
.venv/bin/python -m livepush doctor
```

输出是一串 `[ OK ]` / `[FAIL]`，**每一项都带一句「↳ 怎么办」**。9 项含义：

| 检查项 | 不通过时的含义 |
| --- | --- |
| Python 依赖 | 第 2 步没装成，回去看 pip 报错 |
| 数据目录 | 权限不对，或 `LIVEPUSH_DATA_DIR` 指错地方 |
| SQLite | 数据库文件损坏/磁盘满；可删 `data/livepush.db` 重建（数据会丢） |
| B 站接口 | `code=-352/-412` 是 IP 被风控；先确认服务器能直连 B 站 |
| 签名服务 | 只有内置 Milky 才需要；用托管或外接时忽略这条 |
| QQ 连接 | 还没配机器人 → 第 6 步；配了没起 → 看第 5、8 步 |
| 绑定关系 | 有「悬空」绑定（目标被删了），去 Web UI 删掉重建 |
| Docker | 托管模式才需要；没装会 FAIL 并提示，用外接/内置可忽略 |
| 托管容器 | 有托管连接但容器没在跑：去 `/qq` 点「启动」，或看 `docker logs` |

想给脚本用：`.venv/bin/python -m livepush checks` 输出同样的内容（JSON）。

> `doctor` 只**看**当前连接状态，不会主动去连协议端，所以不会卡住。

---

## 5. 前台试跑一次

先别上 systemd，前台跑能看到所有输出：

```bash
cd ~/livepush
.venv/bin/python -m livepush run
```

预期输出里有 `Uvicorn running on http://127.0.0.1:8787`。

**从自己电脑访问**（服务只监听 127.0.0.1，最省事的是 SSH 隧道）：

```bash
ssh -N -L 8787:127.0.0.1:8787 ubuntu@<服务器IP>
```

然后浏览器打开 `http://127.0.0.1:8787`，用第 3 步的口令登录。

> 想让局域网直接访问：`.env` 里 `LIVEPUSH_HOST=0.0.0.0`，并在云厂商安全组/`ufw` 放行端口。
> 但**没有 HTTPS 就别往公网暴露**——口令是明文表单提交的。公网请用 Nginx/Caddy 反代 + HTTPS。

**这一步的验证清单：**

- [ ] 登录页能打开，口令能登进去
- [ ] `curl -s http://127.0.0.1:8787/healthz` 返回正常
- [ ] 点「立即检查」，`/logs` 页出现采集记录

试跑完 `Ctrl+C` 停掉，进入 Web UI 配置（第 6 步可以在试跑期间做）。

---

## 6. 在 Web UI 里把链路配通（按这个顺序）

顺序很重要，后面的步骤要选前面的东西：

1. **`/qq` 添加 QQ 连接**
   - 有现成协议端（LLBot / Lagrange / waylay 等）→ 选 **外接 + OneBot 11**，填 `ws://127.0.0.1:3001` 和 Token
   - 想用内置 → 选 **内置 + Yogurt / Lagrange.Milky**，需要先做第 7 步（签名 + 协议端二进制）
   - 保存后点「连接」，确认状态变成「已就绪」，能读到账号昵称
2. **`/rooms` 添加直播间**：填 UID 或房间号（`672328094`、`22637261`、`https://live.bilibili.com/22637261` 都行），点解析确认名字没认错。
   想立刻验证推送链路，就勾上「**正在直播则立即推送**」——否则首次采集只记录状态、不发消息（这是有意的，避免加一堆在播房间时刷屏）。
   这个开关是**直播间级**的，添加后在列表里还能改
3. **`/targets` 添加推送目标**：选群聊还是私聊，填群号/QQ 号；从协议端拉列表或手填
4. **`/bindings` 把直播间和目标绑起来**：可选只推开播/只推下播、@全体、自定义文案模板
5. **`/settings` 调参数**：轮询间隔、封面模式（关 / URL / 内嵌 base64）、下播连续确认次数等

渲染模板可用占位符：`{uname}{title}{area}{url}{duration}{online}{room_id}{uid}{prev_title}`。

---

## 7. 可选：内置模式（自建签名 + 协议端）

**只有想用一个进程全包时才需要**。外接 OneBot11 的话整节跳过。

```bash
cd ~/livepush
bash scripts/bootstrap.sh --with-sign
```

它会：装 GUI 依赖 → 拉 `ntqq-sign-server`（`master` 分支）→ **向官方接口解析「当前」QQ Linux 的 x64 deb 地址**
（`https://im.qq.com/rainbow/linuxQQDownload`，版本会变，脚本不钉死）→ 下载前先探活 → 抽出 `wrapper.node`
→ 编译 `libsymbols.so` → 全部放进 `data/sign/`（约 150 MB）。

> 腾讯会删掉旧版本的安装包。所以**不要手动钉死某个版本**——脚本每次运行都重新解析。
> 确实需要指定时：`QQ_DEB_URL="https://.../QQ_xxx_amd64_01.deb" bash scripts/bootstrap.sh --with-sign`

> **已知状态（2026-11 实测）**：官方接口下发的 `QQ_3.2.18_250626_amd64_01.deb` 以及该目录下的
> rpm / AppImage / arm64 全部返回 **404**；对照组「同域名的微信 Linux deb」正常 200 —— 所以是**上游把那个目录下线了**，
> 不是你的网络。`im.qq.com/linuxqq` 页面同样用这个接口拼链接，因此官网点击下载也是坏的。
>
> 好消息：`qqdl.gtimg.cn` 上仍有可用的构建（按构建号组织），例如
> `https://qqdl.gtimg.cn/qqfile/QQNT/9.9.37/beta/816b3c79/linuxqq_3.2.35-54395_amd64.deb`（188 MB）。
> 实测：能下、`wrapper.node` 是 145 MB 的 ELF x86_64、`sign.py` 的偏移**自动探测成功**（`0x6856284`）。
> 脚本已把这条地址列为备选，会自动探活；失效就跳过，不影响其他候选。

拿不到安装包时脚本会打印三条出路并以退出码 **3** 结束（不会留下半装状态）：

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `curl: (22) ... 404` | 该版本的 deb 已被上游删除 | 重跑脚本（重新解析当前地址），或 `QQ_DEB_URL=` 指定新地址 |
| `curl: (28)` 超时 / 卡住 | 服务器到 `dldir1*.qq.com` 不通（常见于 NAT / 代理） | 挂代理，或改走外接 OneBot11，跳过签名 |
| 解包后找不到 `wrapper.node` | QQ 包结构变了 | 脚本会 `find` 打印候选路径；按下面的手动方式放置 |

先确认地址本身是否有效（404 = 地址失效，超时 = 网络问题）：

```bash
curl -fsSI "https://dldir1v6.qq.com/qqfile/qq/QQNT/Linux/QQ_3.2.18_250626_amd64_01.deb" | head -3
```

**在你的电脑上下载、再传到服务器**（服务器下不动、但本机能下时最省事）：

```bash
# 本机：浏览器打开 https://im.qq.com/linuxqq 下载 amd64 .deb，然后
scp QQ_3.2.18_250626_amd64_01.deb ubuntu@<IP>:~/
# 服务器：用 file:// 让脚本直接吃本地文件（不会再走网络）
QQ_DEB_URL="file://$HOME/QQ_3.2.18_250626_amd64_01.deb" bash scripts/bootstrap.sh --with-sign
```

如果连本机也下不动，说明是全局网络问题，走第 7 节下面的手动放置或外接 OneBot11。

**完全手动安装**（脚本路线失败时的兜底）：解开 deb，取出 `opt/QQ/resources/app/wrapper.node`，
连同 `sign.py`、`symbols.c` 放进 `data/sign/`，再编译符号桩：

```bash
cd ~/livepush/data/sign
gcc -std=c99 -shared -fPIC -o libsymbols.so symbols.c
```

然后在 Web UI「设置」页点 **启动自建签名**，再点 **签名体检**：

- 显示「可用」+ 平台/版本 → 继续
- 显示「版本不匹配」→ 见下面的「版本要求」，换对应版本重装（这是 Code 45 的根源）

**版本要求（不是非得 3.2.18）**：签名靠 `wrapper.node` 里的函数地址偏移。`sign.py` 先查已知表
（`3.2.27-47354` / `3.2.19-39038` / `3.2.18-36497`），查不到就用字节模式**自动探测**；探测失败还可以手动指定 ——
在 `.env` 里写：

```bash
LIVEPUSH_SIGN_EXTRA_ARGS="--offset 0x59660D0"
```

所以只要有**任意一份 Linux x86_64 的 QQ 安装包**就能用。两个硬约束：

1. 必须是 **Linux** 版包里的 `wrapper.node`（ELF 文件）；Windows 版里的同名文件是 PE，Linux 上 `dlopen` 不了
2. 版本越新越可能不在已知偏移表里，靠自动探测；探测不出就手动 `--offset`

**已实测的版本**：`3.2.35-54395`（`qqdl.gtimg.cn` 上的 beta 构建）—— `wrapper.node` 145 MB、ELF x86_64、
自动探测得到 `0x6856284`。不在已知偏移表里也能用，因为它能自动探测出来。

不过「能探测到偏移」只证明函数还在**同样的位置形状**上；真正能签发还要在 Linux 上 `dlopen` 跑通，
所以装完请务必点一次「签名体检」，再走一遍登录（报 Code 45 就说明这个版本的 ABI 变了，换版本或走外接 OneBot11）。

当前卡住的**不是**版本挑不出来，而是「一个 Linux QQ 包都下不到」（见上面的已知状态）。

再把协议端二进制放到 `data/protocol/`（本项目不重分发，需自行从上游 releases 下载）：

```
data/protocol/yogurt/yogurt              # chmod +x
data/protocol/lagrange_milky/Lagrange.Milky   # chmod +x
```

回 `/qq` 页用内置模式建连接 → 点「登录」→ 页面显示二维码 → 手机 QQ 扫码。
二维码 180 秒过期会自动换一张（最多 3 次），不必手动重启。

---

## 7b. 可选：托管 NapCat / SnowLuma（Docker，免签名）

不想折腾签名就优先走这条。前提只有一条：**当前用户能直接执行 `docker`**（`docker version` 能看到 Server 段）。

```bash
docker version --format '{{.Server.Version}}'     # 有输出才继续
groups | tr ' ' '\n' | grep -x docker             # 没有的话：sudo usermod -aG docker $USER 重新登录
```

然后在 Web UI 里做（不用敲命令）：

1. `/qq` →「连接方式」选 **托管第三方 NTQQ**；
2. 后端选 `napcat`（网页/日志扫码）或 `snowluma`（自带 WebUI + 远程桌面）；
3. 填 QQ 号 → 创建连接；
4. 列表里点「托管」→「安装并启动」，等下载进度走完（镜像 600–740 MB，只下一次）；
5. NapCat：安装完成后打开它的 WebUI 扫码，或在「查看日志」里找二维码链接；
   SnowLuma：打开它自己的 WebUI 登录，需要桌面时用远程桌面（密码在日志里）；
6. 状态变成「已连接」后，去 `/bindings` 把它绑给群。

命令行等价操作（适合没有浏览器时）：

```bash
cd ~/livepush
.venv/bin/python -m livepush hosted list          # 看容器与端口
.venv/bin/python -m livepush hosted install 1     # 安装/重装 1 号连接
.venv/bin/python -m livepush hosted logs 1        # 看容器最近输出（找二维码/密码）
```

要点：

- 镜像会校验 sha256，校验不过会删掉重下；
- 容器端口固定、宿主机端口随机分配（`30000-34000` 段），只监听 `127.0.0.1`；
- 数据在 `data/hosted/<backend>-<id>/`，删连接不会自动删目录；
- **镜像来自社区归档备份，不会更新**。QQ 大版本升级后镜像里的客户端可能登不上，那时改走「外接」路线。
  细节与排障见 [`HOSTED.md`](HOSTED.md)。

---

## 8. 交给 systemd 常驻

确认第 5 步前台跑通了再上。

```bash
sudo cp ~/livepush/deploy/livepush@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now livepush@ubuntu      # 把 ubuntu 换成你的用户名
```

单元文件里的约定（**对不上就改它**）：

- `User=%i` —— 实例名就是运行用户
- `WorkingDirectory=/home/%i/livepush` —— 项目必须放在 `/home/<用户>/livepush`
- `ExecStart=/home/%i/livepush/.venv/bin/python -m livepush run`
- `Environment=LIVEPUSH_DATA_DIR=/home/%i/livepush/data`
- `KillMode=control-group` —— 停服务时把签名服务/协议端子进程一起带走，不然会残留占端口

如果项目不在 `/home/<用户>/livepush`（比如放在 `~/program/livepush`），装之前先改掉全部路径：

```bash
sudo sed -i 's#/home/%i/livepush#/home/%i/program/livepush#g' /etc/systemd/system/livepush@.service
grep -n livepush /etc/systemd/system/livepush@.service    # 确认四条路径都换成了新位置
```

**验证**：

```bash
systemctl status livepush@ubuntu --no-pager
curl -s http://127.0.0.1:8787/healthz; echo
sudo systemctl restart livepush@ubuntu     # 改完配置后
```

> ⚠️ **`.env` 不生效的坑**：单元文件里的 `Environment=` 优先级**高于** `.env`。
> 所以你改了 `.env` 的 `LIVEPUSH_HOST/PORT/DATA_DIR` 却没用时，去改 `/etc/systemd/system/livepush@.service`，
> 改完 `sudo systemctl daemon-reload && sudo systemctl restart livepush@ubuntu`。

---

## 9. 日志在哪看

**所有业务日志都在 `data/logs/` 下**，一共四类：

| 文件 | 内容 | 什么时候看 |
| --- | --- | --- |
| `data/logs/app.log` | 主日志：采集、状态变化、事件、推送结果、异常栈 | **出任何问题先看这个** |
| `data/logs/service.log` | systemd 下进程的 stdout/stderr：启动横幅、端口、uvicorn 访问日志 | 服务起不来、怀疑没启动 |
| `data/logs/sign.log` | 自建签名服务的输出 | 扫码报 Code 45 / 签名体检失败 |
| `data/logs/yogurt-<id>.log`、`data/logs/lagrange_milky-<id>.log` | 内置协议端的完整输出（含二维码图片路径、登录态） | 二维码不出、登录失败、掉线 |
| `docker logs livepush-<backend>-<id>` | 托管容器的输出（NapCat 二维码、SnowLuma 初始密码） | 托管模式扫码/登录失败 |

systemd 自身的信息（被杀、崩溃、启动失败原因）在 journal：

```bash
journalctl -u livepush@ubuntu -n 200 --no-pager
journalctl -u livepush@ubuntu -f          # 实时跟踪
```

实时排障常用组合：

```bash
cd ~/livepush
tail -f data/logs/app.log data/logs/service.log
grep -iE "error|traceback|412|352|45" data/logs/app.log | tail -50
```

Web UI 里也有三个入口：**`/logs` 页**看每条推送的任务状态和错误原文；
**`/qq` 页**看连接状态并能直接展开协议端日志；**`/settings` 页**做签名体检。

### 9b. 日志保留、故障告警、掉线探测（2.1.0）

这三项都在 **设置页**，不用改 `.env`：

| 想做什么 | 设置页面板 | 关键配置 |
| --- | --- | --- |
| 日志别无限涨 | 日志与清理 | `log_retention_days`（默认 14，0 关闭）、`log_cleanup_hour`（默认 4 点）、`log_service_truncate_mb` |
| 出错主动通知 | 故障告警 | `alert_enabled`、`alert_wecom_url`、`alert_min_level`、`alert_cooldown_sec`、`alert_max_per_minute` |
| 发现 QQ 被踢 | QQ 掉线探测 | `bot_health_interval_sec`、`bot_offline_grace_sec`、`bot_good_fail_threshold` |

日志保留的行为：**每天到点跑一次 + 服务启动时跑一次**；只删 `mtime` 超过 N 天的轮转文件（`app.log.1`、`yogurt-3.log.2` 等）；
`app.log` 与 `service.log` 永不删除，后者只在启动时截断到 `log_service_truncate_mb`。

```bash
python -m livepush logs list                 # 每个文件多大、多久没动、属于哪个分区
python -m livepush logs cleanup --dry-run    # 演练
python -m livepush logs cleanup              # 按设置执行
```

故障告警的三层收敛：等级过滤 → 同类错误冷却（数字/十六进制归一） → 每分钟上限。
告警本身写 `livepush.alerts` logger，不会自己告警自己。配好后点「发送测试告警」验证。

掉线探测：QQ 被风控踢下线时 WebSocket 往往还连着（NapCat 的 `get_status` 甚至仍报 online），
所以看门狗看「`get_login_info` 真实往返 + 心跳年龄 + `status.good` + 下线 notice + 容器状态 + 容器日志关键词」，
连续异常超过宽限期才告警，**只告警不自动重启**。

### 9c. 添加直播间失败（502）怎么定位

页面详情现在会区分阶段；服务器上直接跑：

```bash
python -m livepush bili "https://live.bilibili.com/22637261"
```

五步（解析 / DNS / HTTP 接口 / 批量状态 / 房间详情）逐项给 OK 或 FAIL 与耗时。
也可以 `GET /api/diag/bili?query=...`（需登录）。失败会写进 `livepush.api` logger：

```bash
grep 'livepush.api' data/logs/app.log | tail -n 20
```

### 9d. 内嵌托管 WebUI（可选）

托管模式（NapCat/SnowLuma）的 WebUI 默认只监听 `127.0.0.1`。经 Cloudflare Tunnel 或反代访问 livepush 时，
可以直接在 livepush 里打开：**`/qq` → 列表里点「托管」→「在 livepush 内打开」**，
或直接访问 `http://<livepush 地址>/hosted/<bot_id>`。

它是同源反向代理（HTTP + WebSocket），复用 livepush 的登录态；因此不需要再给 WebUI 单独开端口或加隧道路由。
第三方界面大版本升级后若加了 Service Worker，可能仍需直接暴露端口。

---

## 10. 端到端验收

- [ ] 加一个**此刻正在直播**的房间，30 秒内 `/logs` 出现开播事件和 `sent` 的任务
- [ ] 群里真的收到消息（文本 + 封面图），@全体（如果开了）正常
- [ ] 点「立即检查」能立刻触发一次采集，不用等下一个周期
- [ ] 停掉协议端再发一次 → `/logs` 里看到重试和最终失败原因，协议端恢复后能自动补发
- [ ] `sudo reboot` 后服务自动起来（`systemctl is-enabled livepush@ubuntu` 应为 `enabled`）
- [ ] 总览页「实时日志」有输出，切等级/分区能过滤，暂停与清空可用
- [ ] 设置页「发送测试告警」能在企业微信群里收到 markdown
- [ ] `python -m livepush logs list` 有输出；把某个轮转文件 `touch -d` 成 20 天前，`logs cleanup` 后它消失而 `app.log` 还在
- [ ] 绑定弹窗里「发测试推送」能收到带「测试消息」标注的群消息
- [ ] 托管模式下「在 livepush 内打开」能进 NapCat WebUI

---

## 排障对照表

| 现象 | 先看哪里 | 常见原因 / 处理 |
| --- | --- | --- |
| `systemctl status` 里 `Result=exit-code`、反复重启 | `journalctl -u livepush@ubuntu -n 100` | 路径写错（不在 `/home/<用户>/livepush`）、venv 里没装包、端口被占 |
| `StandardOutput=append` 报错、状态 `EXIT_FILE` | —— | `data/logs/` 目录不存在，`mkdir -p data/logs` 后重启 |
| 浏览器打不开页面 | `curl -sI http://127.0.0.1:8787` | 只监听 127.0.0.1（正常，用 SSH 隧道）；或安全组没放行 |
| 登录总提示口令不对 | `cat data/admin-password.txt` | 复制时带了空格；或 `.env` 里 `LIVEPUSH_ADMIN_PASSWORD` 覆盖了它 |
| 登录后马上跳回登录页 | `app.log` | `data/secret.key` 被删过（重启会重新生成），重新登录即可 |
| 「B 站接口」FAIL，`code=-412` | `app.log` 里的 412 | 服务器 IP 被 B 站风控：等解封、换 IP、或在设置里配 HTTP 代理 / Cookie（`buvid3`） |
| 直播间加了但不推送 | `/logs` 有没有事件 | 刚添加时只记状态、不推送（**首见不推**）：要么勾「正在直播则立即推送」重加，要么等下次开播；另外检查绑定是否启用、直播间当时是否真在播 |
| 有事件但任务一直 `pending`/`failed` | `/logs` 的错误列 | QQ 连接没就绪（去 `/qq` 看）；或机器人不在那个群里 |
| QQ 连接一直是「未就绪」 | `/qq` 页 + 协议端日志 | 外接：WS 地址/Token 错、协议端没起；内置：没扫码或已掉线 |
| 二维码空白/不显示 | `data/logs/<协议端>-<id>.log` | 协议端还没输出二维码；或版本不匹配根本没走到登录 |
| 登录报 **Code 45** | `sign.log` + 设置页签名体检 | 签名服务没起 / QQ 版本与签名服务版本不匹配 / `protocol.version` 写了 `fetched` |
| 消息发出去但群里没有 | `/logs` 里 `retcode` | 机器人被禁言、群号填错、风控（连续发送过快） |
| 停服务后端口还占着 | `ss -lntp \| grep 8787` | 上次没走 systemd（`KillMode=control-group` 才管子进程），`pkill -f livepush` |
| 时间显示不对 | `timedatectl` | 服务器时区不是 `Asia/Shanghai`：`sudo timedatectl set-timezone Asia/Shanghai` |
| 改了 `.env` 却没生效 | `/etc/systemd/system/livepush@.service` | 单元里的 `Environment=` 优先级高于 `.env`；改 unit 后 `daemon-reload` + `restart` |
| `init`/`run` 报 `ModuleNotFoundError: No module named 'greenlet'` | —— | SQLAlchemy 2.1 起 greenlet 只在 `[asyncio]` extra 里。救急：`.venv/bin/python -m pip install greenlet`（或 `pip install "sqlalchemy[asyncio]"`）；根治：确认 `pyproject.toml` 写的是 `sqlalchemy[asyncio]>=2.0` 后重跑 `pip install -e .` |
| `bootstrap.sh --with-sign` 报 404 / 超时 | 见第 7 节表格 | 上游已删除旧版本 deb：重跑脚本让它重新解析地址，或 `QQ_DEB_URL=` 指定；实在不行走外接 OneBot11 免签 |
| 托管模式安装立刻失败、提示找不到 Docker | `docker version` | 没装 Docker 或当前用户不在 `docker` 组（`sudo usermod -aG docker $USER` 后重新登录）；也可改走外接 |
| 托管容器在跑但「连接」失败 | `/qq` 托管面板的日志 | 首次启动要加载 QQ，等 1–2 分钟再点「连接」；或镜像里的 QQ 版本过旧，换后端/走外接 |
| SnowLuma 不知道 WebUI/远程桌面密码 | `.venv/bin/python -m livepush hosted logs <id>` | 首次启动的临时密码打印在容器日志里 |
| 添加直播间报 502 | 页面详情 / `python -m livepush bili "<输入>"` | 详情会写阶段（DNS、接口、业务码、房间不存在）；429 是风控、404 是输入错 |
| 企业微信没收到告警 | 设置页「故障告警」 | webhook 地址/开关/等级；企业微信限 20 条/分钟，超限会被丢弃 |
| 实时日志一片空白 | 总览页日志面板 | 只显示进程启动后的日志；刚重启是正常的，去 `/settings` 看落盘文件 |
| 内嵌 WebUI 白屏 | 浏览器控制台 + `app.log` 的 `livepush.api` | 第三方前端若用了 Service Worker 或绝对路径 module import，需直连端口；先用 `/qq` 里的直链确认 WebUI 本身正常 |

---

## 附：以后怎么更新（Git 流程）

代码在 GitHub 上：`git@github.com:jeremy23333/livepush.git`，分支 `main`，发布打 tag（如 `v2.1.0`）。
服务器上只做「拉取 + 装依赖 + 重启」，**数据（`data/`）不在仓库里，pull 不会碰它**。

### 一次性：把现有的 tar 部署换成 git 仓库

服务器现在（第 0–10 步装的）不是 git 仓库。**原地转换**最省事，`data/` 与 `.venv/` 都是未跟踪文件，不会被覆盖：

```bash
# 1) 停服务 + 备份数据
sudo systemctl stop livepush@alan
cp -a ~/program/livepush/data ~/livepush-data-backup-$(date +%F)

# 2) 让服务器能读 GitHub（用只读 Deploy Key，最省心）
ssh-keygen -t ed25519 -C "livepush@$(hostname)" -f ~/.ssh/livepush_deploy -N ""
cat ~/.ssh/livepush_deploy.pub
# 把这个公钥粘到 GitHub → 仓库 Settings → Deploy keys → Add deploy key（不要勾 Allow write）

cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/livepush_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com        # 看到 "successfully authenticated" 即可

# 3) 原地转换（注意：会强制覆盖被跟踪的源码文件；data/、.venv/、.env 保留）
cd ~/program/livepush
git init -b main
git remote add origin git@github.com:jeremy23333/livepush.git
git fetch origin main
git checkout --force -B main origin/main

# 4) 依赖与自检（venv 路径没变，不需要重建）
.venv/bin/python -m pip install -q -e .
.venv/bin/python -m livepush init
.venv/bin/python -m livepush doctor

# 5) 起服务
sudo systemctl start livepush@alan
```

> 转换前如果 `~/program/livepush` 里有你手工改过、又没提交的文件（比如 `.env`），它们会作为未跟踪文件留着；
> 但如果是**被仓库跟踪的文件**（例如 `app/...`），会被远端版本覆盖——真有本地改动请先 `git stash` 或在 GitHub 上合掉。

### 日常升级（推荐用脚本）

```bash
cd ~/program/livepush
bash scripts/upgrade.sh
```

它做的事：检查工作区干净 → `git fetch --all --tags --prune` → `git merge --ff-only` 到 `main` →
`pip install -e .` → `livepush init`（幂等）→ `livepush doctor` → `sudo systemctl restart livepush@alan`。

常用开关：

```bash
bash scripts/upgrade.sh --no-restart     # 只更新代码，手工验证后再重启
bash scripts/upgrade.sh --no-install     # 依赖没变时省掉 pip
bash scripts/upgrade.sh --tag v2.1.0     # 切到某个发布版本（也可用来回滚）
bash scripts/upgrade.sh --force-restart  # doctor 有失败项也重启（慎用）
```

### 手工升级（等价步骤）

```bash
cd ~/program/livepush
sudo systemctl stop livepush@alan
git fetch --all --tags --prune
git log --oneline HEAD..origin/main      # 看这次会更新什么
git pull --ff-only
.venv/bin/python -m pip install -e .
.venv/bin/python -m livepush init
.venv/bin/python -m livepush doctor
sudo systemctl start livepush@alan
```

只改了 Python / 模板时 `pip install` 可以省略；`pyproject.toml` 加了新依赖才必须重装。
数据库结构有变化时会自动建新表（`schema_version` 表），但**升级前仍建议备份**：

```bash
cp data/livepush.db "data/livepush.db.$(date +%F-%H%M).bak"
```

### 回滚

```bash
cd ~/program/livepush
git log --oneline -10                    # 找到上一个版本的 commit 或 tag
bash scripts/upgrade.sh --tag v2.0.0     # 切回旧 tag（会装依赖并重启）
# 或者：git checkout <commit> 后手工 pip install -e . && sudo systemctl restart livepush@alan
```

回滚后如果数据库里已有新表，旧版本不会去读它，一般无需处理；相反方向（旧库 → 新版本）才会自动建表。

### 本机发版

```bash
git add -A && git commit -m "feat: ..."
git push origin main
git tag -a v2.1.0 -m "livepush 2.1.0" && git push origin v2.1.0   # 打 tag 便于回滚
```

## 附：备份与迁移

要打包走的就三样：`data/livepush.db`、`data/secret.key`、`data/admin-password.txt`
（外加 `data/protocol/` 与 `data/sign/` 里的二进制，但那两样重新下载更省事）。
`data/covers/` 是封面缓存，删了会自动重建。
