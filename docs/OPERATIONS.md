# 运维手册（2.1.0）

这份文档覆盖四件事：**日志存在哪、怎么看**；**日志自动清理**；**企业微信故障告警**；**QQ 掉线探测**；最后是**把托管协议端的 WebUI 内嵌进 livepush**。

---

## 1. 日志清单

所有日志都在 `data/logs/`（`LIVEPUSH_DATA_DIR` 下的 `logs/`）。

| 文件 | 谁写的 | 内容 | 轮转 |
|---|---|---|---|
| `app.log` + `app.log.1…5` | 应用自己（`setup_logging`） | 业务主日志：采集、状态机、推送、登录、托管、Web API | 5 MB × 5 |
| `service.log` | systemd（`StandardOutput/Error=append`） | 进程 stdout/stderr：启动横幅、端口、uvicorn 访问日志、未捕获 traceback | 无轮转；启动时截断到 `log_service_truncate_mb` |
| `yogurt-<id>.log` + `.1…3` | 内置 Yogurt 子进程 | 协议端输出（扫码、登录、断线） | 5 MB × 3 |
| `lagrange_milky-<id>.log` + `.1…3` | 内置 Lagrange.Milky 子进程 | 同上 | 5 MB × 3 |
| `sign.log` + `.1…3` | 自建签名服务子进程 | 签名服务 stdout/stderr | 5 MB × 3 |

> 托管模式（NapCat / SnowLuma 容器）的日志**不在**这里，它们在 Docker 里：
> `docker logs livepush-<backend>-<id>`；Web UI 的「托管 → 查看日志」看的就是它。

### 按功能分区看 `app.log`

日志格式是 `时间 级别 logger 消息`，logger 名就是功能分区：

| logger 前缀 | 功能分区 |
|---|---|
| `livepush` | 服务启停、配置装配 |
| `livepush.monitor` | B 站采集、风控熔断、状态机、开播/下播判定 |
| `livepush.push` | 渲染与 outbox 派发、重试、目标禁用 |
| `livepush.login` | 扫码登录编排（二维码、keystore、换码） |
| `livepush.qq` | 连接注册表（客户端创建/断开） |
| `livepush.onebot11` / `livepush.milky` | 两种传输层的收发与错误 |
| `livepush.qq.hosted.*` / `livepush.hosted.*` | 托管容器：下载、docker 命令、端口探测 |
| `livepush.signer.server` / `livepush.wrapper` | 自建签名与 wrapper.node 发现 |
| `livepush.api` / `livepush.web.*` | Web API 异常、代理 |
| `livepush.alerts` | 告警自身（不会再触发告警，避免死循环） |
| `livepush.logs` | 日志清理任务 |

常用：

```bash
# 只看采集与风控
grep 'livepush.monitor' data/logs/app.log | tail -n 50

# 只看推送失败
grep -E 'livepush.push.*(失败|error)' data/logs/app.log | tail -n 50

# 进程级崩溃（没有 logger 前缀的那种）
tail -n 100 data/logs/service.log
```

### Web UI 里看

- **总览页**底部「实时日志」：内存环形缓冲（最近 1000 条），可按**等级**和**分区**筛选，支持暂停/清空。等级或分区切换会重连 SSE。
- **设置页 → 日志与清理**：列出 `data/logs/` 下每个文件的大小、年龄、分区，并可直接触发一次清理。

实时日志只存在内存里，**重启即清空**；落盘的以 `app.log` 为准。

---

## 2. 日志保留与自动清理

设置在 **设置页 → 日志与清理**：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `log_retention_days` | `14` | 保留天数；`0` = 关闭自动清理 |
| `log_cleanup_hour` | `4` | 每天几点（0–23，服务器本地时间）执行清理 |
| `log_service_truncate_mb` | `32` | `service.log` 超过多少 MB 时在启动阶段截断 |

行为：

- **每天到点跑一次**，服务启动时也先跑一次；
- 只删 `mtime` 早于「现在 − N 天」的**一层** `*.log*` 文件；
- **`app.log` 与 `service.log` 永不删除**（进程持有 inode，删了会丢日志），后者只做启动截断；
- 轮转备份（`app.log.1`、`yogurt-3.log.2` 等）会按年龄正常清理。

命令行等价操作：

```bash
python -m livepush logs list                 # 列出文件、大小、年龄、分区
python -m livepush logs cleanup --dry-run    # 只演练，不删
python -m livepush logs cleanup              # 按设置里的天数真删
```

也可以 `POST /api/logs/cleanup`（Web UI 按钮走的就是它），或用 `GET /api/logs/files` 看清单。

清理结果（删除数、释放空间、下次执行时间）记在 `app_setting` 的 `log_cleanup` 里，重启不清零。

---

## 3. 企业微信故障告警

**设置页 → 故障告警**：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `alert_enabled` | `false` | 开关（没填 webhook 时即使勾选也不会发） |
| `alert_wecom_url` | 空 | 群机器人 webhook，形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |
| `alert_min_level` | `ERROR` | 只收该等级及以上的日志（WARNING / ERROR / CRITICAL） |
| `alert_cooldown_sec` | `600` | 同一类错误多久内只推一次 |
| `alert_max_per_minute` | `10` | 每分钟硬上限（企业微信本身限 20 条/分钟） |

三层收敛：

1. 等级过滤；
2. **冷却键** = `logger + 归一化消息`（数字替换成 `#`、十六进制偏移归一成 `0x#`）；
3. 60 秒滑动窗口限流，超出直接丢弃并计入 `suppressed`。

告警内容是 markdown：等级、时间、冷却说明、原始日志（超过 3000 字符截断）。
点「发送测试告警」发一条固定文案，用来验证 webhook 是否配通。

接口：`GET /api/alerts/state`、`POST /api/alerts/test`。

> 告警自己写日志时用 `livepush.alerts` logger，且`submit()` 会跳过该 logger，不会自己告警自己。

---

## 4. QQ 掉线探测（被风控踢下线）

被踢时协议端**往往不断开 WebSocket**：

- NapCat 的 `get_status` 仍报 `online: true`（进程在、协议层断）；
- SnowLuma 心跳照发，但 `status.good` 约 105 秒后翻 false。

所以看门狗组合判断，30 秒一轮（`bot_health_interval_sec`）：

| 判据 | 触发条件 |
|---|---|
| 连接态 | 客户端不存在，或 `connected == false` |
| 真实往返 | `get_login_info` 5 秒内失败/返回空 |
| 心跳年龄 | 超过 `max(90s, 心跳间隔×3)` 没更新 |
| 心跳内容 | `heartbeat_online == false`，或 `heartbeat_good == false` 连续 N 次（`bot_good_fail_threshold`） |
| 下线通知 | `bot_status` / `offline` / `disconnect` 类 notice |
| 容器状态 | 托管容器不是 `running` |
| 容器日志 | 出现 `账号已下线`、`KickedOffline`、`login expired`、`被踢` 等关键词（取最近 60 行） |

**连续异常超过 `bot_offline_grace_sec`（默认 120 秒）才告警**，避免一次抖动就打扰；
告警走上面的企业微信通道，内容形如「QQ 账号疑似掉线（小号 / hosted）：get_login_info 失败：RuntimeError；心跳已 500 秒未更新」。

> 只告警、**不自动重启**：被踢后立刻重登通常触发更严的风控。恢复由人工决定（重新扫码 / 重启容器）。

健康状态也能在接口里看到：`GET /api/bots` 每项的 `health` 字段（`healthy`、`issues`、`suspect_seconds`、`container_state`、`last_notice` 等）。

参数在 **设置页 → QQ 掉线探测**：`bot_health_interval_sec`、`bot_offline_grace_sec`、`bot_good_fail_threshold`。

---

## 5. 内嵌托管 WebUI（免额外端口/隧道）

原本 NapCat 的 WebUI 监听 `127.0.0.1:<host_webui_port>`，从外部经 Cloudflare Tunnel 访问 livepush 时够不着。
2.1.0 起可以在 livepush 里直接开：

- 页面：`http://<你的 livepush 地址>/hosted/<bot_id>`
- 入口：`/qq` 列表里「托管」弹窗中的「在 livepush 内打开」

实现（`app/livepush/web/proxy.py`）：

- HTTP 代理 `/hosted/{bot_id}/webui/{path}` → `http://127.0.0.1:<webui端口>/{path}`，复用 livepush 的登录 Cookie 鉴权；
- WebSocket 同样代理（NapCat WebUI 的终端/调试走 WS）；
- 因为 NapCat 前端把 `/api`、`/files` 当**绝对路径**发请求（会和 livepush 自己的 `/api` 冲突），HTML 里注入了 `<base>` + 一段 shim，重写 `fetch` / `XMLHttpRequest` / `WebSocket` / `EventSource` / `history.pushState` 的 URL，并改写 HTML 属性里的 `src="/…"`、`href="/…"`；
- 不做 CSRF 校验（WebUI 自己会发 POST，不带 `X-LP`），但**必须已登录**。

> 这是「够用」的方案：第三方界面升级后若引入 Service Worker 或绝对路径 `<script type=module>` 动态 import，可能仍需直接暴露端口。

---

## 6. 502 / 添加直播间失败怎么查

1. **页面上的详情**现在会区分阶段，例如：
   - `B 站没有 ID 为 … 的直播间（code=-404）` → 输入错了或房间已注销；
   - `网络错误（stage=批量状态）` → 本机到 B 站不通/超时/代理配置问题；
   - `HTTP 502` 且详情是 `code=…` → B 站业务码（风控或参数）。
2. **服务器上一条命令定位**：
   ```bash
   python -m livepush bili "https://live.bilibili.com/22637261"
   ```
   会按「解析输入 → DNS → HTTP 接口 → 批量状态 → 房间详情」逐步打印 OK/FAIL 与耗时。
3. Web UI 也有对应接口：`GET /api/diag/bili?query=...`（需登录）。
4. 这条路径现在会写 `livepush.api` 日志并带 traceback（非 404 的情况）：
   ```bash
   grep 'livepush.api' data/logs/app.log | tail -n 20
   ```
5. 如果服务器在墙内直连 B 站不稳：设置页填 `bili_proxy`（如 `http://127.0.0.1:7890`）。客户端已改为 `trust_env=False`，**不会**被系统 `HTTP_PROXY` 环境影响，只用你填的代理。
