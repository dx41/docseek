"""图形界面入口。

    python3 -m docseek.cli gui
    bin/docseek gui

退出路径（关窗口 / Ctrl-C）都会：先让索引线程停下，再回收所有子进程
（tesseract、pdftoppm、soffice），避免"软件关了 CPU 还在满"的情况。
"""

import sys


def main(db_path=None):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from .. import procs
    from .window import MainWindow

    import signal
    procs.install()
    procs.install_signals((signal.SIGTERM,))   # 注销/被杀时也要收掉子进程
    window = MainWindow(db_path=db_path)

    def on_destroy(*_args):
        window.shutdown()          # 停索引 + 回收子进程
        Gtk.main_quit()

    window.connect("destroy", on_destroy)
    window.show_all()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        procs.terminate_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
