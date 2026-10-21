"""启动期兜底：换台电脑装上以后，最怕"点了没反应"。

这一组盯的是 0.1.4 新增的几道保险：
    * 启动自检（SQLite 有没有 FTS5、数据目录能不能写）
    * 出错弹窗（菜单启动没有终端，错误必须能看见）
    * 异常日志（用户说不清时，日志能替他说）
    * 单实例（两个窗口各跑一个索引线程会把 CPU 吃满）
    * WM_CLASS（任务栏/开始菜单靠它找图标）
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from docseek import cli, config  # noqa: E402
from docseek.index.db import FTS5_HELP, probe_fts5  # noqa: E402


class Fts5ProbeTest(unittest.TestCase):
    def test_probe_reports_available_here(self):
        ok, detail = probe_fts5()
        self.assertTrue(ok, "本机 sqlite 应该带 FTS5：%s" % detail)

    def test_help_text_tells_user_what_to_do(self):
        self.assertIn("FTS5", FTS5_HELP)
        self.assertIn("docseek doctor", FTS5_HELP)


class PreflightTest(unittest.TestCase):
    def _run(self, home):
        env = dict(os.environ, HOME=home, PYTHONPATH=ROOT,
                   DOCSEEK_NO_DIALOG="1")
        return subprocess.run(
            [sys.executable, "-m", "docseek.cli", "--preflight"],
            capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)

    def test_passes_on_a_clean_home(self):
        home = tempfile.mkdtemp(prefix="docseek-pre-")
        self.addCleanup(_remove, home)
        result = self._run(home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(home) / ".local/share/docseek").is_dir(),
                        "自检顺带要把数据目录建好")

    def test_preflight_is_hidden_from_help(self):
        result = subprocess.run([sys.executable, "-m", "docseek.cli", "--help"],
                                capture_output=True, text=True, cwd=ROOT,
                                env=dict(os.environ, PYTHONPATH=ROOT))
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("preflight", result.stdout)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, "bin", "docseek")
        with open(self.path, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_shell_syntax(self):
        result = subprocess.run(["sh", "-n", self.path], capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_preflights_before_starting(self):
        self.assertIn("--preflight", self.text)

    def test_launcher_defaults_to_gui_when_bare(self):
        # 终端里敲 docseek 应该直接开界面，而不是甩一段帮助
        self.assertIn("set -- gui", self.text)

    def test_launcher_surfaces_errors_without_a_terminal(self):
        for tool in ("zenity", "kdialog", "xmessage"):
            self.assertIn(tool, self.text, "菜单启动失败时至少要能弹窗")

    def test_launcher_works_when_readlink_is_missing(self):
        # 精简 PATH 里可能没有 coreutils；不能因此 exit 127
        self.assertIn("command -v readlink", self.text)

    def test_launcher_runs_version(self):
        home = tempfile.mkdtemp(prefix="docseek-lau-")
        self.addCleanup(_remove, home)
        result = subprocess.run([self.path, "--version"], capture_output=True,
                                text=True, timeout=120,
                                env=dict(os.environ, HOME=home,
                                         PYTHONPATH="", DOCSEEK_NO_DIALOG="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DocSeek", result.stdout)


class SingleInstanceTest(unittest.TestCase):
    def test_second_instance_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="docseek-inst-")
        self.addCleanup(_remove, tmp)
        db = os.path.join(tmp, "index.db")
        first_ok, handle = cli._single_instance(db)
        self.assertTrue(first_ok)
        self.assertIsNotNone(handle)
        second_ok, second_handle = cli._single_instance(db)
        self.assertFalse(second_ok, "同一份索引不能同时开两个界面")
        self.assertIsNone(second_handle)
        handle.close()
        third_ok, third_handle = cli._single_instance(db)
        self.assertTrue(third_ok, "锁释放后应该还能再开")
        third_handle.close()

    def test_unwritable_location_does_not_block_startup(self):
        ok, handle = cli._single_instance("/proc/nonexistent/index.db")
        self.assertTrue(ok, "连锁文件都建不了时不该拦住用户")
        self.assertIsNone(handle)


class LoggingTest(unittest.TestCase):
    def test_exception_goes_into_log_file(self):
        tmp = tempfile.mkdtemp(prefix="docseek-log-")
        self.addCleanup(_remove, tmp)
        with mock.patch.object(config, "DATA_DIR", Path(tmp)):
            try:
                raise ValueError("装了别人的机器")
            except ValueError:
                path = cli._log_exception("selftest")
        self.assertIsNotNone(path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("装了别人的机器", text)
        self.assertIn("selftest", text)

    def test_log_rotates_instead_of_growing_forever(self):
        tmp = tempfile.mkdtemp(prefix="docseek-log2-")
        self.addCleanup(_remove, tmp)
        log = Path(tmp) / "docseek.log"
        log.write_text("x" * (600 * 1024), encoding="utf-8")
        with mock.patch.object(config, "DATA_DIR", Path(tmp)):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                cli._log_exception("rotate")
        self.assertTrue((Path(tmp) / "docseek.log.1").exists(),
                        "大日志要滚动一份，别无限长")
        self.assertLess(log.stat().st_size, 600 * 1024)


class DialogTest(unittest.TestCase):
    def test_dialog_is_skipped_when_disabled(self):
        with mock.patch.dict(os.environ, {"DOCSEEK_NO_DIALOG": "1"}):
            self.assertFalse(cli._show_error_dialog("测试"))

    def test_dialog_is_skipped_without_display(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DOCSEEK_NO_DIALOG", "DISPLAY", "WAYLAND_DISPLAY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(cli._show_error_dialog("测试"))


class WindowClassTest(unittest.TestCase):
    def test_wmclass_matches_desktop_entry(self):
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
        except Exception:                            # noqa: BLE001
            self.skipTest("没有 Gdk 绑定")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("没有图形环境")
        self.assertTrue(cli._set_window_class())
        self.assertEqual(Gdk.get_program_class(), "docseek")


def _remove(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class VersionDisplayTest(unittest.TestCase):
    """界面上显示的版本号必须跟着包走——0.1.4 发布时"关于"里还写死着 0.1.0，
    用户装了新版一看"还是 0.1.0"，以为没装上。"""

    def setUp(self):
        from docseek import __version__
        self.version = __version__
        self.src_dir = os.path.join(ROOT, "docseek")

    def _sources(self):
        for base, dirs, files in os.walk(self.src_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(base, name)

    def test_about_dialog_reads_the_package_version(self):
        with open(os.path.join(self.src_dir, "ui", "window.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("set_version(__version__)", text)
        self.assertNotRegex(text, r'set_version\("\d')

    def test_no_module_hardcodes_a_version_literal(self):
        import re
        pattern = re.compile(r'"\d+\.\d+\.\d+"')
        bad = []
        for path in self._sources():
            if path.endswith(os.path.join("docseek", "__init__.py")):
                continue            # 唯一允许写版本号的地方
            with open(path, encoding="utf-8") as fh:
                for no, line in enumerate(fh, 1):
                    if pattern.search(line) and "require_version" not in line:
                        bad.append("%s:%d %s" % (os.path.relpath(path, ROOT),
                                                 no, line.strip()))
        self.assertEqual(bad, [], "这些地方把版本号写死了，升级后会跟界面/包对不上：\n"
                         + "\n".join(bad))

    def test_cli_version_matches_package(self):
        result = subprocess.run(
            [sys.executable, "-m", "docseek.cli", "--version"],
            capture_output=True, text=True, cwd=ROOT,
            env=dict(os.environ, PYTHONPATH=ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "DocSeek %s" % self.version)
