#!/bin/bash
# 跑测试。两条纪律，都是踩过坑换来的：
#   1) DOCSEEK_NO_LAUNCH=1 —— 任何用例都不许在真实桌面上弹窗口/文件夹。
#      两条出路 xvfb 都拦不住：`Gio.AppInfo.launch_default_for_uri` 和
#      `org.freedesktop.FileManager1.ShowItems` 都是走**会话总线**请桌面环境打开的，
#      后者还会 D-Bus 激活文件管理器（本机是 peony），于是桌面上冒出一堆文件夹窗口、
#      测试跑完也没人关。所以代码里要有闸门，测试里要替换掉这两条出路
#      （tests/test_ui.py 的 setUpModule 同时 patch `_launch_uri` 与 `_reveal_in_manager`）。
#   2) TMPDIR 指向本次运行专属目录、跑完直接删除 —— 测试产物不留在 /tmp，
#      也不塞进用户的回收站（那是用户的地方，不是垃圾场）。
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
    # 本次运行的 TMPDIR 是本脚本自己 mktemp 出来的产物，直接删干净；只有"用户文件"
    # 才必须进回收站（以前一律 gio trash，跑一趟测试就往回收站塞一个 docseek-tests-*）。
    case "$TMPDIR" in
        */docseek-tests-*) rm -rf -- "$TMPDIR" ;;
        *) echo "注意：临时目录不在白名单，保留在 $TMPDIR" ;;
    esac
}
trap report_and_clean EXIT

cd "$ROOT" || exit 1
if [ $# -gt 0 ]; then
    xvfb-run -a python3 -m unittest "$@"
else
    xvfb-run -a python3 -m unittest discover -s tests
fi
