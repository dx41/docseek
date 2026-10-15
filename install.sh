#!/bin/sh
# DocSeek 安装脚本（装到当前用户，不需要 sudo）
#
#   ~/.local/bin/docseek                                     启动命令（已在 PATH 里）
#   ~/.local/share/applications/docseek.desktop              应用菜单项
#   ~/.local/share/icons/hicolor/scalable/apps/docseek.svg   图标
#
# 源码留在原地（默认 ~/docseek），改代码后无需重装。
set -e

ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

# 1) 启动命令：转发到项目里的 bin/docseek（带 PYTHONPATH，支持从任意目录调用）
cat > "$BIN_DIR/docseek" <<EOF
#!/bin/sh
exec "$ROOT/bin/docseek" "\$@"
EOF
chmod 755 "$BIN_DIR/docseek"

# 2) 图标
cp "$ROOT/docseek.svg" "$ICON_DIR/docseek.svg"
chmod 644 "$ICON_DIR/docseek.svg"

# 3) 桌面项
sed -e "s|^Exec=.*|Exec=$BIN_DIR/docseek gui|" \
    -e "s|^Icon=.*|Icon=docseek|" \
    "$ROOT/docseek.desktop" > "$APP_DIR/docseek.desktop"
chmod 644 "$APP_DIR/docseek.desktop"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$APP_DIR/docseek.desktop" && echo "desktop 文件校验通过"
fi
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "安装完成："
echo "  命令      $BIN_DIR/docseek"
echo "  菜单项    $APP_DIR/docseek.desktop"
echo "  图标      $ICON_DIR/docseek.svg"
echo "  源码      $ROOT"
echo
echo "用法：  docseek gui        # 图形界面"
echo "        docseek index ~/文档   # 命令行建索引"
echo "        docseek search 关键词  # 命令行搜索"
