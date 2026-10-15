"""子进程回收的测试。

这类行为必须在**独立进程**里验证：`terminate_all()` 会把模块置为"正在退出"状态，
在测试进程里调用会污染同一进程内其它用例的子进程（例如后面 test_legacy 的 tesseract）。
所以每个用例都起一个 python 子进程跑真实逻辑，再看结果。
"""

import os
import subprocess
import sys
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_in_child(body):
    """在独立 python 进程里执行 body（已装好 procs），返回 stdout。"""
    code = ("import sys, os, time, subprocess\n"
            "sys.path.insert(0, %r)\n"
            "from docseek import procs\n"
            "procs.install()\n" % ROOT) + textwrap.dedent(body)
    proc = subprocess.run([sys.executable, "-c", code], timeout=60,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AssertionError("子进程失败：%s\n%s" % (
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace")


class ProcsTest(unittest.TestCase):
    def test_children_are_registered_and_killed(self):
        out = run_in_child("""
            p1 = subprocess.Popen(["sleep", "60"])
            p2 = subprocess.Popen(["sleep", "60"])
            print("active_before", len([p for p in procs.active() if p.poll() is None]))
            procs.terminate_all()
            time.sleep(0.4)
            alive = 0
            for pid in (p1.pid, p2.pid):
                try:
                    os.kill(pid, 0)
                    alive += 1
                except OSError:
                    pass
            print("still_alive", alive)
        """)
        self.assertIn("active_before 2", out)
        self.assertIn("still_alive 0", out)

    def test_new_child_spawned_during_shutdown_is_killed(self):
        """退出流程开始后仍被派生出来的子进程，也必须立刻收掉（否则又成孤儿）。"""
        out = run_in_child("""
            procs.terminate_all()              # 进入退出状态
            late = subprocess.Popen(["sleep", "60"])
            time.sleep(0.5)
            # 注意用 poll() 而不是 os.kill(pid, 0)：被 SIGKILL 但还没被收割的僵尸
            # 进程对 kill(pid, 0) 也是"存在"的。
            late.poll()
            print("late_alive", 0 if late.returncode is not None else 1)
        """)
        self.assertIn("late_alive 0", out)

    def test_signal_handler_recycles_children(self):
        """SIGTERM 也要收子进程（timeout / 注销 / kill 走的都是这条路）。"""
        code = textwrap.dedent("""
            import sys, os, time, signal, subprocess
            sys.path.insert(0, %r)
            from docseek import procs
            procs.install()
            procs.install_signals((signal.SIGTERM,))
            child = subprocess.Popen(["sleep", "60"])
            sys.stdout.write("child_pid %%d\\n" %% child.pid)
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        """) % ROOT
        proc = subprocess.run([sys.executable, "-c", code], timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid = int(proc.stdout.decode().split("child_pid")[1].split()[0])
        time.sleep(0.3)
        alive = True
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
        if alive:                       # 兜底清理，避免测试自己留下孤儿
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.assertFalse(alive, "SIGTERM 后子进程仍存活（pid=%d）" % pid)
        self.assertIn("收到信号", proc.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
