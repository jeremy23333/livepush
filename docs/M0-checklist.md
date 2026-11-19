# M0 校准清单（到目标机后第一件事）

代码里所有与**第三方协议端**耦合的部分都集中在两个文件，方便按实际版本校正，不必改业务逻辑：

- `app/livepush/qq/backends/__init__.py` —— 配置模板、启动参数、日志标记、登录态文件名
- `app/livepush/qq/sign_server.py` —— 自建签名的启动方式

下面每一项都对应一个 `# ⚠️ M0` 或注释里的假设。逐条核对，改完跑一遍 `python -m livepush doctor`。

---

## 1. 自建签名服务

```bash
bash scripts/bootstrap.sh --with-sign
ls data/sign/        # 期望看到 sign.py、symbols.c、wrapper.node、libsymbols.so
```

- [ ] `sign.py` 能起来：`python data/sign/sign.py --wrapper data/sign/wrapper.node --host 127.0.0.1 --port 8080`
- [ ] `curl http://127.0.0.1:8080/health` 返回 `{"status":"ok", ...}`
- [ ] `curl http://127.0.0.1:8080/api/sign/appinfo` 返回版本信息
- [ ] 设置页「签名体检」显示**可用**，且版本与协议端一致

**版本匹配**：签名靠 `wrapper.node` 内函数偏移，QQ 客户端升级会失效。已知偏移：

| QQ 版本 | offset |
|---|---|
| 3.2.27-47354 | `0x56D81D1` |
| 3.2.19-39038 | `0x5ADE220` |
| 3.2.18-36497 | `0x59660D0` |

自动探测失败时用 `--offset` 覆盖。`scripts/bootstrap.sh` 里的 `QQ_DEB_URL` 请锁死版本，不要漂。

---

## 2. 协议端（Yogurt / Lagrange.Milky）

把可执行文件放到 `data/protocol/<backend>/`：

```
data/protocol/yogurt/yogurt            # chmod +x
data/protocol/lagrange_milky/Lagrange.Milky
```

- [ ] `BackendSpec.binary_names` 里的候选名与实际文件名一致
- [ ] `BackendSpec.config_name` 与实际读取的配置文件名一致（Yogurt `config.json` / Lagrange `appsettings.json`）
- [ ] `xconfig` 键名与实际版本一致：
  - Yogurt：`protocol.{uin,os,version,signApiUrl,pcLagrangeSignToken}` / `milky.http.{host,port,accessToken}` / `milky.webhook.endpoints`
  - Lagrange：`Lagrange.Login.Uin` / `Lagrange.Protocol.Signer.{BaseUrl,Token}` / `Milky.{AccessToken,HttpServer,Api.Http.Enabled,Event.WebHook.TargetUrls}`
- [ ] `extra_args`：Lagrange.Milky 目前给的是 `shell`，确认这是正确的启动子命令
- [ ] 启动后**二维码落在哪**：
  - 文件？→ 确认文件名在 `QR_FILE_CANDIDATES`（`qrcode.png` / `qr.png` / `cache/qrcode.png` …）
  - 终端打印登录 URL？→ 不需要改，代码会自动渲染成图
  - 只有块字符？→ 页面上会以 `<pre>` 展示，确认能扫
- [ ] `qr_markers` / `success_markers` / `expired_markers`：对照「查看协议端日志」里**实际**出现的字符串改
- [ ] `keystore_globs`：Yogurt `session-store.json`、Lagrange `*.keystore`，确认登录后确实生成
- [ ] 重启服务后是否免扫码直接上线（`_resume_builtin_bots` 依赖登录态文件）

---

## 3. Milky 接口对照

- [ ] `send_group_message` / `send_private_message` 的字段名（`group_id` / `user_id`）与实际一致
- [ ] `message` 段格式：文本 `{"type":"text","data":{"text":...}}`、图片 `{"type":"image","data":{"uri":"base64://..."}}`、@全体 `{"type":"mention_all","data":{}}`
- [ ] `get_login_info` 返回的账号字段是 `user_id` 还是 `uin`（代码两者都兼容）
- [ ] `get_group_list` 返回结构（数组 / `{list:[...]}`，代码都兼容）
- [ ] 事件是否真的 POST 到 `http://127.0.0.1:<port>/webhook/milky`（WebHook 已写进配置）

## 4. OneBot 11（外接免签路线）

- [ ] WS 地址与 Token 正确，`get_login_info` 能返回
- [ ] `send_group_msg` / `send_private_msg` 的 `message` 数组能被协议端接受
- [ ] `get_group_at_all_remain` 是否存在（不存在时 @全体 会静默降级）
- [ ] 群不存在时返回的 `retcode` / `message` 能命中 `_FATAL_HINTS`，从而自动禁用目标

---

## 5. B 站侧

- [ ] 单个房间实时性符合预期（默认 30s/90s/夜间 120s 自适应）
- [ ] 房间数上去后没有触发 `-352` / `412`（看总览页的红色横幅）
- [ ] 如果服务器在云上，考虑配 `bili_proxy` 与 `SESSDATA`
- [ ] 轮播（`live_status=2`）按离线处理是否符合你的预期

---

## 6. 真实推送验收

1. `/qq` 扫码 → 状态变「已连接」
2. `/targets` 导入群 → 选一个测试群
3. `/bindings` 绑定一个**正在直播**的主播，勾选「@全体」「带封面」
4. `/rooms` 添加时勾选「正在直播则立即推送」
5. 期望：目标群收到「昵称 开播啦！/ 分区 / 标题 / 封面图 / 链接」，`/logs` 里该任务状态为「已发送」
6. 再发一条测试消息（`/targets` 的「测试」按钮）确认图片不是裂的
