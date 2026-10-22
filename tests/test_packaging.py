"""打包不变量：装到别人机器上必须能读、能跑、图标有图。

背景（真实事故）：源码树里 docseek/ui/results.py、docseek/doctor.py、docseek.svg
一度是 600，tar 照抄权限打进 deb，装到另一台电脑后的表现是
「图标空白 + 点了没反应」——普通用户根本读不到 .py 文件，import 被 EACCES 拦下。
这几个用例盯住的就是这条不变量。
"""

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch import discard as _discard_scratch   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _walk():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            yield os.path.join(base, name)


class SourcePermissionsTest(unittest.TestCase):
    def test_every_file_is_readable_by_other_users(self):
        bad = []
        for path in _walk():
            mode = os.stat(path).st_mode
            if not mode & 0o004:                     # 没有 o+r
                bad.append("%s (%o)" % (os.path.relpath(path, ROOT), mode & 0o777))
        self.assertEqual(bad, [],
                         "这些文件普通用户读不到，打进 deb 会「点了没反应」：\n  "
                         + "\n  ".join(bad))

    def test_directories_are_traversable(self):
        bad = []
        for base, dirs, _files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for d in dirs:
                path = os.path.join(base, d)
                if not os.stat(path).st_mode & 0o001:     # 没有 o+x
                    bad.append(os.path.relpath(path, ROOT))
        self.assertEqual(bad, [], "这些目录普通用户进不去：%s" % bad)

    def test_bin_launcher_is_executable(self):
        for name in ("docseek",):
            path = os.path.join(ROOT, "bin", name)
            self.assertTrue(os.access(path, os.X_OK), "%s 必须可执行" % path)


class BuildScriptTest(unittest.TestCase):
    def setUp(self):
        self.script = os.path.join(ROOT, "packaging", "build-deb.sh")
        with open(self.script, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_script_normalises_permissions_in_the_stage(self):
        self.assertIn("chmod -R a+rX", self.text,
                      "打包时要把权限归一，否则源码里的 600 会被照抄进包")

    def test_script_refuses_to_build_unreadable_stage(self):
        self.assertRegex(self.text, r"-perm -o=r",
                         "打包前必须断言：普通用户读不到的文件不许进包")

    def test_script_builds_launcher_and_icon_expectations(self):
        self.assertIn("usr/share/icons/hicolor/scalable/apps/docseek.svg", self.text)
        self.assertIn("bin/docseek", self.text)

    def test_postinst_repairs_old_permissions(self):
        # 老机器上已经装了 600 权限的旧包时，重装能顺带修好
        self.assertRegex(self.text, r"chmod -R a\+rX /opt/docseek")

    def test_installed_size_is_computed_not_hardcoded(self):
        # dpkg 拿它判断磁盘够不够；写死一个错数字是在骗包管理器
        self.assertIn("INSTALLED_KB=$(du -sk", self.text)
        self.assertIn("Installed-Size: ${INSTALLED_KB}", self.text)

    def test_depends_matches_dataclasses_requirement(self):
        # 代码用了 dataclasses，那是 3.7 才有的；声明 3.6 会在老机器上直接 import 失败
        self.assertIn("python3 (>= 3.7)", self.text)
        self.assertNotIn("python3 (>= 3.6)", self.text)

    def test_icons_include_pixmaps_and_bitmaps(self):
        self.assertIn("usr/share/pixmaps/docseek.svg", self.text,
                      "老式菜单只从 pixmaps 找图标")
        self.assertIn("GdkPixbuf", self.text, "有渲染能力时要生成 PNG 图标")


class DesktopEntryTest(unittest.TestCase):
    """菜单项写不对，用户看到的就是"图标空白 + 找不到程序"。"""

    def setUp(self):
        self.path = os.path.join(ROOT, "docseek.desktop")
        with open(self.path, encoding="utf-8") as fh:
            self.text = fh.read()
        self.keys = {}
        for line in self.text.splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                self.keys[key.strip()] = value.strip()

    def test_categories_are_valid(self):
        cats = self.keys.get("Categories", "")
        self.assertIn("Office;", cats)
        self.assertNotIn("GTK", cats, "GTK 不是合法分类，菜单不会按它归类")

    def test_startup_wmclass_matches_program_name(self):
        # 任务栏靠 WM_CLASS 找图标；cli.py 里设置的就是 docseek
        self.assertEqual(self.keys.get("StartupWMClass"), "docseek")

    def test_exec_and_icon_present(self):
        self.assertEqual(self.keys.get("Exec"), "docseek gui")
        self.assertEqual(self.keys.get("Terminal"), "false")
        # 图标要写包内真实存在的绝对路径：写成主题名 Icon=docseek 时，若包是在
        # 登录之后才装的，桌面（peony-qt）的图标索引已经缓存过，"添加到桌面快捷
        # 方式"会得到一个空白图标，直到注销重进。绝对路径由 QIcon/Gtk 直接按文
        # 件加载，绕开主题缓存。
        icon = self.keys.get("Icon", "")
        self.assertTrue(icon.startswith("/"), "Icon 应为绝对路径，实际 %r" % icon)
        self.assertIn("/usr/share/icons/hicolor/", icon)
        self.assertTrue(icon.endswith("docseek.png"), icon)

    def test_desktop_file_validates(self):
        if not any(os.access(os.path.join(d, "desktop-file-validate"), os.X_OK)
                   for d in os.environ.get("PATH", "/usr/bin").split(os.pathsep)):
            self.skipTest("没有 desktop-file-validate")
        result = subprocess.run(["desktop-file-validate", self.path],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class BuiltPackageTest(unittest.TestCase):
    """有现成 deb 就顺手体检一下；没有就跳过（CI/新克隆时允许）。"""

    def _current_version(self):
        with open(os.path.join(ROOT, "docseek", "__init__.py"),
                  encoding="utf-8") as fh:
            return re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)

    def _latest_deb(self):
        """只挑当前版本构建出来的包；拿旧包体检等于自欺欺人。"""
        want = "docseek_%s_all.deb" % self._current_version()
        for folder in (os.path.join(os.path.expanduser("~"), "下载"),
                       os.path.join(os.path.expanduser("~"), "Downloads")):
            path = os.path.join(folder, want)
            if os.path.isfile(path):
                return path
        return None

    def test_package_files_are_readable(self):
        deb = self._latest_deb()
        if not deb or not subprocess.call(["which", "dpkg-deb"],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL) == 0:
            self.skipTest("没有现成的 deb 或没有 dpkg-deb")
        out = subprocess.check_output(["dpkg-deb", "-c", deb], text=True)
        bad = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 6 or not parts[0].startswith(("-", "d")):
                continue
            mode = parts[0]
            if len(mode) < 10 or mode[7] != "r":   # 第 8 位是 other-read
                bad.append("%s  %s" % (mode, parts[5]))
        self.assertEqual(bad, [], "包里这些文件普通用户读不到（%s）：\n%s"
                         % (deb, "\n".join(bad)))

    def _extract(self, deb):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="docseek-debtest-")
        subprocess.check_call(["dpkg-deb", "-x", deb, tmp],
                              stdout=subprocess.DEVNULL)
        return tmp

    def test_package_has_menu_icon_variants(self):
        deb = self._latest_deb()
        if not deb:
            self.skipTest("没有现成的 deb")
        listing = subprocess.check_output(["dpkg-deb", "-c", deb], text=True)
        self.assertIn("hicolor/scalable/apps/docseek.svg", listing)
        self.assertIn("usr/share/pixmaps/docseek.svg", listing)
        self.assertRegex(listing, r"hicolor/\d+x\d+/apps/docseek\.png",
                         "打包时应该用 SVG 渲染出位图图标")

    def test_package_desktop_entry_has_wmclass(self):
        deb = self._latest_deb()
        if not deb:
            self.skipTest("没有现成的 deb")
        import tempfile
        tmp = self._extract(deb)
        self.addCleanup(_trash, tmp)
        path = os.path.join(tmp, "usr/share/applications/docseek.desktop")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("StartupWMClass=docseek", text)
        self.assertIn("Exec=docseek gui", text)

    def test_package_desktop_entry_icon_exists_in_package(self):
        """菜单/桌面快捷方式的图标必须真在包里，且写成绝对路径。"""
        deb = self._latest_deb()
        if not deb:
            self.skipTest("没有现成的 deb")
        tmp = self._extract(deb)
        self.addCleanup(_trash, tmp)
        path = os.path.join(tmp, "usr/share/applications/docseek.desktop")
        icon = ""
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Icon="):
                    icon = line.strip().partition("=")[2]
                    break
        self.assertTrue(icon.startswith("/usr/share/icons/"), icon)
        self.assertTrue(os.path.isfile(os.path.join(tmp, icon.lstrip("/"))),
                        "desktop 里写的图标 %r 不在包里" % icon)

    def test_package_metadata_is_honest(self):
        deb = self._latest_deb()
        if not deb:
            self.skipTest("没有现成的 deb")
        info = subprocess.check_output(["dpkg-deb", "-f", deb], text=True)
        self.assertIn("python3 (>= 3.7)", info)
        size = int(re.search(r"Installed-Size: (\d+)", info).group(1))
        tmp = self._extract(deb)
        self.addCleanup(_trash, tmp)
        real = sum(os.path.getsize(os.path.join(b, n))
                   for b, _d, fs in os.walk(tmp) for n in fs) // 1024
        # dpkg 以 KB 计块大小，允许 4 倍以内误差（块对齐、稀疏文件都可能）
        self.assertGreater(size, 0)
        self.assertLess(size, max(real, 1) * 4, "Installed-Size 比实际大太多")


def _trash(path):
    """清理解包目录：自造临时目录直接删，其它走回收站（见 tests/_scratch.py）。"""
    _discard_scratch(path)


if __name__ == "__main__":
    unittest.main()
