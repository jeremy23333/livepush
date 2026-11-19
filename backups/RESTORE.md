# 备份与恢复说明

## 这里有什么

- `livepush-1.0.0-<时间戳>.tar.gz`：v1.0.0 的**源码快照**，包含
  `app/ tests/ tools/ docs/ deploy/ scripts/ pyproject.toml README.md .env.example .gitignore`。
  不含 `.venv/`、`.tmp/`、`data/`、`__pycache__/`、`backups/` 以及 pip/pytest 临时目录。
- `SHA256SUMS`：上面的包与其 SHA256、字节数（一行一个）。

## 恢复源码

```bash
sha256sum -c SHA256SUMS
mkdir -p /home/alan/program/livepush
tar -xzf livepush-1.0.0-<时间戳>.tar.gz -C /home/alan/program/livepush
cd /home/alan/program/livepush
python3 -m venv .venv
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -e .          # 国内可加 -i https://mirrors.aliyun.com/pypi/simple/
.venv/bin/python -m livepush init
```

## 运行期数据不在包里（重要）

`data/` 目录含数据库、封面缓存、日志、协议端工作目录，以及两份**密钥**：
`data/admin-password.txt`、`data/secret.key`。恢复时必须单独备份/还原，否则：
- 没有 `secret.key` → 旧登录 Cookie 全部失效（首次启动会自动生成新的，不影响功能）；
- 没有 `admin-password.txt` → 重新 `init` 会生成新口令，旧页面登录失效。

备份（在服务器上）：

```bash
tar -czf livepush-data-$(date +%F).tar.gz -C /home/alan/program/livepush data
# 只会含 admin-password.txt / secret.key / livepush.db / covers / logs / protocol / sign
```

恢复：

```bash
tar -xzf livepush-data-<日期>.tar.gz -C /home/alan/program/livepush
```

## 校验

```bash
sha256sum -c SHA256SUMS        # Linux
```

Windows PowerShell：

```powershell
Get-FileHash backups\livepush-1.0.0-*.tar.gz -Algorithm SHA256
```
