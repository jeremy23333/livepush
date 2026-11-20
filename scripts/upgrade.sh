#!/usr/bin/env bash
# livepush 服务器端升级脚本（在仓库目录里的任意位置执行都可以，脚本自己 cd 到仓库根）
#
#   bash scripts/upgrade.sh                  # 拉取 main 最新 → 装依赖 → init → doctor → 重启服务
#   bash scripts/upgrade.sh --tag v2.1.0     # 切到某个发布标签（可用于回滚）
#   bash scripts/upgrade.sh --no-install     # 只拉代码，不动依赖
#   bash scripts/upgrade.sh --no-restart     # 不重启 systemd（先手工验证）
#   bash scripts/upgrade.sh --force-restart  # doctor 有失败项也照样重启
#
# 约定：
#   * 工作区必须干净（有未提交改动会直接退出，脚本不会替你丢弃改动）
#   * 数据全在 data/（被 .gitignore 忽略），pull/checkout 不会碰它
#   * 服务名默认 livepush@<当前用户>，可用 LIVEPUSH_SERVICE 覆盖
set -euo pipefail

SERVICE="${LIVEPUSH_SERVICE:-livepush@$(id -un)}"
TAG=""
DO_INSTALL=1
DO_RESTART=1
FORCE_RESTART=0

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)           TAG="$2"; shift 2 ;;
        --no-install)    DO_INSTALL=0; shift ;;
        --no-restart)    DO_RESTART=0; shift ;;
        --force-restart) FORCE_RESTART=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"

[[ -d "$ROOT/.git" ]] || { echo "这里不是 git 仓库：$ROOT" >&2; exit 1; }
[[ -x "$PY" ]]       || { echo "找不到 $PY，先按 docs/DEPLOY-ubuntu.md 第 2 步建 venv" >&2; exit 1; }

if [[ -n "$(git status --porcelain)" ]]; then
    echo "工作区有未提交的改动，先处理掉再升级：" >&2
    git status --short >&2
    exit 1
fi

BEFORE="$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)"
echo "== 拉取远端 =="
git fetch --all --tags --prune

if [[ -n "$TAG" ]]; then
    git -c advice.detachedHead=false checkout --force "$TAG"
    TARGET="$TAG"
else
    git checkout --force main
    git merge --ff-only '@{upstream}'
    TARGET="$(git rev-parse --short HEAD)"
fi

echo "== 版本：$BEFORE → $TARGET =="
git --no-pager log --oneline "$BEFORE..HEAD" 2>/dev/null | head -n 20 || true

if [[ "$DO_INSTALL" -eq 1 ]]; then
    echo "== 安装依赖（可编辑安装） =="
    "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    "$PY" -m pip install -q -e "$ROOT"
fi

echo "== 初始化（幂等，不会覆盖已有口令/密钥） =="
"$PY" -m livepush init

echo "== 自检 =="
DOCTOR_RC=0
"$PY" -m livepush doctor || DOCTOR_RC=$?

if [[ "$DO_RESTART" -eq 1 ]]; then
    if [[ "$DOCTOR_RC" -ne 0 && "$FORCE_RESTART" -ne 1 ]]; then
        echo "!! doctor 有失败项，已跳过重启（确认无碍可加 --force-restart）" >&2
        exit 3
    fi
    if command -v systemctl >/dev/null 2>&1; then
        echo "== 重启 $SERVICE =="
        sudo systemctl restart "$SERVICE"
        sleep 1
        systemctl --no-pager --lines=0 status "$SERVICE" || true
        echo "看日志：journalctl -u $SERVICE -n 100 --no-pager"
    else
        echo "没找到 systemctl，请手工重启进程" >&2
    fi
fi

echo "== 完成：$TARGET =="
