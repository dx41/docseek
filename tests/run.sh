#!/bin/bash
# 跑测试。两条纪律，都是踩过坑换来的：
#   1) DOCSEEK_NO_LAUNCH=1 —— 任何用例都不许在真实桌面上弹窗口/文件夹。
#      两条出路 xvfb 都拦不住：`Gio.AppInfo.launch_default_for_uri` 和
#      `org.freedesktop.FileManager1.ShowItems` 都是走**会话总线**请桌面环境打开的，
#      后者还会 D-Bus 激活文件管理器（本机是 peony），于是桌面上冒出一堆文件夹窗口、
#      测试跑完也没人关。所以代码里要有闸门，测试里要替换掉这两条出路
#      （tests/test_ui.py 的 setUpModule 同时 patch `_launch_uri` 与 `_reveal_in_manager`）。
#   2) TMPDIR 指向本次运行专属目录、跑完整体送回收站 —— 测试产物不留在 /tmp，
#      也不做物理删除（本机纪律：删除一律 `gio trash`）。
#
# 用法：
#   tests/run.sh                       # 全量
#   tests/run.sh tests.test_ui         # 指定模块
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export DOCSEEK_NO_LAUNCH=1
export TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/docseek-tests-XXXXXX")

report_and_clean() {
    local leftovers
    leftovers=$(find "$TMPDIR" -mindepth 1 2>/dev/null | head -8)
    if [ -n "$leftovers" ]; then
        echo ""
        echo "注意：有用例留下了未清理的临时文件（都在本次运行的 TMPDIR 里）："
        echo "$leftovers"
    fi
    # 本机纪律：删除一律进回收站，不做物理删除（AGENTS.md）。没装 gio 就留在原地。
    if command -v gio >/dev/null 2>&1; then
        gio trash -- "$TMPDIR" 2>/dev/null || true
    else
        echo "注意：本机没有 gio，临时目录保留在 $TMPDIR（未做物理删除）"
    fi
}
trap report_and_clean EXIT

cd "$ROOT" || exit 1
if [ $# -gt 0 ]; then
    xvfb-run -a python3 -m unittest "$@"
else
    xvfb-run -a python3 -m unittest discover -s tests
fi
