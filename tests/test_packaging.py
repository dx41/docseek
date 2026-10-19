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


class BuiltPackageTest(unittest.TestCase):
    """有现成 deb 就顺手体检一下；没有就跳过（CI/新克隆时允许）。"""

    def _latest_deb(self):
        for folder in (os.path.join(os.path.expanduser("~"), "下载"),
                       os.path.join(os.path.expanduser("~"), "Downloads")):
            if not os.path.isdir(folder):
                continue
            debs = [os.path.join(folder, n) for n in os.listdir(folder)
                    if re.match(r"^docseek_[\d.]+_all\.deb$", n)]
            if debs:
                return max(debs, key=os.path.getmtime)
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


if __name__ == "__main__":
    unittest.main()
