# QQ 登录路线（v2 调研结论）

> 结论先行：**推荐「托管 NapCat（Docker）」**——免签名、不依赖公共签名服务；
> 没有 Docker 或不想跑容器时，用「外接 + OneBot 11」接现成协议端；
> 「内置 Yogurt / Lagrange.Milky + 自建签名」只在你能稳定维护签名服务时使用。

## 为什么需要这份文档

2025–2026 年 QQ 协议端生态发生了两次剧变，导致「随便找个协议端就能用」的时代结束了：

| 变化 | 影响 |
| --- | --- |
| `NapNeko/NapCatQQ` 主仓库于 2026-11-12 被清空（仅剩 Ending） | 官方更新停摆，只有社区归档备份可用 |
| 公共签名服务 `sign.lagrangecore.org` 不再对外（`/`、`/api/`、`/health` 全部 404） | Lagrange/Yogurt 必须自建签名 |
| `go-cqhttp` / `QSign` 事实停更 | 老教程不能照抄 |
| 腾讯下线 QQ Linux 官方下载目录（`qqfile/qq/QQNT/Linux/*` 全 404） | 自建签名要自己找可用的 QQ deb |

本项目因此把「QQ 怎么登」做成三条可选路线，而不是绑定一种。

## 四条路线

### A. 托管 NapCat / SnowLuma（Docker，免签名）— 推荐

- 原理：它们把真实 QQ 客户端的 `wrapper.node` 当原生模块加载进 QQ 的 Electron 主进程，
  在进程内 hook 收发包，**不需要签名服务器**。版本号、appid、qua 都从已安装的 QQ 读取，
  所以不怕 QQ 升级（但镜像里的 QQ 需要较新）。
- 本项目做的事：下载镜像 tar → `docker load` → 分配本机端口 → 写 OneBot11 配置 →
  `docker run` → 探测 `get_login_info` → 把连接指向 `ws://127.0.0.1:<port>`。
- 代价：需要 Docker；镜像 600–740 MB；NapCat 是 Electron + 虚拟显示，内存占用不低。
- 详见 [HOSTED.md](HOSTED.md)。

### B. 内置 Yogurt / Lagrange.Milky + 自建签名

- 原理：纯协议实现（不跑真实 QQ），因此必须解决 sec-sign 签名。
- 自建签名 = 把某个 QQ 版本的 `wrapper.node` 载入一个本地小服务（`ntqq-sign-server`），
  对每个包做签名；本项目把它当子进程托管（`SignServer`）。
- 关键：签名服务需要「与协议端版本匹配的偏移」；偏移写在 `KNOWN_OFFSETS` 里，
  对不上时用自动探测，再不行就手动 `LIVEPUSH_SIGN_EXTRA_ARGS="--offset 0x..."`。
- 代价：QQ 升级后 ABI 变化 → 登录报 **Code 45**；要重新找 `wrapper.node` 并重试偏移。

### C. 外接已有协议端（OneBot 11 / Milky）

- 你自己在服务器或另一台机器上跑 LLBot / waylay / NapCat / Lagrange，然后：
  - OneBot 11：在 `/qq` 页选「外接」+「OneBot 11」，填 `ws://<host>:<port>` 和 Token；
  - Milky：选「外接」+「Milky」，填 `http://<host>:<port>` 与 Token。
- 本项目不管理它的进程，但会持续重连、推送、记录错误。

### D. 不接 QQ

- 只做监控与 Web UI 也可以：绑定关系建好但不启用推送目标即可（日志里能看到事件）。

## 决策树

```
有 Docker 吗？
├─ 有 ──> 用托管（NapCat；想用 WebUI 远程桌面则 SnowLuma）
└─ 没有
   ├─ 已有别的协议端在跑 ──> 外接 + OneBot 11（最省事）
   └─ 没有
      ├─ 能维护签名服务（有可用 wrapper.node） ──> 内置 Yogurt / Lagrange.Milky
      └─ 都不能 ──> 先只做监控，QQ 以后再说
```

## 签名与版本对照

自建签名服务依赖「QQ 版本 → `wrapper.node` 里的 call 偏移」。本仓库内置：

| QQ 版本 | 偏移 |
| --- | --- |
| 3.2.18-36497 | `0x59660D0` |
| 3.2.19-39038 | `0x5ADE220` |
| 3.2.27-47354 | `0x56D81D1` |
| 3.2.35-54395 | `0x6856284` |

- 版本号来自 `wrapper.node` 同目录（或 `resources/app/`）的 `package.json` 的 `version`。
- 不在表里时，`sign.py` 用特征码自动探测（`.text` 里搜 `29f24c8d4424`，回溯 `call` 并校验序言）；
  自动探测失败才会退出。
- 仍失败时：

  ```bash
  LIVEPUSH_SIGN_EXTRA_ARGS="--offset 0x6856284"
  # 或指定版本：--version 3.2.19-39038
  ```

## 登录态复用（v2 修正）

旧版本一看到协议端目录里出现 keystore 就判「已登录」，这会在**用过期会话**时假成功。
现在流程是：

1. 检测到 keystore → 状态 `reusing_session`，用 Milky `get_login_info` 真正验一次；
2. 30 秒内验通 → `logged_in`；
3. 验不通 → 回到扫码流程（`launching`），并在页面上提示「复用的登录态验证失败」。

## 常见错误码

| 码 | 含义 | 处理 |
| --- | --- | --- |
| 45 | 签名/版本不匹配（sec-sign 失败） | 换匹配版本的 `wrapper.node`，或改偏移；托管路线不受影响 |
| 237 | 需要滑块/验证码 | 换网络或换号，按协议端提示在手机 QQ 上处理 |
| 100 | 参数错误 | 检查 uin / 配置字段；看协议端日志 |
