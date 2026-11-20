# 托管协议端（NapCat / SnowLuma）

托管 = 本项目用 Docker 帮你把第三方 NTQQ 协议端装好、起好，并把连接自动接到
`ws://127.0.0.1:<端口>`。你不需要懂它的配置，也不需要签名服务。

## 前提

- 服务器能跑 Docker（`docker version` 能看到 **Server** 段），当前用户能直接执行 `docker`；
- 磁盘至少留 3 GB（镜像 tar + 载入后的镜像）；
- 内存建议 ≥ 2 GB（NapCat 是 Electron + 虚拟显示）；
- 本机 `30000-34000` 有连续 4 个空闲端口（自动分配）。

本项目**不会**自动安装 Docker，也不会构建自己的镜像。

> 想在没有 Docker 的机器上演练整套流程（安装进度、容器状态、绑定），可以用
> `tools/fake_docker.py`：`chmod +x tools/fake_docker.py` 后
> `export LIVEPUSH_DOCKER_BIN=$PWD/tools/fake_docker.py`。
> Windows 上写成 `LIVEPUSH_DOCKER_BIN=".venv/Scripts/python.exe tools/fake_docker.py"` 即可。

## 在 Web UI 里安装

1. 打开 `/qq` →「连接方式」选 **托管第三方 NTQQ** → 选后端（napcat / snowluma）→ 填 QQ 号 → 创建连接；
2. 列表里点「托管」→「安装并启动」；
3. 安装进度会显示下载百分比（镜像 600–740 MB），完成后自动探测 OneBot11 端口并绑定；
4. 登录：
   - **NapCat**：首次启动会在容器日志里打印二维码链接，并把二维码图片写到
     `data/hosted/napcat-<id>/cache/qrcode.png`；也可以打开它的 WebUI
     （`http://<服务器>:<webui端口>/webui`）。
   - **SnowLuma**：不在这里扫码——打开它自己的 WebUI（`http://<服务器>:<webui端口>`）登录，
     需要看桌面时用远程桌面（`http://<服务器>:<remote端口>`，密码打印在容器日志里）；
     OneBot 连接建议在它的 WebUI 里配置，指向我们分配的端口与 Token。
5. 绑定时如果提示「端口没有响应」，等 1–2 分钟点「连接」重试（首次要加载 QQ）。

### NapCat WebUI 的令牌怎么来的

NapCat 每次启动会生成一个 WebUI 登录令牌，2.1.0 及更早只能在容器日志里手抄，且日志窗口只有 60 行、经常已被挤出。现在 `/qq → 托管` 弹窗会直接显示令牌并提供「复制」「带令牌打开」，内嵌页 `/hosted/<id>` 的 iframe 也会自动带上 `?token=`。读取顺序：

1. **配置文件**（首选）：容器内 `/app/napcat/config/webui.json` 就是宿主机 `data/hosted/napcat-<id>/config/webui.json`（我们把这个目录挂进去了），直接读；
2. **`docker exec cat`**：文件还没落盘时，从容器里 `cat` 同一个路径；
3. **启动日志**：最后再从前 300 行日志里正则捞（这种情况可能是一次性令牌）。

令牌只在内存缓存 10 秒；重装/重启容器后点一下「刷新状态」即可重新读取。三条都拿不到时会提示「手动登录」，不会阻塞其他功能。

> 安全边界：令牌只通过已登录的 livepush 会话展示，`/hosted/<id>/webui/*` 反代本身要求 livepush 登录态；不要把这几个地址公开到无鉴权的隧道上。

## 命令行

```bash
python -m livepush hosted list          # 列出所有托管容器
python -m livepush hosted install 3     # 安装/重装 3 号连接（前台等进度）
python -m livepush hosted start 3       # 启动容器 + 等待端口 + 绑定
python -m livepush hosted stop 3        # 停容器（不删，数据保留）
python -m livepush hosted logs 3        # 看容器最近 300 行输出（NapCat 令牌在启动早期）
```

## 目录与端口布局

```
data/hosted/napcat-3/
├── artifacts/  镜像 tar（校验通过后保留，重装可直接复用）
├── config/     NapCat 的 onebot11_<uin>.json（我们生成）
├── cache/      NapCat 的二维码 qrcode.png
├── data/       （SnowLuma 用）
└── local/      （SnowLuma 用）
```

每个连接固定 4 个本机端口（仅监听 `127.0.0.1`）：

| 端口 | NapCat | SnowLuma |
| --- | --- | --- |
| ws | 3001 | 3001 |
| http | 3000 | 3000 |
| webui | 6099 | 5099 |
| remote | — | 6081 |

这些容器端口是镜像内的约定，宿主机侧用随机高位端口映射，不会跟别的服务抢 3000/3001。

## 镜像来源与校验

| 后端 | 产物 | 大小 | sha256（前 8 位） |
| --- | --- | --- | --- |
| NapCat 4.18.42 | `napcat-docker-v4.18.42-amd64.tar` | 612 MB | `85e3fd02` |
| NapCat 4.18.42 | `napcat-docker-v4.18.42-arm64.tar` | 597 MB | `0d145948` |
| SnowLuma 2.4.2 | `SnowLuma-v2.4.2-docker-amd64.tar` | 736 MB | `2c84c9da` |
| SnowLuma 2.4.2 | `SnowLuma-v2.4.2-docker-arm64.tar` | 724 MB | `3142343b` |

- 下载后会校验大小与完整 sha256，不符会删掉重下；
- GitHub 慢时可用镜像前缀：`LIVEPUSH_ARTIFACT_MIRROR=https://ghproxy.example.com/`；
- 手动下载放到 `data/hosted/<backend>-<id>/artifacts/` 再点安装也能跳过下载。

> **风险**：NapCat 主仓库已被清空，这两个仓库是社区归档备份，镜像**不会再有更新**。
> QQ 客户端继续升级后，镜像里的 QQ 可能登录失败；那时只能换新备份或改走「外接」路线。

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 安装立刻失败，提示找不到 Docker | Docker 没装 / 当前用户无权限 | 装 Docker，或把用户加入 `docker` 组后重新登录 |
| 下载中断 | 网络 | 再点一次「安装」会断点续传；或手动放好 tar |
| 容器在跑但端口无响应 | 首次启动慢 / 镜像里 QQ 版本过旧 | 看「查看日志」；1–2 分钟后点「连接」重试 |
| 扫码后仍显示未登录 | 账号被风控 / 需要验证码 | 在手机 QQ 完成验证，或换号 |
| SnowLuma 不知道密码 | 首次启动生成临时密码 | `python -m livepush hosted logs <id>` 里找「远程桌面密码」 |
| 端口被占用 | 30000-34000 段不够 | 停掉占用进程，或改 `manager.allocate_ports` 起点 |

## 卸载

- 只停：`python -m livepush hosted stop <id>`（数据保留，下次「启动」即恢复）；
- 彻底删：Web UI 的「删除容器」或 `hosted remove`，再手动删 `data/hosted/<backend>-<id>/`
  和 `docker rmi <镜像>`。
