#!/bin/sh
# 登录后执行：创建仓库（若不存在）并推送干净的 master 分支。
# 用法： ./push_to_github.sh [public|private]     默认 public
# 可用环境变量覆盖：GH=<gh 可执行文件> REPO=<owner/name>
set -e

REPO=${REPO:-dx41/docseek}
BRANCH=${BRANCH:-master}
VIS=${1:-public}

# gh 不一定在 PATH 里（本机装在 ~/.local/bin），两处都找一下。
if [ -n "${GH:-}" ]; then
    :
elif command -v gh >/dev/null 2>&1; then
    GH=$(command -v gh)
elif [ -x "$HOME/.local/bin/gh" ]; then
    GH=$HOME/.local/bin/gh
else
    echo "找不到 gh 命令行，请先安装 GitHub CLI 或设置 GH=<路径>"
    exit 1
fi

if ! "$GH" auth status >/dev/null 2>&1; then
    echo "还没登录，请先运行："
    echo "    $GH auth login"
    exit 1
fi

echo "当前账号：$("$GH" api user --jq .login)"
if "$GH" repo view "$REPO" >/dev/null 2>&1; then
    echo "仓库 $REPO 已存在，直接推送"
else
    echo "创建仓库 $REPO（$VIS）"
    "$GH" repo create "$REPO" "--$VIS" \
        --description "DocSeek：本地文档内容搜索（GTK3 + SQLite FTS5 + 中文 bigram + OCR）"
fi

cd "$(dirname "$0")"
# 只推 master（干净的公开历史）；local-history 是本地开发记录，不外传
git push -u origin "$BRANCH"
echo
echo "完成：https://github.com/$REPO"
