#!/bin/sh
# 卸载 DocSeek 的用户级安装（源码目录不动）。
# 按项目约定，删除一律进回收站（gio trash），不做物理删除。
set -e

APP_DIR="$HOME/.local/share/applications"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/docseek.svg"
CMD="$HOME/.local/bin/docseek"

for target in "$APP_DIR/docseek.desktop" "$ICON" "$CMD"; do
    if [ -e "$target" ]; then
        gio trash "$target" && echo "已进回收站：$target"
    fi
done

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true

echo
echo "已卸载。索引库与配置仍在（如需一并移除请手动处理）："
echo "  ~/.config/docseek/         配置"
echo "  ~/.local/share/docseek/    索引库"
echo "  ~/.cache/docseek/          渲染缓存"
