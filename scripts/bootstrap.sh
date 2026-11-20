#!/usr/bin/env bash
# livepush 一次性环境准备（Ubuntu / Debian，x86_64）
#
# 做四件事：
#   1. 检查 Python 版本与系统依赖
#   2. 创建虚拟环境并安装本项目的 Python 依赖
#   3. 可选：安装「自建签名服务」（ntqq-sign-server + 官方 QQ 的 wrapper.node）
#   4. 可选：下载内置协议端（Yogurt / Lagrange.Milky）到 data/protocol/
#
# 用法：
#   bash scripts/bootstrap.sh                # 只装 Python 环境
#   bash scripts/bootstrap.sh --with-sign    # 额外装签名服务
#   bash scripts/bootstrap.sh --with-yogurt  # 额外下载 Yogurt
#
# 设计说明：签名服务与协议端都是第三方程序，本项目不重分发它们的二进制，
# 需要时在运行时从官方地址获取，放在 data/ 下。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${LIVEPUSH_DATA_DIR:-$ROOT/data}"
SIGN_DIR="$DATA/sign"
PROTO_DIR="$DATA/protocol"

# wrapper.node 只能从官方 QQ Linux 安装包里取。版本会变、旧地址会被上游删除，
# 所以默认在运行时向官方接口解析「当前」地址；解析失败才用兜底常量。
# 要钉死版本：QQ_DEB_URL=https://... bash scripts/bootstrap.sh --with-sign
QQ_DEB_URL="${QQ_DEB_URL:-}"
QQ_DEB_API="${QQ_DEB_API:-https://im.qq.com/rainbow/linuxQQDownload}"
QQ_DEB_FALLBACK="${QQ_DEB_FALLBACK:-https://dldir1v6.qq.com/qqfile/qq/QQNT/Linux/QQ_3.2.18_250626_amd64_01.deb}"
# 2026-11 实测可用：qqdl.gtimg.cn 上的 3.2.35 beta 构建（官方 API 目前返回的是失效地址）。
# 这是「按构建号」的路径，将来可能失效；脚本下载前会探活，失效就跳过。
QQ_DEB_EXTRA="${QQ_DEB_EXTRA:-https://qqdl.gtimg.cn/qqfile/QQNT/9.9.37/beta/816b3c79/linuxqq_3.2.35-54395_amd64.deb}"
SIGN_REPO="${SIGN_REPO:-https://github.com/Micuks/ntqq-sign-server}"

ARCH="$(uname -m)"
echo "==> 架构：$ARCH  数据目录：$DATA"

if [[ "$ARCH" != "x86_64" ]]; then
  echo "!! 自建签名服务只支持 x86_64。当前是 $ARCH，请改用免签路线（外接 LLBot / waylay 的 OneBot11）。"
fi

WITH_SIGN=0
WITH_YOGURT=0
for arg in "$@"; do
  case "$arg" in
    --with-sign) WITH_SIGN=1 ;;
    --with-yogurt) WITH_YOGURT=1 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- 1. 系统依赖
if command -v apt-get >/dev/null 2>&1; then
  echo "==> 安装系统依赖（需要 sudo）"
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates
fi

# ---------------------------------------------------------------- 2. Python
echo "==> 创建虚拟环境"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --upgrade pip -q
echo "==> 安装项目依赖"
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT" -q
echo "    Python 环境就绪：$ROOT/.venv/bin/python"
echo "    自检：$ROOT/.venv/bin/python -m livepush doctor"

mkdir -p "$DATA" "$DATA/logs" "$DATA/covers" "$SIGN_DIR" "$PROTO_DIR"

# ---------------------------------------------------------------- 3. 自建签名
if [[ "$WITH_SIGN" == "1" && "$ARCH" != "x86_64" ]]; then
  echo "!! 跳过自建签名：它只支持 x86_64（当前 $ARCH）。改用免签路线（外接 OneBot11）。"
  WITH_SIGN=0
fi

if [[ "$WITH_SIGN" == "1" ]]; then
  echo "==> 安装自建签名服务依赖"
  sudo apt-get install -y --no-install-recommends \
    build-essential python3-dev \
    libgnutls30 libglib2.0-0 libnss3 libatk1.0-0 libcups2 \
    libdrm2 libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 libvips42

  TMP_SIGN="$(mktemp -d)"
  trap 'rm -rf "$TMP_SIGN"' EXIT

  echo "==> 拉取 ntqq-sign-server"
  # 注意：上游默认分支是 master
  git clone --depth 1 --branch master "$SIGN_REPO" "$TMP_SIGN/src"
  cp "$TMP_SIGN/src/sign.py" "$SIGN_DIR/sign.py"
  cp "$TMP_SIGN/src/symbols.c" "$SIGN_DIR/symbols.c"

  # 解析当前可用的 deb 地址：官方接口 → 手动指定 → 兜底常量
  echo "==> 解析 QQ Linux 当前下载地址"
  if [[ -n "$QQ_DEB_URL" ]]; then
    PICKED="$QQ_DEB_URL"
    echo "    使用手动指定：$PICKED"
  else
    PICKED="$(curl -fsSL --max-time 20 "$QQ_DEB_API" 2>/dev/null \
      | grep -oE 'https://[^"]+_amd64_01\.deb' | head -n1 || true)"
    if [[ -n "$PICKED" ]]; then
      echo "    官方接口给出：$PICKED"
    else
      PICKED="$QQ_DEB_FALLBACK"
      echo "!! 官方接口没解析出地址，改用兜底：$PICKED"
    fi
  fi

  probe_url() { curl -fsSL --range 0-0 --max-time 20 -o /dev/null "$1" 2>/dev/null; }

  OK_URL=""
  for cand in "$PICKED" "$QQ_DEB_FALLBACK" "$QQ_DEB_EXTRA"; do
    [[ -n "$OK_URL" && "$cand" == "$OK_URL" ]] && continue
    if probe_url "$cand"; then
      OK_URL="$cand"
      break
    fi
    echo "!! 不可达：$cand"
  done

  if [[ -z "$OK_URL" ]]; then
    cat <<'EOM'
!! 下载不到 QQ Linux 安装包，因此拿不到 wrapper.node。

这不会影响主程序：受影响的只是「内置 Milky（免签签名服务）」这一条路线。

（2026-11 实测：官方接口下发的地址及同目录的 rpm/AppImage/arm64 全部 404，
  而同一 CDN 上的微信 Linux deb 正常 200 —— 属上游把目录下线了，官网页面同样不可用。）

三条出路，任选其一：

  A) 找到当前有效的 deb 地址后手动指定重跑：
       QQ_DEB_URL="https://.../QQ_xxx_amd64_01.deb" bash scripts/bootstrap.sh --with-sign
     （在服务器上先 curl -fsSI 该地址确认返回 200）

  B) 把 deb 拷到服务器后跳过下载：脚本支持手动放置 wrapper.node ——
     解包 `opt/QQ/resources/app/wrapper.node` 到 data/sign/wrapper.node，
     再把 sign.py 与 symbols.c 放到 data/sign/，最后 gcc 编译 libsymbols.so。

  C) 先不做内置模式：改用外接 OneBot11（LLBot / waylay-qq-bridge），
     外接路线不需要签名服务。见 docs/DEPLOY-ubuntu.md 第 7 节。

判定网络问题的命令：
  curl -fsSI "<url>" | head -3      # 404 = 地址失效；超时 = 网络/代理问题
EOM
    exit 3
  fi

  echo "==> 下载 QQ Linux 并提取 wrapper.node（约 150MB，可能较慢）"
  curl -fsSL --retry 2 --connect-timeout 20 -o "$TMP_SIGN/qq.deb" "$OK_URL"
  ls -lh "$TMP_SIGN/qq.deb"
  dpkg -x "$TMP_SIGN/qq.deb" "$TMP_SIGN/qq"
  APP="$TMP_SIGN/qq/opt/QQ/resources/app"
  if [[ ! -f "$APP/wrapper.node" ]]; then
    echo "!! 解包后没找到 opt/QQ/resources/app/wrapper.node，包结构可能变了："
    find "$TMP_SIGN/qq" -maxdepth 9 -name 'wrapper.node' 2>/dev/null | head -n 5 || true
    exit 4
  fi
  cp "$APP/wrapper.node" "$SIGN_DIR/"
  for extra in package.json libvips-cpp.so.42 libbugly.so libcrbase.so; do
    [[ -f "$APP/$extra" ]] && cp "$APP/$extra" "$SIGN_DIR/" || true
  done

  echo "==> 编译符号桩"
  ( cd "$SIGN_DIR" && gcc -std=c99 -shared -fPIC -o libsymbols.so symbols.c )

  echo "    签名服务文件已就位：$SIGN_DIR"
  ls -lh "$SIGN_DIR/wrapper.node"
  echo "    启动方式二选一："
  echo "      A) 打开 livepush 设置页 →「启动自建签名」"
  echo "      B) 手动：$ROOT/.venv/bin/python $SIGN_DIR/sign.py --wrapper $SIGN_DIR/wrapper.node --host 127.0.0.1 --port 8080"
fi


# ---------------------------------------------------------------- 4. 协议端
if [[ "$WITH_YOGURT" == "1" ]]; then
  echo "==> Yogurt 需要手动从上游发布页下载（本项目不重分发）"
  echo "    下载后放到：$PROTO_DIR/yogurt/yogurt（记得 chmod +x）"
  echo "    上游：https://github.com/LagrangeDev/acidify/releases"
  mkdir -p "$PROTO_DIR/yogurt"
fi

echo
echo "完成。下一步："
echo "  1) $ROOT/.venv/bin/python -m livepush doctor"
echo "  2) $ROOT/.venv/bin/python -m livepush run"
echo "  3) 浏览器打开 http://127.0.0.1:8787 ，口令在 $DATA/admin-password.txt"
