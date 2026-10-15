"""子进程登记与回收。

为什么需要这个模块
------------------
抽取与 OCR 会起大量外部进程（tesseract、pdftoppm、soffice）。**程序退出时子进程不会
自动消失**——实测关掉窗口后，已启动的 tesseract / pdftoppm 变成孤儿继续满核跑，
用户看到的就是"软件关了 CPU 还是 100%"。

做法：
* 启动时把 `subprocess.Popen` 换成"会登记"的版本，并让每个子进程自成进程组
  （`start_new_session=True`），这样能整组一起收；
* 退出路径（atexit / 窗口销毁 / Ctrl-C）统一调用 `terminate_all()`：先 TERM，
  宽限后 KILL。

所有模块都不用改：`subprocess.run()` 内部也是走 `Popen`，所以一次替换即可覆盖。
"""

import atexit
import os
import signal
import subprocess
import sys
import threading
import time

_real_popen = subprocess.Popen
_children = {}
_lock = threading.Lock()
_installed = False
_shutting_down = threading.Event()


def install():
    """把 Popen 换成会登记的版本（幂等）。"""
    global _installed
    if _installed:
        return False
    _installed = True

    def tracking_popen(*args, **kwargs):
        # 自成进程组：既能整组终止，也不会把 Ctrl-C 传给子进程
        kwargs.setdefault("start_new_session", True)
        proc = _real_popen(*args, **kwargs)
        with _lock:
            _collect_finished_locked()
            _children[proc.pid] = proc
            # 登记后再确认一次：退出流程可能恰好发生在"检查"与"登记"之间，
            # 那一瞬间的子进程会被 terminate_all 的快照漏掉，成为新的孤儿。
            racing = _shutting_down.is_set()
            if racing:
                _children.pop(proc.pid, None)
        if racing:
            _signal_group(proc, signal.SIGKILL)
        return proc

    subprocess.Popen = tracking_popen
    atexit.register(terminate_all)
    return True


def install_signals(signals=(signal.SIGTERM,)):
    """接住 SIGTERM 之类的退出信号，先收子进程再退出。

    没有这一步的话，被 `timeout`／注销／kill 掉时不会走 finally，
    子进程照样变成孤儿。
    """
    def handler(signum, _frame):
        count = terminate_all()
        sys.stderr.write("\n[docseek] 收到信号 %d，已回收 %d 个子进程\n"
                         % (signum, count))
        try:
            sys.stderr.flush()      # os._exit 不冲刷缓冲区，必须显式 flush
        except Exception:           # noqa: BLE001
            pass
        time.sleep(0.3)         # 给仍在线程里的派生动作一点时间
        terminate_all()         # 再收一次，兜住上一步漏网的
        os._exit(128 + signum)  # 直接退出：此刻走解释器收尾容易卡住

    for sig in signals:
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass


def _collect_finished_locked():
    for pid in [pid for pid, proc in _children.items() if proc.poll() is not None]:
        _children.pop(pid, None)


def active():
    """仍在运行的子进程列表。"""
    with _lock:
        _collect_finished_locked()
        return list(_children.values())


def terminate_all(grace=2.0):
    """终止所有仍在运行的子进程，返回处理的数量。之后新起的子进程会被立即杀掉。"""
    _shutting_down.set()
    with _lock:
        procs = list(_children.values())
        _children.clear()
    alive = [proc for proc in procs if proc.poll() is None]
    if not alive:
        return 0
    for proc in alive:
        _signal_group(proc, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if all(proc.poll() is not None for proc in alive):
            break
        time.sleep(0.05)
    for proc in alive:
        if proc.poll() is None:
            _signal_group(proc, signal.SIGKILL)
    return len(alive)


def _signal_group(proc, sig):
    """优先整组终止，但**绝不**对自己的进程组下手。

    刚 fork 出来、还没完成 setsid() 的子进程，getpgid() 会返回我们自己的进程组——
    那时 killpg 把整个组（包括我们自己，甚至桌面会话）一起带走，后果很严重。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass
    try:
        proc.send_signal(sig)
    except (OSError, ValueError):
        pass
