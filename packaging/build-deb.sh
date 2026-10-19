#!/bin/bash
# 打 deb 包：./packaging/build-deb.sh [输出目录]
# 版本号取自 docseek/__init__.py 的 __version__，不用手改。
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$HOME/下载"}
VERSION=$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import docseek; print(docseek.__version__)")
PKG=docseek_${VERSION}_all
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/docseek-deb-XXXXXX")
# 别在 /tmp 里留打包残留；本机纪律：删除一律 `gio trash`，不做物理删除。
clean_stage() {
    if command -v gio >/dev/null 2>&1; then
        gio trash -- "$STAGE" 2>/dev/null || true
    fi
}
trap clean_stage EXIT

mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/docseek" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/doc/docseek"

# 只装运行需要的文件。索引库在 ~/.local/share/docseek 下，天然不会被打进来。
tar -C "$ROOT" --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='PROGRESS.md' --exclude='tests' --exclude='packaging' \
    -cf - bin docseek README.md docseek.svg docseek.desktop \
    install.sh uninstall.sh \
  | tar -C "$STAGE/opt/docseek" -xf -

ln -s /opt/docseek/bin/docseek "$STAGE/usr/bin/docseek"
cp "$ROOT/docseek.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/docseek.svg"
# 桌面项不写死路径（避免换台机器就失效）
sed -e 's|^Exec=.*|Exec=docseek gui|' "$ROOT/docseek.desktop" \
  > "$STAGE/usr/share/applications/docseek.desktop"
cp "$ROOT/README.md" "$STAGE/usr/share/doc/docseek/README.md"

# 权限归一（踩过的坑）：tar 会照抄源码树里的权限位，源码里若有 600 的文件，
# 装到别人机器上就是「图标空白 + 点了没反应」——普通用户读不到 .py 文件，
# import 直接被 EACCES 拦下；svg 读不到就是空白图标。这里统一收口。
chmod 755 "$STAGE"            # mktemp -d 造出来是 700，包根目录也得让人进得去
chmod -R a+rX "$STAGE/opt" "$STAGE/usr/share"
chmod 755 "$STAGE/opt/docseek/bin/docseek" \
          "$STAGE/opt/docseek/install.sh" \
          "$STAGE/opt/docseek/uninstall.sh"
chmod 644 "$STAGE/usr/share/applications/docseek.desktop" \
          "$STAGE/usr/share/icons/hicolor/scalable/apps/docseek.svg"

# 打包前断言：任何普通用户读不到的东西都不许进包
bad=$(find "$STAGE" \( -type f -o -type d \) ! -perm -o=r -print)
if [ -n "$bad" ]; then
    echo "打包失败：以下文件普通用户读不到（换台机器会表现为图标空白/启动失败）——" >&2
    printf '  %s\n' "$bad" >&2
    exit 1
fi
dirbad=$(find "$STAGE" -type d ! -perm -o=x -print)
if [ -n "$dirbad" ]; then
    echo "打包失败：以下目录普通用户进不去——" >&2
    printf '  %s\n' "$dirbad" >&2
    exit 1
fi
test -x "$STAGE/opt/docseek/bin/docseek" || { echo "打包失败：启动器不可执行" >&2; exit 1; }
test -r "$STAGE/usr/share/icons/hicolor/scalable/apps/docseek.svg" \
    || { echo "打包失败：菜单图标缺失或不可读" >&2; exit 1; }

cat > "$STAGE/DEBIAN/control" <<EOF
Package: docseek
Version: ${VERSION}-1
Section: utils
Priority: optional
Architecture: all
Maintainer: dx41 <dx41@users.noreply.github.com>
Depends: python3 (>= 3.6), python3-gi, gir1.2-gtk-3.0, poppler-utils
Recommends: tesseract-ocr, tesseract-ocr-chi-sim, tesseract-ocr-chi-tra, tesseract-ocr-osd, python3-pil, python3-xlrd, gir1.2-gtksource-3.0, catdoc, libwps-tools, libreoffice-core | libreoffice-writer
Installed-Size: 1024
Description: DocSeek -- local full-text search with match snippets and OCR
 Search inside documents (docx/xlsx/pptx/pdf/txt/code, plus legacy
 doc/xls/wps via LibreOffice/catdoc), Chinese bigram index on SQLite FTS5,
 inline match snippets, full-text highlight and page rendering preview.
 Scanned PDFs are made searchable with tesseract OCR.
EOF

# postinst：装完顺手体检，缺哪些可选依赖就把安装命令打出来
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

if [ -x /usr/bin/update-desktop-database ]; then
    update-desktop-database -q /usr/share/applications || true
fi
if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

# 防御性收口：万一本包里有文件的权限位不对，普通用户看到的就是「图标空白 + 点了没反应」
chmod -R a+rX /opt/docseek 2>/dev/null || true
chmod a+r /usr/share/icons/hicolor/scalable/apps/docseek.svg 2>/dev/null || true
python3 -c "import sys; sys.path.insert(0, '/opt/docseek'); import docseek.ui.results" >/dev/null 2>&1 \
    || echo "提示：DocSeek 模块自检失败，请到终端运行 docseek doctor 看原因。"

# 缺的可选依赖：rp 装包只会装 Depends，Recommends 不一定跟着来
missing=""
command -v pdftotext >/dev/null 2>&1 || missing="$missing poppler-utils"
command -v tesseract  >/dev/null 2>&1 || missing="$missing tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-osd"
command -v soffice    >/dev/null 2>&1 || missing="$missing libreoffice-core"
command -v catdoc     >/dev/null 2>&1 || missing="$missing catdoc"
command -v wps2text   >/dev/null 2>&1 || missing="$missing libwps-tools"
python3 -c "import PIL"   >/dev/null 2>&1 || missing="$missing python3-pil"
python3 -c "import xlrd"  >/dev/null 2>&1 || missing="$missing python3-xlrd"

cat <<'MSG'

DocSeek 安装完成：
  · 命令        docseek gui | docseek index ~/文档 | docseek search 关键词
  · 应用菜单    搜 "DocSeek"
  · 环境自检    docseek doctor      （缺什么依赖、要敲哪条命令，一条命令看清）
  · 首次使用    在「索引设置」里添加要索引的目录，再点左上角 ⟳
  · 本包不含任何索引数据；索引与缓存在 ~/.local/share/docseek 与 ~/.cache/docseek 下自行生成
MSG

if [ -n "$missing" ]; then
    echo ""
    echo "提示：以下可选项没装，缺了对应格式就抽不出正文——"
    echo "    sudo apt install -y$missing"
    echo "装完可用 docseek doctor 复查。"
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

chmod 755 "$STAGE"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/${PKG}.deb" >/dev/null
echo "已生成：$OUT/${PKG}.deb"
