"""自动化场景不许在真实桌面弹窗口。

`Gio.AppInfo.launch_default_for_uri` 是走会话总线请桌面环境打开的——程序跑在
xvfb 之类的虚拟显示上，窗口照样弹到用户的真实桌面上。所以 `DOCSEEK_NO_LAUNCH=1`
时 `_launch_uri` 与 `_open` 的兜底路径都必须闭嘴。

这个用例单独放一个文件：test_ui.py 的 setUpModule 会把 `_launch_uri` 整体换成
mock（那样就测不到真实现了）。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk                          # noqa: F401
    HAVE_GTK = True
except Exception:                                          # noqa: BLE001
    HAVE_GTK = False

from docseek.ui import window as window_module             # noqa: E402


class LaunchUriGuardTest(unittest.TestCase):
    def test_guarded_launch_never_reaches_the_desktop(self):
        with mock.patch.dict(os.environ, {"DOCSEEK_NO_LAUNCH": "1"}), \
                mock.patch.object(window_module.Gio.AppInfo,
                                  "launch_default_for_uri") as launcher:
            self.assertFalse(window_module._launch_uri("file:///tmp"))
        launcher.assert_not_called()

    def test_unguarded_launch_still_works(self):
        env = {k: v for k, v in os.environ.items() if k != "DOCSEEK_NO_LAUNCH"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(window_module.Gio.AppInfo,
                                  "launch_default_for_uri",
                                  return_value=True) as launcher:
            self.assertTrue(window_module._launch_uri("file:///tmp"))
        launcher.assert_called_once()


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
class OpenGuardTest(unittest.TestCase):
    """窗口的"打开"路径：闸门打开时连 xdg-open / gio 兜底也不许跑。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-nolaunch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = os.path.join(self.tmp, "样例.txt")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("内容\n")

    def _window(self):
        window = window_module.MainWindow(db_path=os.path.join(self.tmp, "guard.db"))
        self.addCleanup(window.destroy)
        return window

    def test_open_does_not_spawn_anything_when_guarded(self):
        window = self._window()
        with mock.patch.dict(os.environ, {"DOCSEEK_NO_LAUNCH": "1"}), \
                mock.patch.object(window_module, "procs") as fake_procs:
            fake_procs.Popen.side_effect = AssertionError("不该启动外部程序")
            self.assertFalse(window._open(self.target))
        fake_procs.Popen.assert_not_called()

    def test_open_reports_target_when_not_guarded(self):
        window = self._window()
        env = {k: v for k, v in os.environ.items() if k != "DOCSEEK_NO_LAUNCH"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(window_module, "_launch_uri",
                                  return_value=True) as launcher:
            self.assertTrue(window._open(self.target))
        launcher.assert_called_once()


if __name__ == "__main__":
    unittest.main()
