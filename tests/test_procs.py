"""子进程登记：退出收尾的作用域不能毒化宿主进程。"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import procs                                # noqa: E402


class ShutdownScopeTest(unittest.TestCase):
    def setUp(self):
        procs.install()

    def tearDown(self):
        # 这些用例故意制造/解除"正在退出"状态，跑完必须复位，
        # 否则同一进程里后续用例起的子进程会被立即 SIGKILL。
        procs.release_shutdown()

    def _echo(self, text="hi"):
        proc = subprocess.Popen(["/bin/sh", "-c", "printf %s " + text],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(timeout=30)
        return proc.returncode, out

    def test_plain_terminate_keeps_process_usable(self):
        """命令收尾（sticky=False）之后，宿主还能正常起子进程。"""
        procs.terminate_all(grace=0.2, sticky=False)
        code, out = self._echo("ok")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"ok")

    def test_sticky_terminate_kills_late_children_then_release_recovers(self):
        """真退出路径（sticky）下新子进程会被立刻收掉；release 后可恢复。"""
        procs.terminate_all(grace=0.2, sticky=True)
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -9, "退出中的进程不应还能留下子进程")
        procs.release_shutdown()
        code, out = self._echo("back")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"back")

    def test_children_are_registered_while_running(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 5"])
        try:
            self.assertIn(proc, procs.active())
        finally:
            proc.kill()
            proc.wait(timeout=30)
            procs.terminate_all(grace=0.2, sticky=False)


if __name__ == "__main__":
    unittest.main()
